import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import studio as studio_module
from deletion import read_lineage_snapshot,read_transaction,transaction_ids,write_transaction
from studio import Studio
from studio_io import read_json,write_json


PARENT_ID='a'*16
CHILD_IDS=('b'*16,'c'*16)
DATASET_ID='d'*16


class FakeProcess:
    def poll(self): return None
    def wait(self,timeout=None): return 0
    def kill(self): pass


def add_dataset(studio,identity=DATASET_ID):
    directory=studio.path('datasets',identity); directory.mkdir()
    write_json(directory/'data.json',{'id':identity,'name':'shared','kind':'text','created':'2026','documents':[]})


def add_run(studio,identity,name,dataset_id=DATASET_ID,base_run=None):
    directory=studio.path('runs',identity); (directory/'model').mkdir(parents=True)
    state={'id':identity,'name':name,'dataset_id':dataset_id,'dataset_name':'shared',
        'created':'2026','status':'completed','step':5,'total_steps':5,'mode':'continue' if base_run else 'pretrain',
        'base_run':base_run,'metrics':[]}
    write_json(directory/'state.json',state)
    (directory/'model/model.safetensors').write_text('independent weights',encoding='utf-8')
    return directory


def parent_with_children(root,count=2,shared_dataset=True):
    studio=Studio(root); add_dataset(studio)
    add_run(studio,PARENT_ID,'Parent Model')
    for index,identity in enumerate(CHILD_IDS[:count]):
        dataset_id=DATASET_ID if shared_dataset else chr(ord('e')+index)*16
        if dataset_id!=DATASET_ID: add_dataset(studio,dataset_id)
        add_run(studio,identity,f'Child {index+1}',dataset_id,base_run=PARENT_ID)
    return studio


def test_parent_deletion_snapshots_multiple_independent_children(tmp_path):
    studio=parent_with_children(tmp_path)
    result=studio.delete_run(PARENT_ID)
    assert result['dependent_runs']==2 and result['dataset_shared']
    for identity in CHILD_IDS:
        lineage=read_lineage_snapshot(studio.path('runs',identity)/'lineage.json')
        assert lineage['base_run']==PARENT_ID and lineage['base_run_deleted'] is True
        assert lineage['base_run_snapshot']=={'name':'Parent Model'}
        assert lineage['deleted_at'] and set(lineage)=={'version','transaction_id','base_run',
            'base_run_deleted','base_run_snapshot','deleted_at'}
    snapshot={item['id']:item for item in studio.snapshot()['runs']}
    assert all(snapshot[identity]['base_run_status']=='deleted' for identity in CHILD_IDS)
    assert all(snapshot[identity]['base_run_deleted'] is True for identity in CHILD_IDS)


def test_snapshot_distinguishes_present_deleted_and_unknown_lineage(tmp_path):
    studio=parent_with_children(tmp_path,count=1)
    child=next(item for item in studio.snapshot()['runs'] if item['id']==CHILD_IDS[0])
    assert child['base_run_status']=='present' and child['base_run_deleted'] is False
    result=studio.delete_run(PARENT_ID)
    deleted=next(item for item in studio.snapshot()['runs'] if item['id']==CHILD_IDS[0])
    assert deleted['base_run_status']=='deleted' and deleted['base_run_snapshot']['name']=='Parent Model'
    assert deleted['deleted_at']
    unknown_id='e'*16; add_run(studio,unknown_id,'Unknown parent child','f'*16,base_run='9'*16)
    unknown=next(item for item in studio.snapshot()['runs'] if item['id']==unknown_id)
    assert unknown['base_run_status']=='unknown' and unknown['base_run_deleted'] is None
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])


def test_lineage_write_failure_before_commit_keeps_parent_live(tmp_path,monkeypatch):
    studio=parent_with_children(tmp_path); real_write=studio_module.write_lineage_snapshot; calls=[]
    def fail_second(path,value):
        calls.append(Path(path))
        if len(calls)==2: raise OSError('durable lineage write failed')
        return real_write(path,value)
    monkeypatch.setattr(studio_module,'write_lineage_snapshot',fail_second)
    with pytest.raises(OSError,match='durable lineage'): studio.delete_run(PARENT_ID)
    assert studio.path('runs',PARENT_ID).is_dir()
    transaction=read_transaction(tmp_path,transaction_ids(tmp_path)[0])
    assert transaction['status']=='prepared'
    assert not (tmp_path/'trash'/transaction['transaction_id']/'run').exists()
    snapshot={item['id']:item for item in studio.snapshot()['runs']}
    assert all(snapshot[identity]['base_run_status']=='present' for identity in CHILD_IDS)


