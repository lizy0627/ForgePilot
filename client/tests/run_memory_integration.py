"""Real Qt/HTTP/SQLite with captured offline model inputs and an isolated project."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Thread
import time
from uuid import uuid4

import uvicorn
from forgepilot.api import create_app
from forgepilot.chat_service import ChatService
from forgepilot.llm import ChatReply,AssistantMessage,ToolCall,FunctionCall
from forgepilot.memory_context import SUMMARY_SYSTEM
from forgepilot.process import executable
from forgepilot.sqlite_store import SqliteEventStore


def run(root,output_root):
    output=output_root.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]); output.mkdir(parents=True)
    print('OFFLINE MEMORY evidence: '+str(output),flush=True)
    database=output/'isolated-memory.sqlite3'; captures=[]
    class Model:
        async def chat(self,messages,tools=None):
            captures.append(dict(messages=deepcopy(messages),tools=[t.model_dump() for t in tools] if tools else None))
            await asyncio.sleep(.05)
            if messages[0]['content']==SUMMARY_SYSTEM:
                text='离线摘要：历史讨论过项目目标；旧代码事实需重新读取验证。'
            else:
                prompt=next(m['content'] for m in reversed(messages) if m['role']=='user')
                if '请检查当前文件 fixture' in prompt and messages[-1]['role']!='tool':
                    return ChatReply(message=AssistantMessage(role='assistant',tool_calls=[ToolCall(id='read-current',type='function',function=FunctionCall(name='read_file',arguments='{"path":"main.cpp"}'))]),usage=None)
                text='离线回答：已按当前请求读取背景；代码事实以本轮文件读取为准。'
            return ChatReply(message=AssistantMessage(role='assistant',content=text),usage=None)
        async def aclose(self): pass
    rg=executable(os.environ.get('FORGEPILOT_RG'),'rg'); git=executable(os.environ.get('FORGEPILOT_GIT'),'git')
    with tempfile.TemporaryDirectory(prefix='ForgePilot 记忆验收 ') as temp:
        project=Path(temp); subprocess.run([git,'init','-q',temp],check=True)
        source=b'int main(){return 42;}\n'; (project/'main.cpp').write_bytes(source)
        store=SqliteEventStore(database); cid=store.create_conversation(title='记忆来源对话')['conversation_id']
        for i in range(16):
            rid=str(uuid4()); store.begin_conversation_request(cid,rid,'chat',f'早期目标 {i} '+('文'*1000),'')
            store.finish_conversation_request(rid,'completed',f'历史分析 {i} '+('答'*1000))
        store.close()
        for restart in (False,True):
            store=SqliteEventStore(database)
            def no_task(trace): raise AssertionError('Memory UI must not grant task execution')
            app=create_app(no_task,store=store,chat_factory=lambda:ChatService(Model(),rg_executable=rg,git_executable=git))
            sock=socket.socket(); sock.bind(('127.0.0.1',0))
            server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='warning',timeout_graceful_shutdown=2))
            thread=Thread(target=lambda:server.run(sockets=[sock])); thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError('Backend failed to start')
                    time.sleep(.01)
                env=os.environ.copy(); env.update(FORGEPILOT_INTEGRATION_URL=f'http://127.0.0.1:{sock.getsockname()[1]}',
                    FORGEPILOT_INTEGRATION_QML=str(root/'client/Main.qml'),FORGEPILOT_INTEGRATION_OUTPUT=str(output),
                    FORGEPILOT_INTEGRATION_REPOSITORY=temp,FORGEPILOT_MEMORY_SOURCE=cid)
                if restart: env['FORGEPILOT_MEMORY_RESTART']='1'
                report=output/('memory-restart.txt' if restart else 'memory-ui.txt')
                result=subprocess.run([str(root/'client/build/Release/ForgePilotMemoryTests.exe'),'-v1','-o',str(report)+',txt'],env=env,capture_output=True,timeout=120)
                detail=(report.read_bytes() if report.exists() else b'')+result.stdout+result.stderr
                print(detail.decode('utf-8',errors='replace'),flush=True)
                (output/(report.stem+'.log')).write_bytes(detail)
                if result.returncode: return result.returncode
                assert store.conversation_page()['total']==1 and not store.list()
                assert store.memory_list()['total']==1 and store.memory_list()['items'][0]['source_conversation_id'] is None
                assert store._db.execute('SELECT COUNT(*) FROM conversation_summaries').fetchone()[0]==0
                assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
                assert (project/'main.cpp').read_bytes()==source
            finally:
                server.should_exit=True; thread.join(20); sock.close()
                if thread.is_alive(): raise RuntimeError('Backend failed to stop')
                store.close()
        answers=[x for x in captures if x['messages'][0]['content']!=SUMMARY_SYSTEM]
        def background(record):
            item=next((m for m in record['messages'] if m['content'].startswith('历史背景资料（')),None)
            return json.loads(item['content'].split('\n',1)[1]) if item else {}
        def for_prompt(text): return [x for x in answers if next(m['content'] for m in reversed(x['messages']) if m['role']=='user')==text][-1]
        assert len(background(for_prompt('请检查当前文件 fixture'))['用户手动保存的偏好'])==2
        assert any('42' in m['content'] for m in for_prompt('请检查当前文件 fixture')['messages'] if m['role']=='tool')
        assert background(for_prompt('使用编辑后的摘要 fixture'))['对话摘要']['content']=='手工纠正的摘要：旧代码结论须重新读取确认'
        assert '对话摘要' not in background(for_prompt('删除摘要后继续 fixture'))
        assert '对话摘要' in background(for_prompt('重新生成摘要 fixture'))
        for prompt in ('新对话仅有全局记忆 fixture','重启后的偏好确认'):
            data=background(for_prompt(prompt)); assert len(data['用户手动保存的偏好'])==1 and '对话摘要' not in data
            assert data['用户手动保存的偏好'][0]['content']=='保留的全局偏好：先给结论'
        (output/'actual-model-requests.json').write_text(json.dumps(captures,ensure_ascii=False,indent=2),encoding='utf-8')
        print('REAL QT + HTTP + SQLITE: memory CRUD, summary edit/delete/enable, source deletion, restart, captured model inputs and unchanged project passed',flush=True)
    return 0
