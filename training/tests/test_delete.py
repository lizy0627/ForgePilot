from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio import Studio,create_app
from studio_io import write_json


def test_delete_run_removes_artifacts_and_only_unshared_dataset(tmp_path):
    studio=Studio(tmp_path)
    dataset_id='dddddddddddddddd'
    dataset=studio.path('datasets',dataset_id); dataset.mkdir()
    write_json(dataset/'data.json',{'id':dataset_id,'name':'shared'})
    imported=studio.path('imports',dataset_id); imported.mkdir()
    write_json(imported/'state.json',{'status':'ready'})
    first_id='aaaaaaaaaaaaaaaa'; second_id='bbbbbbbbbbbbbbbb'
    for identity,name in ((first_id,'first'),(second_id,'second')):
        folder=studio.path('runs',identity); (folder/'model').mkdir(parents=True)
        write_json(folder/'state.json',{'id':identity,'name':name,'created':'2026','status':'completed',
            'step':5,'total_steps':5,'dataset_id':dataset_id})
        (folder/'model/model.safetensors').write_text('weights',encoding='utf-8')
    request=studio.root/'requests'/('1'*32); request.mkdir()
    write_json(request/'input.json',{'model_id':first_id,'messages':[]})

    shared=studio.delete_run(first_id)
    assert studio.wait_for_deletion_cleanup(shared['transaction_id'])
    assert shared['dataset_shared'] and not shared['deleted_dataset']
    assert not studio.path('runs',first_id).exists() and dataset.exists()
    assert not request.exists()

    final=studio.delete_run(second_id)
    assert studio.wait_for_deletion_cleanup(final['transaction_id'])
    assert final['deleted_dataset'] and not final['dataset_shared']
    assert not dataset.exists() and not imported.exists()
    assert studio.snapshot()['runs']==[] and studio.snapshot()['datasets']==[]


def test_delete_run_rejects_active_training_and_requires_csrf(tmp_path):
    studio=Studio(tmp_path); identity='aaaaaaaaaaaaaaaa'
    dataset_id='dddddddddddddddd'; dataset=studio.path('datasets',dataset_id); dataset.mkdir()
    write_json(dataset/'data.json',{'id':dataset_id,'name':'dataset'})
    folder=studio.path('runs',identity); folder.mkdir()
    write_json(folder/'state.json',{'id':identity,'dataset_id':dataset_id,'created':'2026','status':'paused','step':2,'total_steps':5})
    studio.active_id=identity
    with pytest.raises(ValueError,match='暂停或停止'): studio.delete_run(identity)
    studio.active_id=None
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        assert client.delete('/api/runs/'+identity).status_code==403
        assert client.post('/api/runs/'+identity+'/delete',headers={'X-Training-Token':studio.csrf},json={}).status_code==404
        response=client.delete('/api/runs/'+identity,headers={'X-Training-Token':studio.csrf})
        assert response.status_code in (200,202) and response.json()['run_id']==identity
        assert studio.wait_for_deletion_cleanup(response.json()['deletion_id'])
