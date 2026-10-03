"""Durable building blocks for recoverable training-record deletion."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import stat

from studio_io import durable_write_json,read_json


TRANSACTION_VERSION=1
TRANSACTION_STATUSES=frozenset({
    'prepared','committed','staged','cleanup_pending','cleaned',
    'manual_intervention_required',
})
_ID_LENGTHS={'run':16,'dataset':16,'import':16,'request':32}
_STORAGE_FOLDERS={'run':'runs','dataset':'datasets','import':'imports','request':'requests'}
_FIELDS=frozenset({
    'version','transaction_id','run_id','dataset_id','status','created_at','committed_at',
    'dataset_shared','dependent_run_ids','targets','moved','cleanup_attempts','last_error',
})
_RECEIPT_FIELDS=frozenset({
    'version','transaction_id','run_id','status','dataset_shared','deleted_dataset',
    'deleted_requests','dependent_runs','committed_at','cleaned_at',
})
_LINEAGE_FIELDS=frozenset({
    'version','transaction_id','base_run','base_run_deleted','base_run_snapshot','deleted_at',
})


def _valid_hex_id(value,length):
    return isinstance(value,str) and len(value)==length and all(c in '0123456789abcdef' for c in value)


def _validate_timestamp(value,field,nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value,str):
        raise ValueError(f'{field} must be an ISO-8601 timestamp.')
    try:
        parsed=datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'{field} must be an ISO-8601 timestamp.') from error
    if parsed.tzinfo is None:
        raise ValueError(f'{field} must include a timezone.')


def validate_target(value):
    if not isinstance(value,dict) or set(value)!= {'kind','id'}:
        raise ValueError('Deletion targets must contain only kind and id.')
    kind=value.get('kind'); identity=value.get('id')
    if kind not in _ID_LENGTHS or not _valid_hex_id(identity,_ID_LENGTHS.get(kind,0)):
        raise ValueError('Deletion target kind or id is invalid.')
    return {'kind':kind,'id':identity}


def validate_transaction(value):
    """Return a normalized transaction after strict, versioned schema validation."""
    if not isinstance(value,dict) or set(value)!=_FIELDS:
        raise ValueError('Deletion transaction fields do not match the schema.')
    if type(value['version']) is not int or value['version']!=TRANSACTION_VERSION:
        raise ValueError('Unsupported deletion transaction version.')
    if not _valid_hex_id(value['transaction_id'],32):
        raise ValueError('Invalid deletion transaction id.')
    if not _valid_hex_id(value['run_id'],16) or not _valid_hex_id(value['dataset_id'],16):
        raise ValueError('Invalid run or dataset id.')
    if value['status'] not in TRANSACTION_STATUSES:
        raise ValueError('Invalid deletion transaction status.')
    _validate_timestamp(value['created_at'],'created_at')
    _validate_timestamp(value['committed_at'],'committed_at',nullable=True)
    if type(value['dataset_shared']) is not bool:
        raise ValueError('dataset_shared must be a boolean.')
    dependent=value['dependent_run_ids']
    if not isinstance(dependent,list) or any(not _valid_hex_id(item,16) for item in dependent):
        raise ValueError('dependent_run_ids must contain valid run ids.')
    if len(set(dependent))!=len(dependent):
        raise ValueError('dependent_run_ids must not contain duplicates.')
    targets=value['targets']; moved=value['moved']
    if not isinstance(targets,list) or not isinstance(moved,list):
        raise ValueError('targets and moved must be lists.')
    normalized_targets=[validate_target(item) for item in targets]
    normalized_moved=[validate_target(item) for item in moved]
    target_keys={(item['kind'],item['id']) for item in normalized_targets}
    moved_keys=[(item['kind'],item['id']) for item in normalized_moved]
    if len(target_keys)!=len(normalized_targets) or len(set(moved_keys))!=len(moved_keys):
        raise ValueError('Deletion targets must not contain duplicates.')
    if any(item not in target_keys for item in moved_keys):
        raise ValueError('moved entries must also appear in targets.')
    attempts=value['cleanup_attempts']
    if type(attempts) is not int or attempts<0:
        raise ValueError('cleanup_attempts must be a non-negative integer.')
    if value['last_error'] is not None and not isinstance(value['last_error'],str):
        raise ValueError('last_error must be a string or null.')
    if isinstance(value['last_error'],str) and ('/' in value['last_error'] or '\\' in value['last_error']):
        raise ValueError('last_error must not contain filesystem paths.')
    return {
        **value,
        'dependent_run_ids':list(dependent),
        'targets':normalized_targets,
        'moved':normalized_moved,
    }


def _storage_root(storage):
    root=Path(os.path.abspath(os.fspath(storage)))
    _reject_reparse(root,root)
    return root


def _journal_path(storage,transaction_id):
    if not _valid_hex_id(transaction_id,32):
        raise ValueError('Invalid deletion transaction id.')
    base=_storage_root(storage)/'deletion-journal'
    _reject_reparse(_storage_root(storage),base)
    return base/f'{transaction_id}.json'


def write_transaction(storage,value):
    transaction=validate_transaction(value)
    path=_journal_path(storage,transaction['transaction_id'])
    durable_write_json(path,transaction)
    return path


def read_transaction(storage,transaction_id):
    return validate_transaction(read_json(_journal_path(storage,transaction_id)))


def delete_transaction(storage,transaction_id):
    _journal_path(storage,transaction_id).unlink(missing_ok=True)


def _receipt_path(storage,run_id):
    if not _valid_hex_id(run_id,16):
        raise ValueError('Invalid receipt run id.')
    base=_storage_root(storage)/'deletion-journal'/'receipts'
    _reject_reparse(_storage_root(storage),base)
    return base/f'{run_id}.json'


def validate_receipt(value):
    if not isinstance(value,dict) or set(value)!=_RECEIPT_FIELDS:
        raise ValueError('Deletion receipt fields do not match the schema.')
    if type(value['version']) is not int or value['version']!=TRANSACTION_VERSION:
        raise ValueError('Unsupported deletion receipt version.')
    if not _valid_hex_id(value['transaction_id'],32) or not _valid_hex_id(value['run_id'],16):
        raise ValueError('Invalid deletion receipt id.')
    if value['status']!='cleaned': raise ValueError('Deletion receipt status must be cleaned.')
    if type(value['dataset_shared']) is not bool or type(value['deleted_dataset']) is not bool:
        raise ValueError('Deletion receipt booleans are invalid.')
    for field in ('deleted_requests','dependent_runs'):
        if type(value[field]) is not int or value[field]<0:
            raise ValueError(f'{field} must be a non-negative integer.')
    _validate_timestamp(value['committed_at'],'committed_at')
    _validate_timestamp(value['cleaned_at'],'cleaned_at')
    return dict(value)


def write_receipt(storage,value):
    receipt=validate_receipt(value)
    durable_write_json(_receipt_path(storage,receipt['run_id']),receipt)


def read_receipt(storage,run_id):
    return validate_receipt(read_json(_receipt_path(storage,run_id)))


def validate_lineage(value):
    if not isinstance(value,dict) or set(value)!=_LINEAGE_FIELDS:
        raise ValueError('Lineage snapshot fields do not match the schema.')
    if type(value['version']) is not int or value['version']!=TRANSACTION_VERSION:
        raise ValueError('Unsupported lineage snapshot version.')
    if not _valid_hex_id(value['transaction_id'],32) or not _valid_hex_id(value['base_run'],16):
        raise ValueError('Invalid lineage snapshot id.')
    if value['base_run_deleted'] is not True:
        raise ValueError('Lineage snapshot must describe a deleted base run.')
    snapshot=value['base_run_snapshot']
    if not isinstance(snapshot,dict) or set(snapshot)!= {'name'}:
        raise ValueError('Lineage snapshot name is invalid.')
    if not isinstance(snapshot['name'],str) or not snapshot['name'] or len(snapshot['name'])>80:
        raise ValueError('Lineage snapshot name is invalid.')
    _validate_timestamp(value['deleted_at'],'deleted_at')
    return {**value,'base_run_snapshot':dict(snapshot)}


def write_lineage_snapshot(path,value):
    lineage=validate_lineage(value)
    durable_write_json(Path(path),lineage)


def read_lineage_snapshot(path):
    return validate_lineage(read_json(Path(path)))


def storage_target_path(storage,target):
    target=validate_target(target)
    return _storage_root(storage)/_STORAGE_FOLDERS[target['kind']]/target['id']


def trash_target_path(storage,transaction_id,target):
    target=validate_target(target)
    if not _valid_hex_id(transaction_id,32):
        raise ValueError('Invalid deletion transaction id.')
    root=_storage_root(storage)/'trash'/transaction_id
    if target['kind']=='request':
        return root/'requests'/target['id']
    return root/target['kind']


def transaction_ids(storage):
    """List validated journal filenames without interpreting their contents."""
    base=_storage_root(storage)/'deletion-journal'
    _reject_reparse(_storage_root(storage),base)
    identities=[]
    for path in base.glob('*.json'):
        if not _valid_hex_id(path.stem,32):
            raise ValueError('Deletion journal filename is invalid.')
        identities.append(path.stem)
    return sorted(identities)


def receipt_run_ids(storage):
    base=_storage_root(storage)/'deletion-journal'/'receipts'
    _reject_reparse(_storage_root(storage),base)
    identities=[]
    for path in base.glob('*.json'):
        if not _valid_hex_id(path.stem,16): raise ValueError('Deletion receipt filename is invalid.')
        identities.append(path.stem)
    return sorted(identities)


def target_locations(storage,transaction_id,target):
    """Reconstruct the only permitted live and trash paths for one target."""
    return storage_target_path(storage,target),trash_target_path(storage,transaction_id,target)


def safe_error(error):
    """Return a durable diagnostic that cannot disclose or persist a path."""
    if getattr(error,'winerror',None) in (32,33): return 'file_in_use'
    if isinstance(error,PermissionError): return 'permission_denied'
    if isinstance(error,OSError): return 'os_error'
    return 'cleanup_error'


def _is_reparse(info):
    attributes=getattr(info,'st_file_attributes',0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400))


def _reject_reparse(root,path):
    """Reject links and Windows junctions in every existing path component."""
    root=Path(os.path.abspath(os.fspath(root)))
    path=Path(os.path.abspath(os.fspath(path)))
    try:
        relative=path.relative_to(root)
    except ValueError as error:
        raise ValueError('Path is outside training storage.') from error
    current=root
    candidates=(root,*[root.joinpath(*relative.parts[:index]) for index in range(1,len(relative.parts)+1)])
    for current in candidates:
        if not os.path.lexists(current):
            continue
        if _is_reparse(os.lstat(current)):
            raise ValueError('Symbolic links, junctions, and reparse points are not allowed.')


def _volume_key(path):
    drive=os.path.splitdrive(os.path.abspath(os.fspath(path)))[0]
    if drive:
        return ('drive',os.path.normcase(drive))
    existing=Path(path)
    while not os.path.lexists(existing):
        if existing.parent==existing:
            break
        existing=existing.parent
    return ('device',os.stat(existing).st_dev)


def atomic_move_within_storage(storage,source,destination):
    """Move a directory atomically inside one storage volume, without copy fallback."""
    root=_storage_root(storage)
    source=Path(os.path.abspath(os.fspath(source)))
    destination=Path(os.path.abspath(os.fspath(destination)))
    _reject_reparse(root,source)
    _reject_reparse(root,destination)
    if _volume_key(source)!=_volume_key(destination):
        raise ValueError('Cross-volume moves are not allowed.')
    if os.path.lexists(destination):
        raise FileExistsError(f'Deletion staging destination already exists: {destination}')
    os.replace(source,destination)
