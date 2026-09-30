"""Deletion is tested only with isolated SQLite, offline models and temporary repositories."""
import asyncio
from pathlib import Path
import sqlite3
from threading import Event
from uuid import uuid4

import httpx
import pytest

from conftest import ScriptedLLM, reply
from forgepilot.errors import ForgePilotError
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_service import TaskService
from test_api import messages, until
from test_api_network import serving
from test_conversations import api, send
from test_persistence import task
from test_build import build_repo,build_config
from conftest import call
from forgepilot.context import TaskState
from test_plan_feedback import hold_replanning, assert_released
from test_planning import contents


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "llm", "tool", "cleanup"])
async def test_delete_conversation_during_replanning_joins_owned_resources(cpp_repo, rg_exe, git_exe, monkeypatch, phase):
    entered, cancellation = Event(), Event()
    services = []
    class Model(ScriptedLLM):
        closed = False
        async def aclose(self): self.closed = True
    following = ([reply(calls=[call("read_file", '{"path":"main.cpp"}', "held-read")])] if phase == "tool"
                 else [reply("analysis"), messages()[1]])
    def factory(trace):
        service = TaskService(Model(messages()[:2] + following), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
        services.append(service)
        return service
    async with api(task_factory=factory) as (client, app, _, _):
        cid = (await client.post("/api/v1/conversations", json={"repository_path": str(cpp_repo)})).json()["conversation_id"]
        rid = str(uuid4())
        task_id = (await client.post(f"/api/v1/conversations/{cid}/requests", json={"request_id": rid, "mode": "task", "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        host = app.state.task_host
        before = contents(cpp_repo)
        async def prepare():
            entry = host._tasks[task_id]
            gates = await hold_replanning(monkeypatch, host, entry, services[0].llm, phase)
            async def signal():
                await gates[0].wait()
                entered.set()
            watcher = asyncio.create_task(signal())
            cancel = entry.service.cancel
            def signal_cancel():
                result = cancel()
                cancellation.set()
                return result
            monkeypatch.setattr(entry.service, "cancel", signal_cancel)
            return gates, watcher
        gates, watcher = await host._submit(prepare())
        assert (await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Revise"})).status_code == 202
        assert await asyncio.to_thread(entered.wait, 3)
        assert (await client.delete(f"/api/v1/conversations/{cid}")).status_code == 409
        deleting = asyncio.create_task(client.delete(f"/api/v1/conversations/{cid}?stop_active=true"))
        async def release(): gates[1].set()
        try:
            assert await asyncio.to_thread(cancellation.wait, 3)
            if phase in {"dispatch", "cleanup"}:
                assert not deleting.done() and not services[0].llm.closed
            await host._submit(release())
            assert (await asyncio.wait_for(deleting, 5)).status_code == 200
        finally:
            await host._submit(release())
        assert watcher.done()
        assert services[0].state == TaskState.CANCELLED and services[0].llm.closed
        assert_released(services[0], cpp_repo)
        assert not host._tasks and not host._repositories and not host.store._live
        assert counts(host.store) == dict.fromkeys(counts(host.store), 0)
        assert host.store._db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert contents(cpp_repo) == before
        if phase == "dispatch": assert len(services[0].llm.requests) == 2
        else: assert gates[2].is_set()
        assert (await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Late feedback"})).status_code == 404


async def eventually(predicate):
    async with asyncio.timeout(10):
        while not predicate(): await asyncio.sleep(.01)


def source_matches(path,content):
    try: return path.read_text()==content
    except PermissionError: return False  # A controlled Windows write may still own its exclusive handle.


def counts(store):
    return {t:store._db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in
        ('conversations','conversation_requests','conversation_messages','tasks','task_events','tool_calls')}


@pytest.mark.asyncio
async def test_delete_complete_graph_retry_restart_and_keep_files(tmp_path,cpp_repo,rg_exe,git_exe):
    path=tmp_path/'delete.sqlite3'
    factory=lambda trace: TaskService(ScriptedLLM(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    async with api(path,task_factory=factory) as (c,app,_,_):
        keep=(await c.post('/api/v1/conversations',json={'title':'保留'})).json()['conversation_id']
        await send(c,keep,'其他对话不得改变')
        saved=(await c.get(f'/api/v1/conversations/{keep}')).json()
        cid=(await c.post('/api/v1/conversations',json={'repository_path':str(cpp_repo)})).json()['conversation_id']
        task_ids=[]
        for _ in range(2):
            rid=str(uuid4())
            result=await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=rid,mode='task',prompt='修改'))
            tid=result.json()['task_id']; task_ids.append(tid)
            await until(c,tid,{'waiting_approval'})
            assert (await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})).status_code==202
            await until(c,tid,{'completed'})
            await eventually(lambda:tid not in app.state.task_host._tasks)
        preserved={p.relative_to(cpp_repo):p.read_bytes() for p in cpp_repo.rglob('*') if p.is_file()}
        store=app.state.task_host.store
        assert counts(store)['tool_calls']>0 and counts(store)['task_events']>0
        expected={'conversation_id':cid,'deleted':True}
        assert (await c.delete(f'/api/v1/conversations/{cid}')).json()==expected
        assert (await c.delete(f'/api/v1/conversations/{cid}?stop_active=true')).json()==expected
        assert (await c.get(f'/api/v1/conversations/{cid}')).status_code==404
        assert (await c.get(f'/api/v1/conversations/{keep}')).json()==saved
        assert counts(store)==dict(conversations=1,conversation_requests=1,conversation_messages=2,tasks=0,task_events=0,tool_calls=0)
        assert not store._live and not app.state.task_host._tasks and not store._waiters
        for tid in task_ids:
            for suffix in ('','/trace','/events'):
                assert (await c.get(f'/api/v1/tasks/{tid}'+suffix)).status_code==404
            assert (await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})).status_code==404
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert preserved=={p.relative_to(cpp_repo):p.read_bytes() for p in cpp_repo.rglob('*') if p.is_file()}
    async with api(path) as (c,app,_,_):
        assert (await c.get('/api/v1/tasks')).json()['total']==0
        assert (await c.get('/api/v1/conversations')).json()['total']==1
        assert (await c.get(f'/api/v1/conversations/{keep}')).json()==saved
        assert (await c.delete(f'/api/v1/conversations/{cid}')).json()==expected
        assert (await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=rid,prompt='迟到重试'))).status_code==404


