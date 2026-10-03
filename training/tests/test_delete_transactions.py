import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import studio as studio_module
from deletion import read_receipt,read_transaction,receipt_run_ids,transaction_ids,write_transaction
from studio import Studio
from studio_io import write_json


RUN_ID='a'*16
DATASET_ID='d'*16
REQUEST_IDS=('1'*32,'2'*32)


class InjectedCrash(BaseException):
    pass


def build_deletion_fixture(root,shared=False,dependent=False):
    studio=Studio(root)
    dataset=studio.path('datasets',DATASET_ID); dataset.mkdir()
    write_json(dataset/'data.json',{'id':DATASET_ID,'name':'dataset'})
    imported=studio.path('imports',DATASET_ID); imported.mkdir()
    write_json(imported/'state.json',{'status':'ready'})
    run=studio.path('runs',RUN_ID); (run/'model').mkdir(parents=True)
    write_json(run/'state.json',{'id':RUN_ID,'dataset_id':DATASET_ID,'created':'2026',
        'status':'completed','step':5,'total_steps':5})
    (run/'model/model.safetensors').write_text('weights',encoding='utf-8')
    if shared or dependent:
        other_id='b'*16; other=studio.path('runs',other_id); other.mkdir()
        write_json(other/'state.json',{'id':other_id,'dataset_id':DATASET_ID if shared else 'e'*16,
            'base_run':RUN_ID if dependent else None,'created':'2026','status':'completed',
            'step':5,'total_steps':5})
    for request_id in REQUEST_IDS:
        request=studio.root/'requests'/request_id; request.mkdir()
        write_json(request/'input.json',{'model_id':RUN_ID,'messages':[]})
    return studio


def crash_and_recover(root,point):
    studio=build_deletion_fixture(root)
    studio.deletion_fault=lambda current: (_ for _ in ()).throw(InjectedCrash(point)) if current==point else None
    with pytest.raises(InjectedCrash,match=point): studio.delete_run(RUN_ID)
    identities=transaction_ids(root)
    assert len(identities)==1
    transaction_id=identities[0]
    recovered=Studio(root)
    recovered.recover()
    assert recovered.wait_for_deletion_cleanup(transaction_id)
    receipt=read_receipt(root,RUN_ID)
    assert receipt['transaction_id']==transaction_id
    assert receipt['status']=='cleaned'
    assert not (root/'runs'/RUN_ID).exists()
    assert not (root/'datasets'/DATASET_ID).exists()
    assert not (root/'imports'/DATASET_ID).exists()
    assert all(not (root/'requests'/request_id).exists() for request_id in REQUEST_IDS)
    assert not (root/'trash'/transaction_id).exists()
    assert transaction_ids(root)==[]
    assert receipt_run_ids(root)==[RUN_ID]


def test_recover_after_prepared_journal(tmp_path):
    crash_and_recover(tmp_path,'after_prepared')


def test_recover_after_run_rename_before_committed_journal(tmp_path):
    crash_and_recover(tmp_path,'after_run_move')


def test_recover_after_committed_journal(tmp_path):
    crash_and_recover(tmp_path,'after_committed')


def test_recover_after_dataset_move(tmp_path):
    crash_and_recover(tmp_path,'after_dataset_move')


def test_recover_after_import_move(tmp_path):
    crash_and_recover(tmp_path,'after_import_move')


def test_recover_after_first_request_move(tmp_path):
    crash_and_recover(tmp_path,'after_first_request_move')


def test_recover_after_staged_journal(tmp_path):
    crash_and_recover(tmp_path,'after_staged')


def test_cleanup_failure_stays_logically_deleted_and_recovers(tmp_path,monkeypatch):
    studio=build_deletion_fixture(tmp_path)
    real_rmtree=shutil.rmtree
    monkeypatch.setattr(studio_module,'CLEANUP_RETRY_DELAYS',(0,0))
    def fail_cleanup(path,*args,**kwargs):
        assert not studio.lock._is_owned()
        if Path(path).parent==tmp_path/'trash': raise PermissionError(13,'in use')
        return real_rmtree(path,*args,**kwargs)
    monkeypatch.setattr(studio_module.shutil,'rmtree',fail_cleanup)
    result=studio.delete_run(RUN_ID)
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])
    result=studio.deletion_status(result['transaction_id'])
    assert result['status']=='cleanup_pending' and result['cleanup_pending']
    assert not (tmp_path/'runs'/RUN_ID).exists()
    transaction_id=result['transaction_id']
    assert (tmp_path/'trash'/transaction_id/'run').is_dir()
    studio.close()
    monkeypatch.setattr(studio_module.shutil,'rmtree',real_rmtree)
    recovered=Studio(tmp_path); recovered.recover()
    assert recovered.wait_for_deletion_cleanup(transaction_id)
    assert read_receipt(tmp_path,RUN_ID)['status']=='cleaned'


