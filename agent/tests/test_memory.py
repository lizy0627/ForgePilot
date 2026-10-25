"""Capture actual model inputs; all SQLite/history/projects are isolated fixtures."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import shutil
import sqlite3
from threading import Event
from uuid import uuid4

import httpx
import pytest

from conftest import reply,ScriptedLLM
from forgepilot.api import create_app
from forgepilot.chat_service import ChatService,READ_TOOLS
from forgepilot.memory_context import SUMMARY_SYSTEM,project_identity
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION
from forgepilot.task_service import TaskService
from test_conversations import send
from test_api import messages,until
from test_conversation_deletion import eventually


class Capture:
    def __init__(self): self.requests=[]; self.summaries=[]; self.closed=0
    def model(self):
        capture=self
        class Model:
            async def chat(self,messages,tools=None):
                data=deepcopy(messages)
                if messages[0]['content']==SUMMARY_SYSTEM:
                    assert tools is None
                    source=json.loads(messages[1]['content']); capture.summaries.append(source)
                    return reply('历史目标摘要，覆盖消息 '+str(source['messages'][-1]['message_id'])+'；代码事实需重新读取验证。')
                capture.requests.append((data,deepcopy(tools)))
                return reply('已收到当前问题。')
            async def aclose(self): capture.closed+=1
        return Model()


@asynccontextmanager
async def memory_api(path=':memory:',*,capture=None,model=None,summary_model=None,rg=None,git=None,task_factory=None):
    capture=capture or Capture(); store=SqliteEventStore(path)
    app=create_app(task_factory or (lambda trace:pytest.fail('Question must not create a task')),store=store,
        chat_factory=lambda:ChatService(model() if model else capture.model(),rg_executable=rg,git_executable=git),
        summary_factory=(lambda:ChatService(summary_model(),rg_executable=rg,git_executable=git)) if summary_model else None)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
                yield c,app,store,capture
    finally: store.close()


def seed(store,cid,n=10):
    for i in range(n):
        rid=str(uuid4()); store.begin_conversation_request(cid,rid,'chat',f'原文 {i} '+('文'*1000),'')
        store.finish_conversation_request(rid,'completed',f'回答 {i} '+('答'*1000))


def background(capture,index=-1):
    row=next((m for m in capture.requests[index][0] if m['content'].startswith('历史背景资料（')),None)
    return json.loads(row['content'].split('\n',1)[1]) if row else {}


async def add(c,content,scope='global',project=None,source=None,message=None):
    r=await c.post('/api/v1/memories',json=dict(content=content,scope=scope,repository_path=str(project) if project else None,
        source_conversation_id=source,source_message_id=message))
    assert r.status_code==201,r.text
    return r.json()


async def wait_summary(app,cid):
    # Request polling can observe the durable answer just before the host creates the background task.
    await asyncio.sleep(.1)
    await eventually(lambda:cid not in app.state.conversation_host._summary_tasks)


@pytest.mark.asyncio
async def test_manual_scopes_edit_delete_normalized_project_and_restart(tmp_path,cpp_repo,rg_exe,git_exe,monkeypatch):
    other=tmp_path/'另一个项目'; shutil.copytree(cpp_repo,other); path=tmp_path/'mem.sqlite3'
    monkeypatch.chdir(cpp_repo.parent)
    assert project_identity(cpp_repo.name)==project_identity(str(cpp_repo/'..'/cpp_repo.name).upper())
    async with memory_api(path,rg=rg_exe,git=git_exe) as (c,app,store,cap):
        a=(await c.post('/api/v1/conversations',json={'repository_path':str(cpp_repo)})).json()['conversation_id']
        b=(await c.post('/api/v1/conversations',json={'repository_path':str(other)})).json()['conversation_id']
        plain=(await c.post('/api/v1/conversations',json={})).json()['conversation_id']
        global_m=await add(c,'全局偏好标记：默认中文回答')
        project_m=await add(c,'项目甲偏好标记：保留现有测试','project',cpp_repo.name)
        assert project_m['project_key']==project_identity(str(cpp_repo))
        await send(c,a,'本轮明确要求英文，先列步骤。',tool_mode='project')
        assert len(background(cap)['用户手动保存的偏好'])==2
        assert {s.name for s in cap.requests[-1][1]}==READ_TOOLS
        assert cap.requests[-1][0][-1]['content']=='本轮明确要求英文，先列步骤。'
        assert '本轮明确指令优先' in cap.requests[-1][0][0]['content']
        await send(c,b,'只问另一个项目'); assert len(background(cap)['用户手动保存的偏好'])==1
        await send(c,plain,'无项目'); assert len(background(cap)['用户手动保存的偏好'])==1
        update=dict(content='项目甲更新偏好标记：先给结论',scope='project',repository_path=str(cpp_repo),revision=1)
        changed=await c.patch('/api/v1/memories/'+project_m['memory_id'],json=update); assert changed.status_code==200
        assert (await c.patch('/api/v1/memories/'+project_m['memory_id'],json=update)).status_code==409
        assert (await c.delete('/api/v1/memories/'+global_m['memory_id'])).status_code==200
        assert (await c.delete('/api/v1/memories/'+global_m['memory_id'])).status_code==200
        await send(c,a,'继续'); payload=json.dumps(cap.requests[-1],ensure_ascii=False,default=lambda value:value.model_dump())
        assert '项目甲更新偏好标记' in payload and '全局偏好标记' not in payload and '项目甲偏好标记' not in payload
        await send(c,b,'继续'); assert not background(cap)
    async with memory_api(path,rg=rg_exe,git=git_exe) as (c,app,store,cap):
        assert store.memory_list(str(cpp_repo))['items'][0]['revision']==2
        await send(c,a,'重启后继续'); assert '项目甲更新偏好标记' in json.dumps(background(cap),ensure_ascii=False)
        await c.delete('/api/v1/memories/'+project_m['memory_id']); await send(c,a,'删除后继续'); assert not background(cap)


@pytest.mark.asyncio
async def test_memory_injection_has_count_length_budget():
    async with memory_api() as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']
        for n in range(15): await add(c,f'偏好{n} '+('文'*490))
        await send(c,cid,'现在回答')
        selected=background(cap)['用户手动保存的偏好']
        assert len(selected)==12 and sum(len(x['content']) for x in selected)<=6000
        assert store.memory_list()['total']==15
        assert selected[0]['content'].startswith('偏好14')


@pytest.mark.asyncio
@pytest.mark.parametrize('secret',['api_key=abc123','password: hidden','password is hidden','Cookie: sessionvalue','我的密码是 不可保存','API Key: plain_secret',
    'sk-proj-123456789abcdefghijkl','-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'])
async def test_reject_credentials_without_persisting_or_echoing(secret):
    async with memory_api() as (c,app,store,cap):
        response=await c.post('/api/v1/memories',json={'content':secret})
        assert response.status_code==422 and response.json()['error']['code']=='memory_secret'
        assert secret not in response.text and store.memory_list()['total']==0


@pytest.mark.asyncio
async def test_summary_contiguous_coverage_recent_originals_and_incremental_restart(tmp_path):
    path=tmp_path/'summary.sqlite3'
    async with memory_api(path) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,16)
        original=store.conversation_detail(cid)['messages']
        await send(c,cid,'新一轮')
        # The foreground answer used bounded originals immediately; it did not wait for or inject the new summary.
        assert '对话摘要' not in background(cap)
        await wait_summary(app,cid)
        state=store.summary_state(cid); cursor=state['summary']['through_message_id']
        covered=[x for s in cap.summaries for x in s['messages']]
        expected=[m['message_id'] for m in original if m['message_id']<=cursor]
        assert [m['message_id'] for m in covered]==expected
        assert len(expected)==len(set(expected)) and len(original)+2-len(expected)>=6
        await send(c,cid,'使用已有摘要')
        assert background(cap)['对话摘要']['through_message_id']==cursor
        recent=[m for m in cap.requests[-1][0][1:-1] if not m['content'].startswith('历史背景资料（')]
        current=store.conversation_detail(cid)['messages'][:-2]
        assert recent==[{'role':m['role'],'content':m['content']} for m in current if m['message_id']>cursor]
        assert store.conversation_detail(cid)['messages'][:len(original)]==original
        assert all(len(json.dumps(s,ensure_ascii=False))<=48000 for s in cap.summaries)
    async with memory_api(path) as (c,app,store,cap):
        assert store.summary_state(cid)['summary']['through_message_id']==cursor
        seed(store,cid,13); await send(c,cid,'继续摘要')
        await wait_summary(app,cid)
        assert cap.summaries and all(m['message_id']>cursor for s in cap.summaries for m in s['messages'])
        assert cap.summaries[0]['previous_summary']
        assert store.summary_state(cid)['summary']['through_message_id']>cursor


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['exception','tool','oversize','secret'])
async def test_summary_failure_keeps_original_messages_and_answers(failure):
    class Model:
        async def chat(self,messages,tools=None):
            if messages[0]['content']==SUMMARY_SYSTEM:
                if failure=='exception': raise RuntimeError('offline summary failure')
                if failure=='tool':
                    from conftest import call
                    return reply(calls=[call('write_file','{}')])
                return reply('长'*4001 if failure=='oversize' else '密码是 secret_never_save')
            return reply('正常继续回答')
        async def aclose(self): pass
    async with memory_api(model=Model) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        before=store.conversation_detail(cid)['messages']; result=await send(c,cid,'继续提问')
        assert result['status']=='completed' and '摘要生成未完成' not in result['context_notice']
        assert store.conversation_detail(cid)['messages'][-1]['content']=='正常继续回答'
        await wait_summary(app,cid)
        assert store.summary_state(cid)['summary'] is None
        assert store.conversation_detail(cid)['messages'][:len(before)]==before


@pytest.mark.asyncio
async def test_foreground_first_delta_does_not_wait_for_background_summary():
    started=Event(); release=Event()
    class Delayed:
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('立即回答')
            started.set(); await asyncio.to_thread(release.wait,10); return reply('后台摘要')
        async def aclose(self): pass
    async with memory_api(model=Delayed) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15); rid=str(uuid4())
        result=await send(c,cid,'不能等待摘要',rid=rid)
        assert result['status']=='completed'
        await eventually(started.is_set)
        metrics=app.state.conversation_host._streams[rid].metrics
        before=metrics.snapshot()
        assert before['request_to_first_safe_delta_ms']>=0 and before['request_total_ms']>=0
        assert before['summary_started']==1 and before['summary_ms']==-1
        await asyncio.sleep(.2); release.set(); await wait_summary(app,cid)
        after=metrics.snapshot()
        assert after['summary_result']=='committed' and after['summary_ms']>=150
        assert after['request_to_first_safe_delta_ms']<after['summary_ms']


@pytest.mark.asyncio
async def test_same_conversation_deduplicates_running_summary():
    started=Event(); release=Event(); values={'calls':0,'active':0,'maximum':0}
    class Delayed:
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('前台完成')
            values['calls']+=1; values['active']+=1
            values['maximum']=max(values['maximum'],values['active']); started.set()
            try: await asyncio.to_thread(release.wait,10)
            finally: values['active']-=1
            return reply('唯一后台摘要')
        async def aclose(self): pass
    async with memory_api(model=Delayed) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        await send(c,cid,'第一轮'); await eventually(started.is_set)
        second=await send(c,cid,'摘要运行时的第二轮')
        assert second['status']=='completed' and len(app.state.conversation_host._summary_tasks)==1
        assert values=={'calls':1,'active':1,'maximum':1}
        release.set(); await wait_summary(app,cid)
        assert values['calls']==1 and values['maximum']==1 and store.summary_state(cid)['summary']


@pytest.mark.asyncio
async def test_background_summaries_are_globally_single_concurrency():
    first_started=Event(); release_first=Event(); values={'calls':0,'active':0,'maximum':0}
    class DelayedFirst:
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('前台完成')
            values['calls']+=1; values['active']+=1
            values['maximum']=max(values['maximum'],values['active'])
            try:
                if values['calls']==1:
                    first_started.set(); await asyncio.to_thread(release_first.wait,10)
                return reply('后台摘要')
            finally: values['active']-=1
        async def aclose(self): pass
    async with memory_api(model=DelayedFirst) as (c,app,store,cap):
        conversations=[store.create_conversation()['conversation_id'] for _ in range(2)]
        for cid in conversations: seed(store,cid,15)
        await asyncio.gather(*(send(c,cid,'并发前台') for cid in conversations))
        await eventually(first_started.is_set); await asyncio.sleep(.1)
        assert values['calls']==1 and values['maximum']==1
        release_first.set()
        await asyncio.gather(*(wait_summary(app,cid) for cid in conversations))
        assert values['calls']==2 and values['maximum']==1


@pytest.mark.asyncio
async def test_failed_background_summary_retries_after_next_answer():
    attempts={'value':0}
    class Retry:
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('回答保持成功')
            attempts['value']+=1
            if attempts['value']==1: raise RuntimeError('summary fixture failure')
            return reply('重试成功的摘要')
        async def aclose(self): pass
    async with memory_api(model=Retry) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        first=await send(c,cid,'第一次'); await wait_summary(app,cid)
        assert first['status']=='completed' and store.summary_state(cid)['summary'] is None
        second=await send(c,cid,'第二次'); await wait_summary(app,cid)
        assert second['status']=='completed' and attempts['value']==2
        assert store.summary_state(cid)['summary']['content']=='重试成功的摘要'


@pytest.mark.asyncio
async def test_chat_and_background_summary_use_separate_model_factories():
    calls={'chat':0,'summary':0}
    class Chat:
        async def chat(self,messages,tools=None): calls['chat']+=1; return reply('聊天模型回答')
        async def aclose(self): pass
    class Summary:
        async def chat(self,messages,tools=None):
            assert messages[0]['content']==SUMMARY_SYSTEM
            calls['summary']+=1
            return reply('摘要模型结果')
        async def aclose(self): pass
    async with memory_api(model=Chat,summary_model=Summary) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        result=await send(c,cid,'使用聊天模型')
        assert result['status']=='completed' and calls=={'chat':1,'summary':0}
        await wait_summary(app,cid)
        assert calls=={'chat':1,'summary':1}
        assert store.summary_state(cid)['summary']['content']=='摘要模型结果'


def test_summary_commit_cas_does_not_require_running_request():
    store=SqliteEventStore(':memory:')
    try:
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        plan=store.summary_plan(cid); assert plan and plan['through_message_id']>0
        assert not store._db.execute("SELECT 1 FROM conversation_requests WHERE status='running'").fetchone()
        assert store.commit_summary(plan,'终态请求之后提交的摘要')
        seed(store,cid,15); stale=store.summary_plan(cid); assert stale
        state=store.summary_state(cid)
        store.edit_summary(cid,state['revision'],content='用户编辑后的摘要')
        assert not store.commit_summary(stale,'不得覆盖用户编辑')
    finally: store.close()


@pytest.mark.asyncio
async def test_service_close_cancels_background_summary_without_writeback(tmp_path):
    path=tmp_path/'close-summary.sqlite3'; started=Event(); closed=Event()
    class Hanging:
        def __init__(self): self.summary=False
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('回答已持久化')
            self.summary=True; started.set(); await asyncio.Future()
        async def aclose(self):
            if self.summary: closed.set()
    async with memory_api(path,model=Hanging) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        assert (await send(c,cid,'关闭服务'))['status']=='completed'
        await eventually(started.is_set)
    assert closed.is_set()
    reopened=SqliteEventStore(path)
    try: assert reopened.summary_state(cid)['summary'] is None
    finally: reopened.close()


@pytest.mark.asyncio
async def test_delete_source_removes_summary_and_memories_only(tmp_path):
    path=tmp_path/'source.sqlite3'
    async with memory_api(path) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; keep=store.create_conversation()['conversation_id']; seed(store,cid,15)
        await send(c,cid,'生成摘要'); await wait_summary(app,cid); assert store.summary_state(cid)['summary']
        mid=store.conversation_detail(cid)['messages'][0]['message_id']
        await add(c,'来源偏好标记',source=cid,message=mid)
        await add(c,'无来源保留标记')
        bad=await c.post('/api/v1/memories',json=dict(content='错误来源',source_conversation_id=keep,source_message_id=mid)); assert bad.status_code==422
        assert (await c.delete('/api/v1/conversations/'+cid)).status_code==200
        assert (await c.get('/api/v1/conversations/'+cid+'/context')).status_code==404
        assert store._db.execute('SELECT COUNT(*) FROM conversation_summaries').fetchone()[0]==0
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
        await send(c,keep,'继续'); payload=json.dumps(cap.requests[-1],ensure_ascii=False)
        assert '来源偏好标记' not in payload and '无来源保留标记' in payload and '对话摘要' not in background(cap)
    async with memory_api(path) as (c,app,store,cap):
        assert store.memory_list()['total']==1; await send(c,keep,'重启后继续')
        assert '来源偏好标记' not in json.dumps(cap.requests[-1],ensure_ascii=False)


@pytest.mark.asyncio
async def test_summary_delete_during_generation_fences_writeback_and_disables_regeneration():
    started=Event(); release=Event(); capture=Capture()
    class Delayed:
        async def chat(self,messages,tools=None):
            if messages[0]['content']==SUMMARY_SYSTEM:
                started.set(); await asyncio.to_thread(release.wait,10); return reply('迟到的摘要')
            capture.requests.append((deepcopy(messages),tools)); return reply('继续回答')
        async def aclose(self): pass
    async with memory_api(model=Delayed) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        req=asyncio.create_task(send(c,cid,'摘要期间删除摘要'))
        try:
            await eventually(started.is_set)
            result=await c.delete(f'/api/v1/conversations/{cid}/context?revision=0'); assert result.status_code==200
            release.set(); await req
            state=store.summary_state(cid); assert state['summary'] is None and not state['enabled']
            assert '对话摘要' not in background(capture)
            started.clear(); release.clear(); await send(c,cid,'再次提问'); assert not started.is_set()
            assert (await c.patch(f'/api/v1/conversations/{cid}/context',json=dict(revision=0,enabled=True))).status_code==409
            assert (await c.patch(f'/api/v1/conversations/{cid}/context',json=dict(revision=state['revision'],enabled=True))).status_code==200
            await send(c,cid,'重新生成'); await eventually(started.is_set)
            assert store.summary_state(cid)['summary'] is None
            release.set(); await wait_summary(app,cid); assert store.summary_state(cid)['summary']
            state=store.summary_state(cid)
            edited=await c.patch(f'/api/v1/conversations/{cid}/context',json=dict(revision=state['revision'],content='手工修正后的摘要'))
            assert edited.status_code==200; await send(c,cid,'使用修改后摘要')
            assert background(capture)['对话摘要']['content']=='手工修正后的摘要'
        finally: release.set()


@pytest.mark.asyncio
async def test_manual_summary_edit_cancels_stale_background_writeback():
    started=Event(); release=Event(); state={'delay':False}
    class Editable:
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('前台回答')
            if state['delay']:
                started.set(); await asyncio.to_thread(release.wait,10)
            return reply('模型生成但可能迟到的摘要')
        async def aclose(self): pass
    async with memory_api(model=Editable) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15)
        await send(c,cid,'先生成摘要'); await wait_summary(app,cid)
        before=store.summary_state(cid); cursor=before['summary']['through_message_id']
        seed(store,cid,13); state['delay']=True
        await send(c,cid,'触发增量摘要'); await eventually(started.is_set)
        edited=await c.patch(f'/api/v1/conversations/{cid}/context',json={
            'revision':before['revision'],'content':'用户手工摘要'})
        assert edited.status_code==200
        release.set(); await wait_summary(app,cid)
        final=store.summary_state(cid)
        assert final['summary']['content']=='用户手工摘要'
        assert final['summary']['through_message_id']==cursor


@pytest.mark.asyncio
async def test_delete_conversation_waits_summary_cleanup_and_no_resurrection():
    started=Event(); closing=Event(); release=Event()
    class LateSummary:
        def __init__(self): self.summarizing=False
        async def chat(self,messages,tools=None):
            if messages[0]['content']!=SUMMARY_SYSTEM: return reply('前台回答已完成')
            self.summarizing=True; started.set()
            try: await asyncio.Future()
            except asyncio.CancelledError: return reply('删除之后不得写回的摘要')
        async def aclose(self):
            if self.summarizing: closing.set(); await asyncio.to_thread(release.wait,10)
    async with memory_api(model=LateSummary) as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15); await add(c,'待删来源记忆',source=cid)
        await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=str(uuid4()),prompt='触发摘要'))
        await eventually(started.is_set)
        deleting=asyncio.create_task(c.delete(f'/api/v1/conversations/{cid}?stop_active=true'))
        try:
            await eventually(closing.is_set); assert not deleting.done()
            assert (await c.post('/api/v1/memories',json=dict(content='迟到来源保存',source_conversation_id=cid))).status_code==409
            release.set(); assert (await deleting).status_code==200
            for table in ('conversations','conversation_messages','conversation_requests','conversation_summaries','memories'):
                assert store._db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
            assert not app.state.conversation_host._answers
        finally: release.set()


def version_two(path):
    store=SqliteEventStore(path); cid=store.create_conversation()['conversation_id']; seed(store,cid,2); store.close()
    with sqlite3.connect(path) as db:
        for table in ("security_events", "validation_runs", "workspace_records", "checkpoint_intents", "checkpoints",
                      "graph_node_results", "graph_node_states", "graph_run_inputs", "graph_events",
                      "model_calls", "approvals", "artifact_relations", "artifacts",
                      "task_node_attempts", "task_graph_runs"):
            db.execute(f'DROP TABLE "{table}"')
        db.execute('DROP TABLE rag_index_runs')
        db.execute('DROP TABLE rag_files')
        db.execute('DROP TABLE mcp_servers')
        db.execute('DROP TABLE mcp_migrations')
        db.execute('ALTER TABLE conversation_requests DROP COLUMN mcp_calls_json')
        db.execute('DROP TABLE memories'); db.execute('DROP TABLE conversation_summaries')
        db.execute('ALTER TABLE conversations DROP COLUMN summary_revision'); db.execute('ALTER TABLE conversations DROP COLUMN summary_enabled')
        db.execute('PRAGMA user_version=2')
    return cid


def test_v2_backup_migration_preserves_history_and_rollback(tmp_path,monkeypatch):
    path=tmp_path/'v2.sqlite3'; cid=version_two(path)
    store=SqliteEventStore(path)
    try:
        assert len(store.conversation_detail(cid)['messages'])==4
        assert store._db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION
        backups=list(tmp_path.glob('*.v2-backup-*.sqlite3')); assert len(backups)==1
        with sqlite3.connect(backups[0]) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==2
            assert db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0]==4
    finally: store.close()
    store=SqliteEventStore(path); store.close(); assert len(list(tmp_path.glob('*.v2-backup-*.sqlite3')))==1
    failed=tmp_path/'failed.sqlite3'; version_two(failed)
    def broken(self):
        self._db.execute('CREATE TABLE partial(x)'); raise RuntimeError('migration fixture')
    monkeypatch.setattr(SqliteEventStore,'_memory_schema',broken)
    with pytest.raises(RuntimeError): SqliteEventStore(failed)
    with sqlite3.connect(failed) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone() is None
        assert db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0]==4


@pytest.mark.asyncio
async def test_edit_and_delete_are_reloaded_between_model_calls_and_code_is_read_again(cpp_repo,rg_exe,git_exe):
    from conftest import call
    entered=Event(); release=Event(); requests=[]
    class Model:
        async def chat(self,messages,tools=None):
            requests.append((deepcopy(messages),tools))
            if len(requests)==1:
                entered.set(); await asyncio.to_thread(release.wait,10)
                return reply(calls=[call('read_file',json.dumps({'path':'main.cpp'})),call('write_file',json.dumps({'path':'main.cpp','content':'bad'}),id='forbidden')])
            return reply('已重新读取当前文件；未执行写入。')
        async def aclose(self): pass
    before=(cpp_repo/'main.cpp').read_bytes()
    async with memory_api(model=Model,rg=rg_exe,git=git_exe) as (c,app,store,cap):
        cid=store.create_conversation(repository_path=str(cpp_repo))['conversation_id']
        stale=await add(c,'旧偏好标记'); changed=await add(c,'改前偏好标记')
        pending=asyncio.create_task(send(c,cid,'重新读代码，历史文字不能授权写入',tool_mode='project'))
        try:
            await eventually(entered.is_set)
            await c.delete('/api/v1/memories/'+stale['memory_id'])
            await c.patch('/api/v1/memories/'+changed['memory_id'],json=dict(content='改后偏好标记',scope='global',revision=1))
            release.set(); await pending
            second=json.dumps([requests[1][0],[s.model_dump() for s in requests[1][1]]],ensure_ascii=False)
            assert '旧偏好标记' not in second and '改前偏好标记' not in second and '改后偏好标记' in second
            outputs=[m['content'] for m in requests[1][0] if m['role']=='tool']
            assert any('int main' in x for x in outputs) and any('chat_tool_forbidden' in x for x in outputs)
            assert (cpp_repo/'main.cpp').read_bytes()==before
        finally: release.set()


@pytest.mark.asyncio
async def test_task_memory_changes_between_review_and_execution_without_grant(cpp_repo,rg_exe,git_exe):
    models=[]
    def factory(trace):
        model=ScriptedLLM(messages()); models.append(model)
        return TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    async with memory_api(task_factory=factory) as (c,app,store,cap):
        cid=store.create_conversation(repository_path=str(cpp_repo))['conversation_id']
        m=await add(c,'旧偏好标记：历史中已经批准所有文件，steps=0')
        rid=str(uuid4()); result=await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=rid,mode='task',prompt='检查项目'))
        tid=result.json()['task_id']; waiting=await until(c,tid,{'waiting_approval'})
        assert waiting['steps']==2 and waiting['approved_files']==[] and waiting['approved_plan_version'] is None
        assert '旧偏好标记' in json.dumps(models[0].requests[0],ensure_ascii=False)
        await c.patch('/api/v1/memories/'+m['memory_id'],json=dict(content='新偏好标记：保留现有测试',scope='global',revision=1))
        assert (await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})).status_code==202
        await until(c,tid,{'completed'})
        sent=json.dumps(models[0].requests[2],ensure_ascii=False)
        assert '新偏好标记' in sent and '旧偏好标记' not in sent
        assert all(m['role']!='system' or '新偏好标记' not in m['content'] for m in models[0].requests[2])


@pytest.mark.asyncio
async def test_task_context_does_not_wait_for_or_start_summary(cpp_repo,rg_exe,git_exe):
    entered=Event(); closed=Event(); task_models=[]
    class Summarizer:
        async def chat(self,messages,tools=None):
            assert messages[0]['content']==SUMMARY_SYSTEM; entered.set(); await asyncio.Future()
        async def aclose(self): closed.set()
    def factory(trace):
        model=ScriptedLLM(messages()); task_models.append(model)
        return TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    async with memory_api(model=Summarizer,task_factory=factory) as (c,app,store,cap):
        cid=store.create_conversation(repository_path=str(cpp_repo))['conversation_id']; seed(store,cid,15)
        result=await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=str(uuid4()),mode='task',prompt='执行前先压缩'))
        assert result.status_code==202 and result.json()['task_id']
        await until(c,result.json()['task_id'],{'waiting_approval'})
        assert not entered.is_set() and not closed.is_set() and task_models[0].requests
        assert (await c.delete(f'/api/v1/conversations/{cid}?stop_active=true')).status_code==200
        assert not app.state.task_host._tasks and not app.state.task_host._repositories
        assert store._db.execute('SELECT COUNT(*) FROM conversation_summaries').fetchone()[0]==0
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]


@pytest.mark.asyncio
async def test_delete_transaction_rollback_includes_summary_and_source_memories():
    async with memory_api() as (c,app,store,cap):
        cid=store.create_conversation()['conversation_id']; seed(store,cid,15); await send(c,cid,'摘要')
        await add(c,'源记忆',source=cid)
        original=store.conversation_detail(cid); summary=store.summary_state(cid); memories=store.memory_list()
        store._db.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON conversations BEGIN SELECT RAISE(ABORT,'rollback fixture'); END")
        with pytest.raises(sqlite3.IntegrityError): store.delete_conversation(cid)
        assert store.conversation_detail(cid)==original and store.summary_state(cid)==summary and store.memory_list()==memories
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
        store._db.execute('DROP TRIGGER refuse_delete'); store.delete_conversation(cid)
        assert store.memory_list()['total']==0