@pytest.mark.asyncio
async def test_delete_running_answer_waits_cleanup_and_survives_disconnect():
    started=Event(); closing=Event(); release=Event(); closed=Event()
    class LateAnswer:
        async def chat(self,messages,tools=None):
            started.set()
            try: await asyncio.Future()
            except asyncio.CancelledError: return reply('取消后迟到的回答也必须一起删除')
        async def aclose(self):
            closing.set(); await asyncio.to_thread(release.wait,20); closed.set()
    async with api(chat=LateAnswer) as (c,app,_,models):
        cid=(await c.post('/api/v1/conversations',json={})).json()['conversation_id']
        rid=str(uuid4()); body=dict(request_id=rid,prompt='等待')
        await c.post(f'/api/v1/conversations/{cid}/requests',json=body)
        await eventually(started.is_set)
        assert (await c.delete(f'/api/v1/conversations/{cid}')).status_code==409
        assert not closing.is_set()
        deleting=asyncio.create_task(c.delete(f'/api/v1/conversations/{cid}?stop_active=true'))
        try:
            await eventually(closing.is_set)
            assert not deleting.done() and not closed.is_set()
            assert (await c.get(f'/api/v1/conversations/{cid}')).status_code==200
            assert (await c.post(f'/api/v1/conversations/{cid}/requests',json=body)).status_code==409
            deleting.cancel()
            with pytest.raises(asyncio.CancelledError): await deleting
            retry=asyncio.create_task(c.delete(f'/api/v1/conversations/{cid}?stop_active=true'))
            await asyncio.sleep(.03); assert not retry.done()
            release.set()
            assert (await retry).status_code==200
            assert closed.is_set() and len(models)==1
            assert not app.state.conversation_host._answers
            await eventually(lambda:not app.state.conversation_host._deletions)
            assert counts(app.state.task_host.store)==dict.fromkeys(counts(app.state.task_host.store),0)
        finally: release.set()


