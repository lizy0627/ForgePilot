from pathlib import Path
import shutil
import sys
import threading
import time

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import studio as studio_module
from deletion import read_receipt,read_transaction
from studio import Studio,create_app
from studio_io import write_json


RUN_ID='a'*16
DATASET_ID='d'*16


def staged_transaction(root):
    studio=Studio(root)
    dataset=studio.path('datasets',DATASET_ID); dataset.mkdir()
    write_json(dataset/'data.json',{'id':DATASET_ID,'name':'dataset'})
    run=studio.path('runs',RUN_ID); run.mkdir()
    write_json(run/'state.json',{'id':RUN_ID,'dataset_id':DATASET_ID,'created':'2026',
        'status':'completed','step':5,'total_steps':5})
    transaction=studio.prepare_deletion(studio.plan_run_deletion(RUN_ID))
    transaction=studio.commit_deletion(transaction)
    transaction=studio.stage_deletion_targets(transaction)
    assert transaction['status']=='staged'
    return studio,transaction['transaction_id']


def test_blocked_background_rmtree_does_not_block_state_api(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    entered=threading.Event(); release=threading.Event(); real_rmtree=shutil.rmtree
    def blocked(path,*args,**kwargs):
        entered.set(); assert release.wait(5)
        return real_rmtree(path,*args,**kwargs)
    monkeypatch.setattr(studio_module.shutil,'rmtree',blocked)
    assert studio._schedule_cleanup(transaction_id)
    assert entered.wait(2)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        finished=threading.Event(); responses=[]
        def request_state():
            responses.append(client.get('/api/state')); finished.set()
        caller=threading.Thread(target=request_state,daemon=True); caller.start()
        assert finished.wait(.5)
        assert responses[0].status_code==200
    release.set()
    assert studio.wait_for_deletion_cleanup(transaction_id)


def test_transaction_has_at_most_one_cleanup_task(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    entered=threading.Event(); release=threading.Event(); calls=[]; real_rmtree=shutil.rmtree
    def blocked(path,*args,**kwargs):
        calls.append(Path(path)); entered.set(); assert release.wait(5)
        return real_rmtree(path,*args,**kwargs)
    monkeypatch.setattr(studio_module.shutil,'rmtree',blocked)
    assert studio._schedule_cleanup(transaction_id)
    assert entered.wait(2)
    assert not studio._schedule_cleanup(transaction_id)
    assert not studio.retry_deletion_cleanup(transaction_id)
    assert len(calls)==1
    release.set(); assert studio.wait_for_deletion_cleanup(transaction_id)


def test_permission_error_exhausts_finite_retries_then_manual_retry_succeeds(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    real_rmtree=shutil.rmtree; calls=[]
    monkeypatch.setattr(studio_module,'CLEANUP_RETRY_DELAYS',(0,0))
    def denied(path,*_args,**_kwargs):
        calls.append(Path(path)); raise PermissionError(13,'sensitive path omitted')
    monkeypatch.setattr(studio_module.shutil,'rmtree',denied)
    assert studio._schedule_cleanup(transaction_id)
    assert studio.wait_for_deletion_cleanup(transaction_id)
    transaction=read_transaction(tmp_path,transaction_id)
    assert transaction['status']=='cleanup_pending'
    assert transaction['cleanup_attempts']==studio_module.CLEANUP_MAX_ATTEMPTS==3
    assert transaction['last_error']=='permission_denied' and len(calls)==3
    monkeypatch.setattr(studio_module.shutil,'rmtree',real_rmtree)
    assert studio.retry_deletion_cleanup(transaction_id)
    assert studio.wait_for_deletion_cleanup(transaction_id)
    assert read_receipt(tmp_path,RUN_ID)['status']=='cleaned'


def test_close_is_bounded_and_restart_continues_pending_cleanup(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    entered=threading.Event(); release=threading.Event(); real_rmtree=shutil.rmtree
    monkeypatch.setattr(studio_module,'CLEANUP_CLOSE_TIMEOUT',.05)
    def blocked_then_fail(_path,*_args,**_kwargs):
        entered.set(); assert release.wait(5); raise PermissionError(13,'in use')
    monkeypatch.setattr(studio_module.shutil,'rmtree',blocked_then_fail)
    assert studio._schedule_cleanup(transaction_id)
    assert entered.wait(2)
    started=time.monotonic(); studio.close(); elapsed=time.monotonic()-started
    assert elapsed<.5
    assert read_transaction(tmp_path,transaction_id)['status']=='cleanup_pending'
    release.set(); assert studio.wait_for_deletion_cleanup(transaction_id)
    monkeypatch.setattr(studio_module.shutil,'rmtree',real_rmtree)
    restarted=Studio(tmp_path); restarted.recover()
    assert restarted.wait_for_deletion_cleanup(transaction_id)
    assert read_receipt(tmp_path,RUN_ID)['status']=='cleaned'


def test_receipt_is_minimal_and_supports_idempotent_repeat(tmp_path):
    studio,transaction_id=staged_transaction(tmp_path)
    assert studio._schedule_cleanup(transaction_id)
    assert studio.wait_for_deletion_cleanup(transaction_id)
    receipt_path=tmp_path/'deletion-journal'/'receipts'/(RUN_ID+'.json')
    receipt=read_receipt(tmp_path,RUN_ID)
    assert receipt_path.is_file()
    assert set(receipt)=={'version','transaction_id','run_id','status','dataset_shared',
        'deleted_dataset','deleted_requests','dependent_runs','committed_at','cleaned_at'}
    assert not (tmp_path/'deletion-journal'/(transaction_id+'.json')).exists()
    repeated=studio.delete_run(RUN_ID)
    assert repeated['transaction_id']==transaction_id and repeated['status']=='cleaned'


def test_cleanup_retry_only_removes_transaction_trash_not_live_directories(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    live=studio.path('runs','b'*16); live.mkdir(); marker=live/'keep.txt'; marker.write_text('keep')
    real_rmtree=shutil.rmtree; removed=[]
    def checked(path,*args,**kwargs):
        removed.append(Path(path))
        assert Path(path)==tmp_path/'trash'/transaction_id
        return real_rmtree(path,*args,**kwargs)
    monkeypatch.setattr(studio_module.shutil,'rmtree',checked)
    assert studio.retry_deletion_cleanup(transaction_id)
    assert studio.wait_for_deletion_cleanup(transaction_id)
    assert removed==[tmp_path/'trash'/transaction_id]
    assert marker.read_text()=='keep'


def test_startup_recovery_performs_only_one_cleanup_attempt(tmp_path,monkeypatch):
    studio,transaction_id=staged_transaction(tmp_path)
    calls=[]
    def denied(path,*_args,**_kwargs):
        calls.append(Path(path)); raise PermissionError(13,'denied')
    monkeypatch.setattr(studio_module.shutil,'rmtree',denied)
    restarted=Studio(tmp_path); restarted.recover()
    assert restarted.wait_for_deletion_cleanup(transaction_id)
    transaction=read_transaction(tmp_path,transaction_id)
    assert calls==[tmp_path/'trash'/transaction_id]
    assert transaction['status']=='cleanup_pending' and transaction['cleanup_attempts']==1
