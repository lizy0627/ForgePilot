"""Offline UI chain: settings -> ChatService -> HTTP search/extract -> durable sources."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4


def run(root,output_root,*,live_model=False):
    sys.path.insert(0,str(root/'agent/tests'))
    from fixtures.mcp_http_peer import HttpPeer
    from test_mcp_api import serving
    from conftest import call,reply
    from forgepilot.chat_service import ChatService
    from forgepilot.mcp_config import TAVILY_ENDPOINT,create_mcp_connection
    from forgepilot.tools.mcp_tools import tool_alias
    from forgepilot.llm import OpenAICompatibleClient
    from fastapi import Request

    output=output_root.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    output.mkdir(parents=True); print(('REAL MODEL + MCP FIXTURE' if live_model else 'OFFLINE MCP')+' runtime UI evidence: '+str(output),flush=True)
    model_settings={k:os.environ.get('FORGEPILOT_'+k.upper(),'') for k in ('base_url','api_key','model')} if live_model else {}
    if live_model and not all(model_settings.values()):
        raise RuntimeError('Explicit project model configuration is required; no credential search is performed')

    async def exercise():
        peer=await HttpPeer(auth='bearer',sse=True).start(); secret='fixture-runtime-ui-'+uuid4().hex
        peer.expected_secret=secret; clients=[]; models=[]; state={'delay_records':False}; evidence={'delayed_records':0}
        for tool,name in zip(peer.tools,('tavily-search','tavily-extract')):
            tool['name']=name; tool.pop('outputSchema')
            tool['description']='Search public fixture' if name=='tavily-search' else 'Extract public fixture'
        peer.tools[1]['inputSchema']={'type':'object','properties':{'urls':{'type':'array',
            'items':{'type':'string'},'minItems':1,'maxItems':1}},'required':['urls'],'additionalProperties':False}
        normal={'content':[{'type':'text','text':json.dumps({'results':[
            {'content':'Qt 官方文档摘录。'+secret+('中文正文\"\\' * 6000),
             'title':'Qt 6 文档','url':'https://doc.qt.io/qt-6/'},
            {'title':'非法链接','url':'javascript:alert(1)'}]},ensure_ascii=False)}]}
        extracted={'content':[{'type':'text','text':'正文提取 fixture。'+('中文正文\"\\' * 6000)+'\nDetailed Results:\n\nURL: https://doc.qt.io/qt-6/\n'
            'Raw Content: <html><p>Qt 正文提取 fixture。</p><script>discard this script</script></html>'+secret}]}
        peer.call_results={'tavily-search':normal,'tavily-extract':extracted}

        class Model:
            def __init__(self): self.calls=0; self.closed=False; models.append(self)
            async def chat(self,messages,tools=None):
                self.calls+=1
                await asyncio.sleep(.2)
                if self.calls==1:
                    assert len(tools)==2 and all(set(t.model_dump())=={'name','description','input_schema'} for t in tools)
                    selected=next(t for t in tools if t.description.startswith('Search public fixture'))
                    return reply(calls=[call(selected.name,'{"query":"Qt documentation"}')])
                result=json.loads(messages[-1]['content'])
                assert secret not in messages[-1]['content']
                if self.calls==2 and result['success']:
                    assert 'TRUNCATED' in result['output']
                    assert json.loads(result['output'])['sources']==[{'title':'Qt 6 文档','url':'https://doc.qt.io/qt-6/'}]
                    selected=next(t for t in tools if t.description.startswith('Extract public fixture'))
                    return reply(calls=[call(selected.name,'{"urls":["https://doc.qt.io/qt-6/"]}')])
                if self.calls==3:
                    assert result['success'] and '正文提取 fixture' in messages[-1]['content']
                    assert 'TRUNCATED' in result['output']
                    evidence['long_results_sources_retained']=True
                return reply('根据实际查询结果：Qt 文档 https://doc.qt.io/qt-6/' if result['success'] else '查询失败，本次没有可引用的搜索结果。')
            async def aclose(self): self.closed=True

        def chat_factory():
            return ChatService(OpenAICompatibleClient(**model_settings,timeout=90) if live_model else Model())

        class DelayedRecords:
            def __init__(self,app): self.app=app
            async def __call__(self,scope,receive,send):
                if not scope.get('path','').endswith('/mcp-tools') or not state['delay_records']:
                    return await self.app(scope,receive,send)
                state['delay_records']=False; messages=[]
                async def capture(message): messages.append(message)
                await self.app(scope,receive,capture); evidence['delayed_records']+=1
                await asyncio.sleep(.7)
                for message in messages: await send(message)

        def configure(app):
            app.add_middleware(DelayedRecords)
            def connection(config):
                assert config.endpoint==TAVILY_ENDPOINT
                c=create_mcp_connection(config.model_copy(update={'endpoint':peer.endpoint})); clients.append(c); return c
            app.state.task_host.mcp_service.connection_factory=connection
            @app.post('/fixture/control')
            async def control(request:Request):
                data=await request.json()
                if 'delay_records' in data: state['delay_records']=data['delay_records']
                for key in ('mode','fail_on'):
                    if key in data: setattr(peer,key,data[key])
                peer.call_result={'content':[{'type':'text','text':'offline failure'}],'isError':True} if peer.mode=='tool_error' else None
                return {'ok':True,'business_call_count':sum(r['body'].get('method')=='tools/call' for r in peer.records)}

        def qt(url,phase):
            env=os.environ.copy(); env.update(FORGEPILOT_INTEGRATION_URL=url,FORGEPILOT_MCP_PHASE=phase,
                FORGEPILOT_INTEGRATION_OUTPUT=str(output),FORGEPILOT_INTEGRATION_QML=str(root/'client/Main.qml'),
                FORGEPILOT_MCP_SECRET=secret,QT_QUICK_CONTROLS_STYLE='Basic')
            # The Qt client never receives the real model credential.
            env.pop('FORGEPILOT_API_KEY',None)
            report=output/(phase+'.txt')
            result=subprocess.run([str(root/'client/build/Release/ForgePilotMcpTests.exe'),'-v1','-o',str(report)+',txt'],
                                   env=env,capture_output=True,timeout=300 if live_model else 120)
            detail=(report.read_bytes() if report.exists() else b'')+result.stdout+result.stderr
            assert secret.encode() not in detail
            if live_model: assert model_settings['api_key'].encode() not in detail
            (output/(phase+'.log')).write_bytes(detail); print(detail.decode('utf-8',errors='replace'),flush=True)
            if result.returncode: raise RuntimeError('Qt MCP runtime failed: '+phase)
        try:
            with tempfile.TemporaryDirectory(prefix='ForgePilot MCP runtime ') as folder:
                with serving(Path(folder),configure=configure,chat_factory=chat_factory) as (url,app,store):
                    qt(url,'runtime-model' if live_model else 'runtime')
                    requests=store._db.execute('SELECT request_id,conversation_id,status,mcp_calls_json FROM conversation_requests ORDER BY created_at').fetchall()
                    assert len(requests)==(1 if live_model else 3)
                    assert [r['status'] for r in requests]==(['completed'] if live_model else ['completed','completed','cancelled'])
                    records=[json.loads(r['mcp_calls_json']) for r in requests]
                    assert [r[0]['status'] for r in records]==(['succeeded'] if live_model else ['succeeded','failed','cancelled'])
                    assert [r['tool_name'] for r in records[0]]==['tavily-search','tavily-extract']
                    assert records[0][1]['status']=='succeeded' and '正文提取 fixture' in records[0][1]['result_excerpt']
                    assert '<html>' not in records[0][1]['result_excerpt'] and 'discard this script' not in records[0][1]['result_excerpt']
                    assert records[0][1]['sources']==[{'title':'https://doc.qt.io/qt-6/','url':'https://doc.qt.io/qt-6/'}]
                    assert len(records[0][0]['sources'])==1
                    assert not store.list() and all(c.owner.done() and c.transport.closed for c in clients)
                    for file in Path(folder).glob('api.sqlite3*'):
                        if file.suffix!='.lock':
                            raw=file.read_bytes(); assert secret.encode() not in raw
                            if live_model: assert model_settings['api_key'].encode() not in raw
                    if live_model:
                        evidence.update(model=model_settings['model'],model_base_url=model_settings['base_url'],
                            timestamp=datetime.now(timezone.utc).isoformat(),calls=records[0],
                            answer=store.conversation_detail(requests[0]['conversation_id'])['messages'][-1]['content'],
                            real_model_verified=True,real_mcp_service_verified=False)
                count=len(peer.records)
                with serving(Path(folder),configure=configure,chat_factory=chat_factory) as (url,app,store):
                    qt(url,'runtime-model-restart' if live_model else 'runtime-restart')
                    assert not store._db.execute('SELECT * FROM conversation_requests').fetchall()
                    assert not store._db.execute('PRAGMA foreign_key_check').fetchall()
                assert len(peer.records)==count and all(m.closed for m in models)
                assert evidence['delayed_records']==(0 if live_model else 1)
                calls=[r for r in peer.records if r['body'].get('method')=='tools/call']; assert len(calls)==(2 if live_model else 4)
                assert calls[0]['body']['params']['name']=='tavily-search'
                if not live_model: assert calls[0]['body']['params']['arguments']=={'query':'Qt documentation'}
                assert calls[1]['body']['params']=={'name':'tavily-extract','arguments':{'urls':['https://doc.qt.io/qt-6/']}}
                evidence.update(business_calls=len(calls),search_and_extract=True,normal_failure_cancel=not live_model,plain_chat_without_repository=True,
                    sources_opened_via_http_link=True,restart_history_and_delete=True,credentials_redacted=True,
                    migration_version=5,real_service_verified=False)
                encoded=json.dumps(evidence,ensure_ascii=False,indent=2)
                assert secret not in encoded
                if live_model: assert model_settings['api_key'] not in encoded
                (output/'evidence.json').write_text(encoded,encoding='utf-8')
                print('REAL QML + ChatService + HTTP MCP + SQLite: runtime loop, sources, cancel, restart and delete passed',flush=True)
        except Exception as exc:
            if live_model:
                (output/'evidence.json').write_text(json.dumps(dict(real_model_verified=False,
                    real_mcp_service_verified=False,status='failed',error_type=type(exc).__name__,
                    timestamp=datetime.now(timezone.utc).isoformat(),model=model_settings['model']),indent=2),encoding='utf-8')
            raise
        finally: await peer.close()
    asyncio.run(exercise()); return 0