def test_shared_dataset_is_retained_and_dependents_are_reported(tmp_path):
    studio=build_deletion_fixture(tmp_path,shared=True,dependent=True)
    result=studio.delete_run(RUN_ID)
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])
    result=studio.deletion_status(result['transaction_id'])
    assert result['status']=='cleaned' and result['dataset_shared']
    assert not result['deleted_dataset'] and result['dependent_runs']==1
    assert (tmp_path/'datasets'/DATASET_ID).is_dir()
    assert (tmp_path/'imports'/DATASET_ID).is_dir()


def test_last_dataset_reference_is_staged_and_cleaned(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    result=studio.delete_run(RUN_ID)
    assert studio.wait_for_deletion_cleanup(result['transaction_id'])
    result=studio.deletion_status(result['transaction_id'])
    assert result['status']=='cleaned' and result['deleted_dataset']
    assert not (tmp_path/'datasets'/DATASET_ID).exists()
    assert not (tmp_path/'imports'/DATASET_ID).exists()


def test_corrupt_other_run_state_conservatively_retains_dataset(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    corrupt=studio.path('runs','b'*16); corrupt.mkdir()
    (corrupt/'state.json').write_text('{broken',encoding='utf-8')
    result=studio.delete_run(RUN_ID)
    assert result['dataset_shared'] and not result['deleted_dataset']
    assert (tmp_path/'datasets'/DATASET_ID).is_dir()


def test_repeated_delete_returns_same_transaction_and_receipt(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    first=studio.delete_run(RUN_ID)
    assert studio.wait_for_deletion_cleanup(first['transaction_id'])
    second=studio.delete_run(RUN_ID)
    assert first['transaction_id']==second['transaction_id']
    assert second['status']=='cleaned'


def test_receipt_makes_repeat_idempotent_even_if_old_journal_is_corrupt(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    first=studio.delete_run(RUN_ID)
    assert studio.wait_for_deletion_cleanup(first['transaction_id'])
    journal=tmp_path/'deletion-journal'/(first['transaction_id']+'.json')
    journal.write_text('{broken',encoding='utf-8')
    second=studio.delete_run(RUN_ID)
    assert second['transaction_id']==first['transaction_id'] and second['status']=='cleaned'


def test_corrupt_transaction_is_never_used_to_delete_live_data(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    transaction=studio.prepare_deletion(studio.plan_run_deletion(RUN_ID))
    journal=tmp_path/'deletion-journal'/(transaction['transaction_id']+'.json')
    journal.write_text('{broken',encoding='utf-8')
    recovered=Studio(tmp_path); recovered.recover()
    assert (tmp_path/'runs'/RUN_ID).is_dir()
    assert (tmp_path/'datasets'/DATASET_ID).is_dir()
    assert (tmp_path/'trash'/transaction['transaction_id']).is_dir()


def test_active_inference_for_run_is_rejected(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    studio.inference=object(); studio.inference_id=RUN_ID
    with pytest.raises(ValueError,match='试用'): studio.delete_run(RUN_ID)
    assert (tmp_path/'runs'/RUN_ID).is_dir()


@pytest.mark.parametrize('failure',[PermissionError(13,'access denied'),OSError(32,'file in use')])
def test_commit_move_errors_leave_run_live_and_prepared(tmp_path,monkeypatch,failure):
    studio=build_deletion_fixture(tmp_path)
    monkeypatch.setattr(studio_module,'atomic_move_within_storage',
        lambda *_args,**_kwargs: (_ for _ in ()).throw(failure))
    with pytest.raises(type(failure)): studio.delete_run(RUN_ID)
    assert (tmp_path/'runs'/RUN_ID).is_dir()
    transaction=read_transaction(tmp_path,transaction_ids(tmp_path)[0])
    assert transaction['status']=='prepared' and transaction['last_error']


def test_source_and_target_both_exist_requires_manual_intervention(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    plan=studio.plan_run_deletion(RUN_ID); transaction=studio.prepare_deletion(plan)
    target=tmp_path/'trash'/transaction['transaction_id']/'run'
    shutil.copytree(tmp_path/'runs'/RUN_ID,target)
    recovered=Studio(tmp_path); recovered.recover()
    transaction=read_transaction(tmp_path,transaction['transaction_id'])
    assert transaction['status']=='manual_intervention_required'
    assert (tmp_path/'runs'/RUN_ID).is_dir() and target.is_dir()


def test_source_and_target_both_missing_requires_manual_intervention(tmp_path):
    studio=build_deletion_fixture(tmp_path)
    plan=studio.plan_run_deletion(RUN_ID); transaction=studio.prepare_deletion(plan)
    shutil.rmtree(tmp_path/'runs'/RUN_ID)
    recovered=Studio(tmp_path); recovered.recover()
    transaction=read_transaction(tmp_path,transaction['transaction_id'])
    assert transaction['status']=='manual_intervention_required'
    assert not (tmp_path/'trash'/transaction['transaction_id']/'run').exists()
