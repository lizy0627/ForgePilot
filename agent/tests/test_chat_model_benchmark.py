import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest


ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/'scripts/benchmark_chat_models.py'


def test_chat_model_benchmark_defaults_to_offline_and_reports_required_distributions(tmp_path):
    output=tmp_path/'model-latency.json'
    result=subprocess.run([sys.executable,str(SCRIPT),'--repeats','1','--output',str(output)],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text(encoding='utf-8'))
    assert report['external_model_enabled']==0 and report['fixed_prompt_count']==3
    assert len(report['models'])==2
    for model in report['models']:
        assert model['samples']==3 and model['success_rate']==1
        for field in ('first_token_ms','complete_ms','response_chars'):
            assert model[field]['p50']>=0 and model[field]['p95']>=model[field]['p50']
            assert model[field]['sample_count']==3


def test_ordinary_chat_benchmark_default_remains_offline(tmp_path):
    output=tmp_path/'ordinary-chat.json'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/benchmark_chat.py'),
                           '--samples','1','--warmup','0','--first-token-delay-ms','0',
                           '--chunk-delay-ms','0','--output',str(output)],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text(encoding='utf-8'))
    assert report['external_model_enabled']==0
    assert report['benchmark']=='ordinary_chat_offline'
    assert report['metrics']['request_total_ms']['sample_count']==1


def test_candidate_models_cannot_trigger_network_without_explicit_real_switch():
    result=subprocess.run([sys.executable,str(SCRIPT),'--model','candidate'],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and '--real-model' in result.stderr


def test_real_switch_requires_environment_and_candidates(monkeypatch):
    spec=importlib.util.spec_from_file_location('chat_model_benchmark',SCRIPT)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.delenv('FORGEPILOT_BASE_URL',raising=False)
    monkeypatch.delenv('FORGEPILOT_API_KEY',raising=False)
    args=type('Args',(),{'real_model':True,'models':['candidate'],'repeats':1,'timeout':1})()
    import pytest
    with pytest.raises(SystemExit,match='requires'):
        import asyncio
        asyncio.run(module.run(args))


@pytest.mark.asyncio
async def test_candidate_transport_uses_gateway_stream_contract_and_closes_fake_provider():
    spec=importlib.util.spec_from_file_location('chat_model_benchmark_fake',SCRIPT)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    requests=[]

    def respond(request):
        requests.append(json.loads(request.content))
        chunks = [
            {'choices': [{'index': 0, 'delta': {'content': 'OK'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        ]
        body = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
        return httpx.Response(200, headers={'Content-Type':'text/event-stream'}, content=body.encode())

    class Transport(httpx.MockTransport):
        closed=False

        async def aclose(self):
            self.closed=True
            await super().aclose()

    transport=Transport(respond)
    rows=await module.real_candidate('https://fake.test/v1','offline-key','fake-model',1,1,
                                     transport=transport)
    assert transport.closed
    assert len(rows)==len(requests)==len(module.FIXED_PROMPTS)
    assert all(row['success'] and row['response_chars']==2 for row in rows)
    assert all(request['model']=='fake-model' and request['stream'] for request in requests)
    assert all('max_tokens' not in request and 'max_completion_tokens' not in request for request in requests)
