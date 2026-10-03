from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deletion
from deletion import (
    atomic_move_within_storage,read_transaction,storage_target_path,trash_target_path,
    validate_transaction,write_transaction,
)
from studio import Studio
from studio_io import durable_write_json,read_json


def transaction(**changes):
    value={
        'version':1,
        'transaction_id':'1'*32,
        'run_id':'a'*16,
        'dataset_id':'d'*16,
        'status':'prepared',
        'created_at':datetime.now(timezone.utc).isoformat(),
        'committed_at':None,
        'dataset_shared':False,
        'dependent_run_ids':['b'*16],
        'targets':[{'kind':'run','id':'a'*16},{'kind':'dataset','id':'d'*16}],
        'moved':[],
        'cleanup_attempts':0,
        'last_error':None,
    }
    value.update(changes)
    return value


def test_studio_creates_deletion_storage_layout(tmp_path):
    Studio(tmp_path)
    assert (tmp_path/'deletion-journal').is_dir()
    assert (tmp_path/'deletion-journal/receipts').is_dir()
    assert (tmp_path/'trash').is_dir()


def test_durable_json_writes_and_replaces(tmp_path):
    target=tmp_path/'record.json'
    durable_write_json(target,{'value':'old'})
    durable_write_json(target,{'value':'new'})
    assert read_json(target)=={'value':'new'}
    assert list(tmp_path.glob('record.json.*.tmp'))==[]


def test_failed_durable_write_preserves_old_file_and_cleans_temp(tmp_path,monkeypatch):
    target=tmp_path/'record.json'
    target.write_text('{"value":"old"}',encoding='utf-8')
    monkeypatch.setattr(os,'fsync',lambda _fd: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError,match='disk failure'):
        durable_write_json(target,{'value':'new'})
    assert json.loads(target.read_text(encoding='utf-8'))=={'value':'old'}
    assert list(tmp_path.glob('record.json.*.tmp'))==[]


@pytest.mark.parametrize('changes',[{'status':'unknown'},{'run_id':'../outside'},
    {'dataset_id':'D'*16},{'transaction_id':'short'},{'version':2},
    {'last_error':r'access denied: C:\\external\\secret'}])
def test_transaction_rejects_invalid_status_ids_and_version(changes):
    with pytest.raises(ValueError):
        validate_transaction(transaction(**changes))


def test_transaction_journal_round_trip_and_strict_fields(tmp_path):
    Studio(tmp_path)
    value=transaction()
    path=write_transaction(tmp_path,value)
    assert path==tmp_path/'deletion-journal'/('1'*32+'.json')
    assert read_transaction(tmp_path,'1'*32)==value
    damaged={**value,'external_path':r'C:\\Users\\victim'}
    path.write_text(json.dumps(damaged),encoding='utf-8')
    with pytest.raises(ValueError,match='schema'):
        read_transaction(tmp_path,'1'*32)


def test_transaction_descriptors_cannot_construct_paths_outside_storage(tmp_path):
    Studio(tmp_path)
    with pytest.raises(ValueError):
        storage_target_path(tmp_path,{'kind':'run','id':'../outside'})
    with pytest.raises(ValueError):
        storage_target_path(tmp_path,{'kind':'external','id':'a'*16})
    with pytest.raises(ValueError):
        trash_target_path(tmp_path,'../outside',{'kind':'run','id':'a'*16})


def test_atomic_directory_move_succeeds(tmp_path):
    Studio(tmp_path)
    source=tmp_path/'runs'/('a'*16); source.mkdir(); (source/'state.json').write_text('{}')
    destination=tmp_path/'trash'/('1'*32)/'run'/('a'*16)
    destination.parent.mkdir(parents=True)
    atomic_move_within_storage(tmp_path,source,destination)
    assert not source.exists() and (destination/'state.json').is_file()


def test_atomic_move_rejects_existing_target_and_out_of_bounds(tmp_path):
    Studio(tmp_path)
    source=tmp_path/'runs'/('a'*16); source.mkdir()
    destination=tmp_path/'trash'/'occupied'; destination.mkdir()
    with pytest.raises(FileExistsError):
        atomic_move_within_storage(tmp_path,source,destination)
    with pytest.raises(ValueError,match='outside'):
        atomic_move_within_storage(tmp_path,source,tmp_path.parent/'escape')


def test_atomic_move_propagates_permission_error_without_fallback(tmp_path,monkeypatch):
    Studio(tmp_path)
    source=tmp_path/'runs'/('a'*16); source.mkdir()
    destination=tmp_path/'trash'/'destination'
    failure=PermissionError('directory is in use')
    monkeypatch.setattr(deletion.os,'replace',lambda *_args: (_ for _ in ()).throw(failure))
    with pytest.raises(PermissionError) as raised:
        atomic_move_within_storage(tmp_path,source,destination)
    assert raised.value is failure and source.is_dir() and not destination.exists()


def test_atomic_move_rejects_cross_volume(tmp_path,monkeypatch):
    Studio(tmp_path)
    source=tmp_path/'runs'/('a'*16); source.mkdir()
    destination=tmp_path/'trash'/'destination'
    real_key=deletion._volume_key
    monkeypatch.setattr(deletion,'_volume_key',lambda path: ('fake','source' if Path(path)==source else 'target'))
    with pytest.raises(ValueError,match='Cross-volume'):
        atomic_move_within_storage(tmp_path,source,destination)
    monkeypatch.setattr(deletion,'_volume_key',real_key)


def test_atomic_move_rejects_reparse_component(tmp_path,monkeypatch):
    Studio(tmp_path)
    source=tmp_path/'runs'/('a'*16); source.mkdir()
    real_lstat=os.lstat; marker=object()
    monkeypatch.setattr(deletion.os,'lstat',lambda path: marker if Path(path)==source else real_lstat(path))
    monkeypatch.setattr(deletion,'_is_reparse',lambda info: info is marker)
    with pytest.raises(ValueError,match='Symbolic links'):
        atomic_move_within_storage(tmp_path,source,tmp_path/'trash'/'destination')