@pytest.mark.asyncio
async def test_delete_does_not_stop_another_active_conversation():
    class PendingAnswer:
        def __init__(self): self.started=Event(); self.closed=False
        async def chat(self,messages,tools=None):
            self.started.set(); await asyncio.Future()
        async def aclose(self): self.closed=True
    async with api(chat=PendingAnswer) as (c,app,_,models):
        ids=[]; requests=[]
        for prompt in ('删除这一段','另一段继续回答'):
            cid=(await c.post('/api/v1/conversations',json={})).json()['conversation_id']; ids.append(cid)
            rid=str(uuid4()); requests.append(rid)
            assert (await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=rid,prompt=prompt))).status_code==202
        await eventually(lambda:len(models)==2 and all(m.started.is_set() for m in models))
        kept=(await c.get(f'/api/v1/conversations/{ids[1]}')).json()
        assert (await c.delete(f'/api/v1/conversations/{ids[0]}?stop_active=true')).status_code==200
        assert models[0].closed and not models[1].closed
        assert set(app.state.conversation_host._answers)=={requests[1]}
        assert (await c.get(f'/api/v1/conversations/{ids[1]}')).json()==kept
        assert counts(app.state.task_host.store)==dict(conversations=1,conversation_requests=1,conversation_messages=1,tasks=0,task_events=0,tool_calls=0)
        assert app.state.task_host.store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert (await c.post(f'/api/v1/conversations/{ids[1]}/requests/{requests[1]}/stop')).status_code==200


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['analyzing','waiting_approval','modifying','cleanup'])
async def test_delete_task_in_each_execution_phase(phase,cpp_repo,rg_exe,git_exe):
    closing=Event(); release=Event(); services=[]
    class Model(ScriptedLLM):
        async def chat(self,history,tools=None):
            if (phase=='analyzing' and not self.requests) or (phase=='modifying' and len(self.requests)==3):
                await asyncio.Future()
            return await super().chat(history,tools)
        async def aclose(self):
            closing.set(); await asyncio.to_thread(release.wait,20)
    def factory(trace):
        s=TaskService(Model(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=trace); services.append(s); return s
    async with api(task_factory=factory) as (c,app,_,_):
        cid=(await c.post('/api/v1/conversations',json={'repository_path':str(cpp_repo)})).json()['conversation_id']
        result=await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=str(uuid4()),mode='task',prompt='任务'))
        tid=result.json()['task_id']
        await until(c,tid,{'analyzing'} if phase=='analyzing' else {'waiting_approval'})
        if phase in {'modifying','cleanup'}:
            await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})
            await eventually(lambda:source_matches(cpp_repo/'main.cpp','int main(){return 0;}\n'))
            if phase=='cleanup': await eventually(closing.is_set)
        before={p.relative_to(cpp_repo):p.read_bytes() for p in cpp_repo.rglob('*') if p.is_file()}
        assert (await c.delete(f'/api/v1/conversations/{cid}')).status_code==409
        operation=asyncio.create_task(c.delete(f'/api/v1/conversations/{cid}?stop_active=true'))
        try:
            await eventually(closing.is_set); await asyncio.sleep(.03)
            assert not operation.done()
            assert (await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})).status_code==409
            assert (await c.get(f'/api/v1/tasks/{tid}')).status_code==200
            release.set(); assert (await operation).status_code==200
            host=app.state.task_host
            assert not host._tasks and not host._repositories and not host.store._live
            assert services[0]._lock is None or services[0]._lock.handle is None
            assert before=={p.relative_to(cpp_repo):p.read_bytes() for p in cpp_repo.rglob('*') if p.is_file()}
            assert not any(counts(host.store).values())
        finally: release.set()


