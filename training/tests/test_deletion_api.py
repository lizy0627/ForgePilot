from pathlib import Path
import shutil
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import studio as studio_module
from studio import Studio,create_app
from studio_io import write_json


RUN_ID='a'*16
DATASET_ID='d'*16


def api_fixture(root):
    studio=Studio(root)
    dataset=studio.path('datasets',DATASET_ID); dataset.mkdir()
    write_json(dataset/'data.json',{'id':DATASET_ID,'name':'dataset','kind':'text','created':'2026','documents':[]})
    run=studio.path('runs',RUN_ID); run.mkdir()
    write_json(run/'state.json',{'id':RUN_ID,'name':'model','dataset_id':DATASET_ID,
        'dataset_name':'dataset','created':'2026','status':'completed','step':5,'total_steps':5,
        'mode':'pretrain','base_run':None,'metrics':[]})
    return studio


def headers(studio,origin=None):
    value={'X-Training-Token':studio.csrf}
    if origin is not None: value['Origin']=origin
    return value


def test_delete_api_csrf_origin_and_old_post_removed(tmp_path):
    studio=api_fixture(tmp_path)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        assert client.delete('/api/runs/'+RUN_ID).status_code==403
        assert client.delete('/api/runs/'+RUN_ID,headers=headers(studio,'https://example.invalid')).status_code==403
        assert client.post('/api/runs/'+RUN_ID+'/delete',headers=headers(studio),json={}).status_code==404
        response=client.delete('/api/runs/'+RUN_ID,headers=headers(studio,'http://127.0.0.1'))
        assert response.status_code in (200,202)
        assert response.json()['logical_status']=='deleted'


def test_delete_returns_202_then_status_and_retry_reach_cleaned_200(tmp_path,monkeypatch):
    studio=api_fixture(tmp_path); real_schedule=studio._schedule_cleanup
    monkeypatch.setattr(studio,'_schedule_cleanup',lambda *_args,**_kwargs: True)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        response=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        value=response.json(); deletion_id=value['deletion_id']
        assert response.status_code==202
        assert value['logical_status']=='deleted' and value['cleanup_status']=='pending'
        assert client.get('/api/deletions/'+deletion_id).json()==value
        state=client.get('/api/state').json()
        assert not any(run['id']==RUN_ID for run in state['runs'])
        assert value in state['deletions']
        monkeypatch.setattr(studio,'_schedule_cleanup',real_schedule)
        retry=client.post('/api/deletions/'+deletion_id+'/retry',headers=headers(studio),json={})
        assert retry.status_code in (200,202)
        assert studio.wait_for_deletion_cleanup(deletion_id)
        cleaned=client.get('/api/deletions/'+deletion_id)
        assert cleaned.status_code==200 and cleaned.json()['cleanup_status']=='cleaned'
        cleaned_retry=client.post('/api/deletions/'+deletion_id+'/retry',headers=headers(studio),json={})
        assert cleaned_retry.status_code==200 and cleaned_retry.json()['cleanup_status']=='cleaned'
        repeated=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        assert repeated.status_code==200 and repeated.json()['deletion_id']==deletion_id


def test_response_loss_retry_is_idempotent(tmp_path,monkeypatch):
    studio=api_fixture(tmp_path)
    monkeypatch.setattr(studio,'_schedule_cleanup',lambda *_args,**_kwargs: True)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        first=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        second=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        assert first.status_code==second.status_code==202
        assert first.json()==second.json()
        assert len(studio.deletion_summaries())==1


def test_invalid_deletion_id_and_retry_permissions(tmp_path):
    studio=api_fixture(tmp_path)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        assert client.get('/api/deletions/not-an-id').status_code==400
        assert client.post('/api/deletions/'+'1'*32+'/retry',json={}).status_code==403
        assert client.post('/api/deletions/'+'1'*32+'/retry',headers=headers(studio),json={}).status_code==404
        assert client.post('/api/deletions/'+'1'*32+'/retry',headers=headers(studio,'https://bad.invalid'),json={}).status_code==403


def test_manual_intervention_response_is_sanitized(tmp_path):
    studio=api_fixture(tmp_path)
    transaction=studio.prepare_deletion(studio.plan_run_deletion(RUN_ID))
    target=tmp_path/'trash'/transaction['transaction_id']/'run'
    shutil.copytree(studio.path('runs',RUN_ID),target)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        response=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        value=response.json()
        assert response.status_code==409
        assert value['logical_status']=='not_deleted'
        assert value['cleanup_status']=='manual_intervention_required'
        assert 'last_error' not in value and str(tmp_path) not in response.text
        retry=client.post('/api/deletions/'+transaction['transaction_id']+'/retry',headers=headers(studio),json={})
        assert retry.status_code==409 and retry.json()['cleanup_status']=='manual_intervention_required'


def test_precommit_locked_conflict_and_storage_failure_are_safe(tmp_path,monkeypatch):
    studio=api_fixture(tmp_path); studio.active_id=RUN_ID
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        locked=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        assert locked.status_code==423 and studio.path('runs',RUN_ID).is_dir()
        studio.active_id=None
        state_path=studio.path('runs',RUN_ID)/'state.json'; state_path.write_text('{broken',encoding='utf-8')
        conflict=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        assert conflict.status_code==409 and studio.path('runs',RUN_ID).is_dir()
        write_json(state_path,{'id':RUN_ID,'name':'model','dataset_id':DATASET_ID,'created':'2026',
            'status':'completed','step':5,'total_steps':5})
        monkeypatch.setattr(studio_module,'write_transaction',
            lambda *_args,**_kwargs: (_ for _ in ()).throw(PermissionError('C:\\private\\secret')))
        unavailable=client.delete('/api/runs/'+RUN_ID,headers=headers(studio))
        assert unavailable.status_code==503 and studio.path('runs',RUN_ID).is_dir()
        assert 'private' not in unavailable.text and 'secret' not in unavailable.text
