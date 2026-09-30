import io
import json
from pathlib import Path
import sys
import zipfile

import pytest
import torch
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio import Studio,create_app,lock_storage
from studio_data import demo_files,parse_files,split_documents
from studio_io import read_json,write_json
from studio_model import LanguageModel,ModelConfig


def test_model_is_causal_and_gradients_are_finite():
    torch.set_num_threads(2)
    torch.manual_seed(7)
    cfg=ModelConfig(vocab_size=300,layers=2)
    model=LanguageModel(cfg).eval()
    assert sum(p.numel() for p in model.parameters())==cfg.parameter_count()
    tokens=torch.randint(0,300,(1,12)); changed=tokens.clone(); changed[:,6:]=(changed[:,6:]+1)%300
    torch.testing.assert_close(model(tokens)[:,:6],model(changed)[:,:6],atol=1e-6,rtol=1e-6)
    model.train()
    loss=torch.nn.functional.cross_entropy(model(tokens[:,:-1]).flatten(0,1),tokens[:,1:].flatten())
    loss.backward()
    assert torch.isfinite(loss) and all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters())
    with pytest.raises(ValueError): ModelConfig(kv_heads=0).validate()


def test_dedup_and_source_split_have_no_overlap():
    source=demo_files()[0]['text']
    dataset=parse_files([{'name':f'{i}.txt','text':source.replace('示例',f'资料{i}')} for i in range(4)])
    split=split_documents(dataset['documents'])
    hashes=[{d['hash'] for d in split[k]} for k in ('train','validation','test')]
    sources=[{d['source'] for d in split[k]} for k in ('train','validation','test')]
    for values in (hashes,sources):
        assert all(not values[a]&values[b] for a,b in ((0,1),(0,2),(1,2)))
    duplicate=parse_files(demo_files()+demo_files())
    assert duplicate['count']==96 and duplicate['duplicates_removed']==96


@pytest.mark.parametrize('files',[None,[None],[{'name':'a.exe','text':'text'}],
    [{'name':'a.jsonl','text':'[]'}],demo_files()+demo_files('qa'),[{'name':'a.txt','text':'too little'}]])
def test_invalid_data_is_rejected(files):
    with pytest.raises(ValueError): parse_files(files)


def test_api_boundary_export_and_restart(tmp_path):
    studio=Studio(tmp_path)
    with TestClient(create_app(studio),base_url='http://127.0.0.1') as client:
        assert client.get('/api/state').json()['active_run'] is None
        assert client.post('/api/demo',json={}).status_code==403
        headers={'X-Training-Token':studio.csrf}
        assert client.post('/api/demo',headers={**headers,'Origin':'https://example.org'},json={}).status_code==403
        assert client.get('/health',headers={'Host':'untrusted.example'}).status_code==400
        assert client.post('/api/datasets',headers=headers,json={}).status_code==400
        data=client.post('/api/demo',headers=headers,json={}).json()
        assert data['count']==96
        assert client.post('/api/runs',headers=headers,json={'dataset_id':data['id'],'steps':1}).status_code==400
        assert client.post('/v1/chat/completions',json={}).status_code==401
        assert client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+studio.api_key},json={'tools':[{}]}).status_code==422
        assert client.get('/api/connection/aaaaaaaaaaaaaaaa').status_code==403
        with pytest.raises(ValueError): studio.path('runs','../../agent')
        identity='aaaaaaaaaaaaaaaa'; folder=studio.path('runs',identity); model=folder/'model'; model.mkdir(parents=True)
        write_json(folder/'state.json',{'id':identity,'created':'2026','status':'running','step':2,'total_steps':5})
        for name in ('model.safetensors','config.json','tokenizer.json','training.json','MODEL_CARD.md'):
            (model/name).write_text('test fixture',encoding='utf-8')
        studio.recover()
        assert read_json(folder/'state.json')['status']=='interrupted'
        response=client.get('/api/export/'+identity)
        assert response.status_code==200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist())=={'model.safetensors','config.json','tokenizer.json','training.json','MODEL_CARD.md','studio_model.py'}
        assert studio.snapshot()['active_run'] is None


def test_second_instance_does_not_own_storage(tmp_path):
    first=lock_storage(tmp_path)
    try:
        assert first is not None
        second=lock_storage(tmp_path)
        assert second is None
    finally: first.close()