def test_child_creation_resume_and_inference_use_only_child_directories_after_parent_delete(tmp_path,monkeypatch):
    studio=Studio(tmp_path); add_dataset(studio); parent=add_run(studio,PARENT_ID,'Parent Model')
    for name,content in {'config.json':'{}','tokenizer.json':'tokenizer','training.json':'{"mode":"pretrain"}',
            'studio_model.py':'model code'}.items():
        (parent/'model'/name).write_text(content,encoding='utf-8')
    spawns=[]
    monkeypatch.setattr(studio,'spawn',lambda args,log,owner=None: spawns.append((args,log)) or FakeProcess())
    child=studio.start({'dataset_id':DATASET_ID,'name':'Derived','training_mode':'continue',
        'base_run':PARENT_ID,'steps':5,'preset':'demo'})
    child_id=child['id']; child_directory=studio.path('runs',child_id)
    request=read_json(child_directory/'request.json')
    assert Path(request['base_model'])==child_directory/'base_model'
    assert (child_directory/'base_model/model.safetensors').read_text()=='independent weights'
    state=read_json(child_directory/'state.json'); state.update(status='paused',step=1,total_steps=5)
    write_json(child_directory/'state.json',state); (child_directory/'checkpoint.pt').write_text('checkpoint')
    (child_directory/'model').mkdir(exist_ok=True)
    for name in ('model.safetensors','config.json','tokenizer.json','training.json'):
        (child_directory/'model'/name).write_text('{}')
    studio.process=None; studio.active_id=None
    deleted=studio.delete_run(PARENT_ID)
    assert not studio.path('runs',PARENT_ID).exists()
    studio.control(child_id,'resume')
    assert spawns[-1][0]==['train',str(child_directory)]
    studio.process=None; studio.active_id=None
    def generate_spawn(args,log,owner=None):
        assert args[0]=='generate' and Path(args[1])==child_directory/'model'
        result=Path(args[args.index('--result')+1]); write_json(result,{'text':'ok'})
        return FakeProcess()
    monkeypatch.setattr(studio,'spawn',generate_spawn)
    assert studio.generate(child_id,{'messages':[{'role':'user','content':'test'}]})['text']=='ok'
    assert studio.wait_for_deletion_cleanup(deleted['transaction_id'])


def test_deleting_child_does_not_change_parent(tmp_path):
    studio=parent_with_children(tmp_path,count=1)
    result=studio.delete_run(CHILD_IDS[0])
    assert studio.path('runs',PARENT_ID).is_dir()
    parent=read_json(studio.path('runs',PARENT_ID)/'state.json')
    assert parent['name']=='Parent Model'
    assert not (studio.path('runs',PARENT_ID)/'lineage.json').exists()
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])


def test_parent_and_children_sharing_dataset_preserves_live_reference(tmp_path):
    studio=parent_with_children(tmp_path,count=1,shared_dataset=True)
    result=studio.delete_run(PARENT_ID)
    assert result['dataset_shared'] and not result['deleted_dataset']
    assert studio.path('datasets',DATASET_ID).is_dir()
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])


def test_dependent_already_committed_for_deletion_is_not_live_lineage_or_dataset_reference(tmp_path):
    studio=parent_with_children(tmp_path,count=1,shared_dataset=True)
    child_transaction=studio.prepare_deletion(studio.plan_run_deletion(CHILD_IDS[0]))
    child_transaction=studio.commit_deletion(child_transaction)
    child_transaction=studio.stage_deletion_targets(child_transaction)
    assert child_transaction['status']=='staged' and not studio.path('runs',CHILD_IDS[0]).exists()
    parent_result=studio.delete_run(PARENT_ID)
    assert parent_result['dependent_runs']==0 and not parent_result['dataset_shared']
    assert not studio.path('datasets',DATASET_ID).exists()
    assert studio.wait_for_deletion_cleanup(parent_result['transaction_id'])
    assert studio.retry_deletion_cleanup(child_transaction['transaction_id'])
    assert studio.wait_for_deletion_cleanup(child_transaction['transaction_id'])


def test_committed_dependent_is_not_live_even_if_an_inconsistent_directory_remains(tmp_path):
    studio=parent_with_children(tmp_path,count=1,shared_dataset=True)
    child_transaction=studio.prepare_deletion(studio.plan_run_deletion(CHILD_IDS[0]))
    child_transaction={**child_transaction,'status':'committed',
        'committed_at':child_transaction['created_at']}
    write_transaction(tmp_path,child_transaction)
    assert studio.path('runs',CHILD_IDS[0]).is_dir()
    parent_result=studio.delete_run(PARENT_ID)
    assert parent_result['dependent_runs']==0 and not parent_result['dataset_shared']
    assert not studio.path('datasets',DATASET_ID).exists()


def test_corrupt_live_state_preserves_dataset_without_inventing_lineage(tmp_path):
    studio=parent_with_children(tmp_path,count=0)
    corrupt=studio.path('runs','f'*16); corrupt.mkdir()
    (corrupt/'state.json').write_text('{broken',encoding='utf-8')
    result=studio.delete_run(PARENT_ID)
    assert result['dataset_shared'] and not result['deleted_dataset']
    assert studio.path('datasets',DATASET_ID).is_dir()