def test_transaction_rollback_and_deleted_page_boundaries(tmp_path):
    store=SqliteEventStore(tmp_path/'pages.sqlite3')
    try:
        for n in range(1,31):
            s=task(n); s.update(state='completed',finished_at=s['updated_at']); store.create(s['task_id'],s); store.publish(s['task_id'],s,[],closed=True)
        page=store.conversation_page(7); boundary=page['items'][-1]['conversation_id']
        task_page=store.list_page(7)
        original=counts(store); live=set(store._live)
        store._db.execute("CREATE TRIGGER deletion_fixture BEFORE DELETE ON tasks BEGIN SELECT RAISE(ABORT,'rollback fixture'); END")
        with pytest.raises(sqlite3.IntegrityError): store.delete_conversation(boundary)
        assert counts(store)==original and store._live==live
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
        store._db.execute('DROP TRIGGER deletion_fixture')
        store.delete_conversation(boundary)
        # Both cursors contain the timestamp/identity boundary, no lookup of a deleted row.
        rest=store.conversation_page(100,page['next_cursor'])
        assert len(rest['items'])==23 and rest['total']==29
        assert not ({r['conversation_id'] for r in page['items']} & {r['conversation_id'] for r in rest['items']})
        assert len(store.list_page(100,task_page['next_cursor'])['items'])==23
        for invalid in ['p1.???','p1.W10','p1.eyJpbmplY3QiOjF9']:
            with pytest.raises(ForgePilotError,match='invalid_page'): store.conversation_page(10,invalid)
        assert store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally: store.close()


@pytest.mark.asyncio
async def test_delete_ends_live_sse_and_prevents_replay(cpp_repo,rg_exe,git_exe):
    def factory(trace): return TaskService(ScriptedLLM(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    with serving(factory,heartbeat=10) as (url,app):
        async with httpx.AsyncClient(base_url=url,timeout=15,trust_env=False) as c:
            tid=(await c.post('/api/v1/tasks',json=dict(repository_path=str(cpp_repo),prompt='等待审批'))).json()['task_id']
            waiting=await until(c,tid,{'waiting_approval'})
            cid=(await c.get('/api/v1/conversations')).json()['items'][0]['conversation_id']
            async with c.stream('GET',f'/api/v1/tasks/{tid}/events',headers={'Last-Event-ID':waiting['last_event_id']}) as stream:
                await eventually(lambda:app.state.task_host.store.waiter_count(tid)>0)
                assert (await c.delete(f'/api/v1/conversations/{cid}?stop_active=true')).status_code==200
                await stream.aread()  # Clean EOF even if deletion wins the waiter/read race.
            await eventually(lambda:app.state.task_host.store.waiter_count(tid)==0)
            assert (await c.get(f'/api/v1/tasks/{tid}/events',headers={'Last-Event-ID':waiting['last_event_id']})).status_code==404
            assert (await c.get(f'/api/v1/tasks/{tid}/events')).status_code==404


@pytest.mark.asyncio
async def test_delete_cancels_real_build_tree_without_rolling_back(build_repo,build_config,rg_exe,git_exe):
    (build_repo/'slow.cmake').write_text('file(WRITE "${CMAKE_CURRENT_LIST_DIR}/build-started.txt" "started")\nexecute_process(COMMAND "${CMAKE_COMMAND}" -E sleep 60)\n')
    with (build_repo/'CMakeLists.txt').open('a') as f:
        f.write('add_custom_command(TARGET counter POST_BUILD COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/slow.cmake")\n')
    services=[]
    def factory(trace):
        s=TaskService(ScriptedLLM(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=trace,build_config=build_config); services.append(s); return s
    async with api(task_factory=factory) as (c,app,_,_):
        cid=(await c.post('/api/v1/conversations',json={'repository_path':str(build_repo)})).json()['conversation_id']
        response=await c.post(f'/api/v1/conversations/{cid}/requests',json=dict(request_id=str(uuid4()),mode='task',prompt='构建'))
        tid=response.json()['task_id']; await until(c,tid,{'waiting_approval'})
        await c.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})
        async with asyncio.timeout(30):
            while not (build_repo/'build-started.txt').exists(): await asyncio.sleep(.02)
        source=(build_repo/'main.cpp').read_bytes(); notes=(build_repo/'notes.txt').read_bytes()
        assert (await c.delete(f'/api/v1/conversations/{cid}?stop_active=true')).status_code==200
        saved=services[0].snapshot()
        assert saved.build_result['stages']['cmake_build']=='cancelled'
        assert 'owned process tree terminated' in saved.build_result['commands'][-1]['stderr']
        assert (build_repo/'main.cpp').read_bytes()==source and (build_repo/'notes.txt').read_bytes()==notes
        assert not app.state.task_host._tasks and not app.state.task_host._repositories
        assert not any(counts(app.state.task_host.store).values())
