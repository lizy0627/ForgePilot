"""Offline fixtures only; real SQLite transactions, process restart and Runtime calls."""
import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest

from conftest import ScriptedLLM, reply, call
from forgepilot.api import create_app
from forgepilot.api_models import EventPayload
from forgepilot.context import TaskContext, TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.recording import now, snapshot_context, project_events, TaskJournal
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION, SchemaVersion, requires_upgrade_backup
import forgepilot.sqlite_store as sqlite_store_module
from forgepilot.task_service import TaskService
from forgepilot.task_host import TaskHost
from forgepilot.trace import TraceEvent
from forgepilot.trace import MemoryTrace
from test_api import until, frames, messages
from test_build import build_repo, build_config, finish
from test_repair import script, reflection
from test_changes import write
from test_planning import proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", [1, 2, 3, "snapshot"])
async def test_feedback_admission_storage_failure_never_starts_model(cpp_repo, rg_exe, git_exe, tmp_path, monkeypatch, publication):
    from test_conversations import api
    path = tmp_path / "feedback-storage.sqlite3"
    services = []
    class Model(ScriptedLLM):
        closed = False
        async def aclose(self): self.closed = True
    def factory(trace):
        service = TaskService(Model(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
        services.append(service)
        return service
    async with api(path, task_factory=factory) as (client, app, _, _):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        host, store = app.state.task_host, app.state.task_host.store
        entry = host._tasks[task_id]
        publish = store._publish
        attempts = []
        def fail(*args, **kwargs):
            attempts.append(1)
            result = publish(*args, **kwargs)
            if isinstance(publication, int) and len(attempts) >= publication:
                raise sqlite3.OperationalError("private disk details and feedback must not escape")
            return result
        monkeypatch.setattr(store, "_publish", fail)
        if publication == "snapshot":
            def fail_read(task_id):
                raise sqlite3.OperationalError("private snapshot read failure")
            monkeypatch.setattr(store, "get", fail_read)
        response = await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Keep the API"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
        assert "private disk" not in response.text and "Keep the API" not in response.text
        assert len(attempts) == (3 if publication == "snapshot" else publication) and len(services[0].llm.requests) == 2
        assert entry.running.done() and entry.closed and services[0].llm.closed
        assert services[0].state == TaskState.FAILED and services[0]._active is None
        assert services[0]._lock.handle is None and not host._tasks and not host._repositories
        assert services[0]._runtime._grant is None and services[0]._runtime._review is None
        assert (await client.get(f"/api/v1/tasks/{task_id}")).status_code == 503
        row = store._db.execute("SELECT closed,state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        assert not row["closed"] and row["state"] in {"planning", "waiting_approval"}
        assert store._db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='plan_feedback' AND trace_sequence IS NOT NULL", (task_id,)).fetchone()[0] == (publication in {3, "snapshot"})
    reopened = SqliteEventStore(path)
    try:
        saved = reopened.get(task_id)
        assert saved["state"] == "failed" and saved["error_code"] == "service_interrupted" and saved["read_only"]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_feedback_keeps_one_conversation_request_until_original_task_finishes(cpp_repo, rg_exe, git_exe, tmp_path):
    from uuid import uuid4
    from test_conversations import api
    path = tmp_path / "feedback-linked.sqlite3"
    sequence = messages()[:2] + [reply("revised analysis"), messages()[1]] + messages()[2:]
    def factory(trace):
        return TaskService(ScriptedLLM(sequence), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    async with api(path, task_factory=factory) as (client, app, _, _):
        cid = (await client.post("/api/v1/conversations", json={"repository_path": str(cpp_repo)})).json()["conversation_id"]
        rid = str(uuid4())
        task_id = (await client.post(f"/api/v1/conversations/{cid}/requests", json={"request_id": rid, "mode": "task", "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        before = (await client.get(f"/api/v1/conversations/{cid}")).json()["messages"]
        store = app.state.task_host.store
        version = store._db.execute("PRAGMA user_version").fetchone()[0]
        tables = store._db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        response = await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Keep API"})
        assert response.status_code == 202
        assert (await until(client, task_id, {"waiting_approval"}))["plan_version"] == 2
        request = (await client.get(f"/api/v1/conversations/{cid}/requests/{rid}")).json()
        assert request["status"] == "running" and request["task_id"] == task_id
        assert (await client.get(f"/api/v1/conversations/{cid}")).json()["messages"] == before
        assert store._db.execute("SELECT COUNT(*) FROM conversation_requests").fetchone()[0] == 1
        assert store._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 2})
        saved = await until(client, task_id, {"completed"})
        assert saved["task_diff"]["files"]
        request = (await client.get(f"/api/v1/conversations/{cid}/requests/{rid}")).json()
        assert request["status"] == "completed"
        assert len((await client.get(f"/api/v1/conversations/{cid}")).json()["messages"]) == len(before) + 1
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == version
        assert store._db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == tables
    async with api(path) as (client, app, _, _):
        historic = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        assert historic == {**saved, "read_only": True}
        page = (await client.get(f"/api/v1/tasks/{task_id}/trace?limit=100")).json()
        assert sum(item["kind"] == "plan_feedback" for item in page["items"]) == 1
        assert sum(item["kind"] == "plan_created" for item in page["items"]) == 2
        assert (await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 2, "feedback": "Revise"})).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["planning", "completed"])
async def test_feedback_process_restart_preserves_trace_and_is_read_only(cpp_repo, rg_exe, git_exe, tmp_path, ending):
    from test_conversations import api
    path = tmp_path / "feedback-crash.sqlite3"
    script = '''
import asyncio, json, os, sys
from forgepilot.llm import ChatReply
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.task_service import TaskService
class Model:
 def __init__(self): self.answers = iter(["analysis", sys.argv[5], "revised analysis", sys.argv[5], "done"])
 async def chat(self, messages, tools=None):
  return ChatReply.model_validate({"message":{"role":"assistant","content":next(self.answers)},"usage":None})
async def main():
 store = SqliteEventStore(sys.argv[1])
 host = TaskHost(lambda trace: TaskService(Model(), rg_executable=sys.argv[3], git_executable=sys.argv[4], trace=trace), store=store)
 accepted = await host._create(sys.argv[2], "restart fixture")
 tid = accepted["task_id"]; entry = host._tasks[tid]
 await entry.running
 await host._feedback(tid, 1, "Persist this feedback")
 if sys.argv[6] == "completed":
  await entry.running
  await host._approve(tid, 2)
  await entry.running
 os._exit(17)
asyncio.run(main())
'''
    run = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(path), str(cpp_repo), rg_exe, git_exe,
        json.dumps(proposal("main.cpp")), ending], timeout=20, capture_output=True,
        cwd=Path(__file__).parents[1])
    assert run.returncode == 17, run.stderr.decode(errors="replace")
    async with api(path) as (client, app, _, _):
        store = app.state.task_host.store
        saved = store.list()[0]
        task_id = saved["task_id"]
        assert saved["read_only"] and saved["state"] == ("completed" if ending == "completed" else "failed")
        if ending == "planning":
            assert saved["error_code"] == "service_interrupted" and not saved["duration_complete"]
        for endpoint, body in [("approve", {"plan_version": saved["plan_version"]}),
                               ("feedback", {"plan_version": saved["plan_version"], "feedback": "Cannot resume"})]:
            response = await client.post(f"/api/v1/tasks/{task_id}/{endpoint}", json=body)
            assert response.status_code == 409 and response.json()["error"]["code"] == "invalid_state"
        page = (await client.get(f"/api/v1/tasks/{task_id}/trace?limit=100")).json()
        feedbacks = [row for row in page["items"] if row["kind"] == "plan_feedback"]
        plans = [row for row in page["items"] if row["kind"] == "plan_created"]
        assert len(feedbacks) == 1 and len(plans) == (2 if ending == "completed" else 1)
        detail = (await client.get(f'/api/v1/tasks/{task_id}/trace/{feedbacks[0]["sequence"]}')).json()
        assert detail["data"] == {"plan_version": 1, "feedback": "Persist this feedback"}
        for version, row in enumerate(plans, 1):
            detail = (await client.get(f'/api/v1/tasks/{task_id}/trace/{row["sequence"]}')).json()
            assert detail["data"]["plan_version"] == version and detail["data"]["plan"] == proposal("main.cpp")
        events = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        assert events[-1][1]["event"] == ("task_completed" if ending == "completed" else "task_failed")
        assert store.get(task_id) == saved


def task(number=1):
    context=TaskContext("D:/development-test","offline fixture",task_id=f"00000000-0000-4000-8000-{number:012d}")
    return snapshot_context(context,created_at=f"2026-09-18T10:00:{number:02d}+00:00",updated_at=now())


@pytest.mark.asyncio
@pytest.mark.parametrize("already_waiting", [False, True])
async def test_unavailable_task_wakes_waiters_and_guards_all_history_reads(tmp_path, already_waiting):
    store = SqliteEventStore(tmp_path / "unavailable.sqlite3")
    value = task(); tid = value["task_id"]
    store.create(tid, value)
    cid, rid = store._db.execute("SELECT conversation_id,request_id FROM conversation_requests WHERE task_id=?", (tid,)).fetchone()
    waiting = None
    try:
        if already_waiting:
            waiting = asyncio.create_task(store.wait(tid, 0, 10))
            async with asyncio.timeout(2):
                while not store.waiter_count(tid):
                    await asyncio.sleep(.01)
        store.mark_task_unavailable(tid)
        if waiting is None:
            waiting = asyncio.create_task(store.wait(tid, 0, 10))
        with pytest.raises(ForgePilotError, match="storage_unavailable"):
            await asyncio.wait_for(waiting, 2)
        assert store.waiter_count(tid) == 0 and tid not in store._live
        for read in (lambda: store.get(tid), lambda: store.read(tid, 0), lambda: store.cursor(tid, None),
                     lambda: store.trace_page(tid), lambda: store.trace_detail(tid, 1), store.list_page,
                     lambda: store.conversation_detail(cid), lambda: store.conversation_request(cid, rid),
                     lambda: store.conversation_mcp_tools(cid, rid), store.conversation_page):
            with pytest.raises(ForgePilotError, match="storage_unavailable"):
                read()
        assert store._db.execute("SELECT state,closed FROM tasks").fetchone()[:] == ("created", 0)
        assert store._db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    finally:
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("segment,fault", [("start", "manager"), ("start", "legacy"), ("start", "both"),
    ("start", "report"), ("execute", "manager"), ("execute", "legacy"), ("execute", "both")])
async def test_service_cleanup_failures_do_not_skip_other_resources(cpp_repo, rg_exe, git_exe, segment, fault):
    calls = []
    class Legacy:
        async def close(self):
            calls.append("legacy")
            if fault in {"legacy", "both"}: raise RuntimeError("legacy fixture")
    class Pipeline:
        def report(self):
            calls.append("report")
            if fault == "report": raise RuntimeError("report fixture")
            return {}
    async def manager_close():
        calls.append("manager")
        if fault in {"manager", "both"}: raise RuntimeError("manager fixture")
    def inject():
        service.mcp_manager.close = manager_close
        service._mcp = Legacy()
        if segment == "start": service._runtime.build_pipeline = Pipeline()
    def on_event(event):
        if segment == "start" and event.kind == "task_waiting": inject()
    trace = MemoryTrace(on_event=on_event)
    service = TaskService(ScriptedLLM([reply("analysis"), reply(json.dumps(proposal("main.cpp"))), reply("done")]),
                          rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    try:
        if segment == "start":
            with pytest.raises(RuntimeError): await service.start(str(cpp_repo), "fixture")
        else:
            pending = await service.start(str(cpp_repo), "fixture")
            service.approve(pending.task_id, 1)
            inject()
            with pytest.raises(RuntimeError): await service.execute(pending.task_id)
        assert calls == (["manager", "legacy", "report"] if segment == "start" else ["manager", "legacy"])
        assert service._active is None and service._lock.handle is None and service.state == TaskState.FAILED
        assert service._runtime._grant is None and not service._context.approved_files
        before = len(trace.events)
        service.fail_without_trace(ForgePilotError("storage_unavailable", "fixture"))
        assert len(trace.events) == before and service._context.failure_kind == "storage_unavailable"
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["service", "llm", "both"])
@pytest.mark.parametrize("storage_fault", [False, True])
async def test_finalize_attempts_both_closes_and_always_releases_reservation(cpp_repo, rg_exe, git_exe, tmp_path, monkeypatch, fault, storage_fault):
    calls = []
    class Model(ScriptedLLM):
        async def aclose(self):
            calls.append("llm")
            if fault in {"llm", "both"}: raise RuntimeError("llm close fixture")
    service = None
    def factory(trace):
        nonlocal service
        service = TaskService(Model(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
        return service
    store = SqliteEventStore(tmp_path / "close-fault.sqlite3")
    host = TaskHost(factory, store=store)
    try:
        created = await host._create(str(cpp_repo), "fixture")
        tid = created["task_id"]; entry = host._tasks[tid]
        await entry.running
        close = service.close
        def failing_close():
            calls.append("service")
            close()
            if fault in {"service", "both"}: raise RuntimeError("service close fixture")
        monkeypatch.setattr(service, "close", failing_close)
        publish = store._publish
        def failing_publish(tid, snapshot, events, traces, closed, **kwargs):
            result = publish(tid, snapshot, events, traces, closed, **kwargs)
            if storage_fault and closed: raise sqlite3.OperationalError("commit fixture")
            return result
        monkeypatch.setattr(store, "_publish", failing_publish)
        await host._approve(tid, 1)
        await asyncio.wait_for(entry.running, 2)
        assert calls == ["service", "llm"]
        assert not host._tasks and not host._repositories and service._lock.handle is None
        assert service._active is None and service.state == TaskState.FAILED
        assert service._context.failure_kind == ("storage_unavailable" if storage_fault else "task_cleanup")
        if storage_fault:
            with pytest.raises(ForgePilotError, match="storage_unavailable"): store.get(tid)
            assert store._db.execute("SELECT closed FROM tasks WHERE task_id=?", (tid,)).fetchone()[0] == 0
        else:
            assert store.get(tid)["state"] == "failed" and store.read(tid, 0)[1]
    finally:
        await host._shutdown()
        store.close()


@pytest.mark.asyncio
async def test_failure_after_actual_commit_does_not_rewrite_durable_history(cpp_repo, rg_exe, git_exe, tmp_path, monkeypatch):
    path = tmp_path / "commit-ack.sqlite3"
    store = SqliteEventStore(path)
    host = TaskHost(lambda trace: TaskService(ScriptedLLM(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=trace), store=store)
    publish = store.publish
    def lost_ack(*args, **kwargs):
        publish(*args, **kwargs)
        if kwargs.get("closed"): raise sqlite3.OperationalError("commit acknowledgement failed")
    monkeypatch.setattr(store, "publish", lost_ack)
    try:
        created = await host._create(str(cpp_repo), "fixture")
        tid = created["task_id"]; entry = host._tasks[tid]
        await entry.running
        await host._approve(tid, 1)
        await asyncio.wait_for(entry.running, 2)
        with pytest.raises(ForgePilotError, match="storage_unavailable"): store.get(tid)
        assert entry.service.state == TaskState.FAILED
        assert store._db.execute("SELECT state,closed FROM tasks WHERE task_id=?", (tid,)).fetchone()[:] == ("completed", 1)
    finally:
        await host._shutdown()
        store.close()
    recovered = SqliteEventStore(path)
    try:
        assert recovered.get(tid)["state"] == "completed"
        assert recovered.get(tid)["error"] is None and recovered.get(tid)["read_only"]
    finally:
        recovered.close()


def test_journal_reuses_event_snapshot_and_flushes_settled_terminal_data():
    context = TaskContext("D:/development-test", "offline fixture", state=TaskState.ANALYZING)
    reads = []
    def snapshot():
        reads.append(context.state)
        return context
    store = SqliteEventStore()
    journal = TaskJournal(store, snapshot, Redactor())
    try:
        journal.flush()
        assert not reads
        journal.emit(context.task_id, "task_created", {})
        assert len(reads) == 1  # One capture for both creating and publishing this event.
        assert store.get(context.task_id)["state"] == "analyzing"
        context.state = TaskState.COMPLETED
        journal.emit(context.task_id, "task_state_changed", {"state": "completed"})
        journal.emit(context.task_id, "task_finished", {})
        assert store.get(context.task_id)["state"] == "analyzing"
        assert not store.read(context.task_id, 0)[1]  # Resource cleanup has not settled yet.
        context.final_result = "result after cleanup"
        journal.flush()
        events, closed = store.read(context.task_id, 0)
        assert closed and events[-1].payload.event == "task_completed"
        assert events[-1].payload.data["final_result"] == context.final_result
        assert store.get(context.task_id)["finished_at"]
        assert len(reads) == 4
        context.task_diff = {"files": [], "restored": True}
        journal.emit(context.task_id, "file_restore", {"restored": True})
        journal.flush()
        assert store.get(context.task_id)["task_diff"] == context.task_diff
        assert store.read(context.task_id, 0)[0] == events  # Amendments cannot republish terminal SSE.
        assert [row[0] for row in store._db.execute(
            "SELECT kind FROM task_events WHERE trace_sequence IS NOT NULL ORDER BY trace_sequence"
        )] == ["task_created", "task_state_changed", "task_finished", "file_restore"]
    finally:
        store.close()


def test_initialize_reopen_three_tables_and_version(tmp_path):
    path=tmp_path/"history.sqlite3"
    for _ in range(2):
        store=SqliteEventStore(path)
        assert store._db.execute("PRAGMA user_version").fetchone()[0]==CURRENT_SCHEMA_VERSION
        assert {r[0] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}=={
            "tasks","task_events","tool_calls","conversations","conversation_requests","conversation_messages",
            "conversation_summaries","memories","mcp_servers","mcp_migrations","rag_files","rag_index_runs",
            "task_graph_runs","task_node_attempts","artifacts","artifact_relations","approvals","model_calls",
            "checkpoints","checkpoint_intents","workspace_records","validation_runs","security_events",
            "graph_node_states","graph_run_inputs","graph_node_results","graph_events",
        }
        assert store._db.execute("PRAGMA foreign_keys").fetchone()[0]==1
        store.close()
    with sqlite3.connect(path) as db: db.execute("PRAGMA user_version=99")
    with pytest.raises(ForgePilotError,match="storage_version"): SqliteEventStore(path)
    with sqlite3.connect(path) as db: assert db.execute("PRAGMA user_version").fetchone()[0]==99


def test_schema_version_ledger_has_unique_contiguous_ownership():
    versions = [int(version) for version in SchemaVersion]
    assert versions == list(range(1, int(CURRENT_SCHEMA_VERSION) + 1))
    assert SchemaVersion.RAG_FILES == 6
    assert SchemaVersion.RAG_INDEX_LIFECYCLE == 7
    assert SchemaVersion.GRAPH_ARTIFACT_STORE == 8
    assert SchemaVersion.GRAPH_ORCHESTRATOR == 9
    assert SchemaVersion.GRAPH_CHECKPOINT_RECOVERY == CURRENT_SCHEMA_VERSION == 10
    assert not requires_upgrade_backup(0)
    assert all(requires_upgrade_backup(version) for version in versions[:-1])
    assert not requires_upgrade_backup(int(CURRENT_SCHEMA_VERSION))


def test_schema_ledger_cannot_advance_without_a_migration(monkeypatch):
    monkeypatch.setattr(sqlite_store_module, "CURRENT_SCHEMA_VERSION", int(CURRENT_SCHEMA_VERSION) + 1)
    with pytest.raises(ForgePilotError, match="migration path") as error:
        SqliteEventStore()
    assert error.value.code == "storage_version"


def test_writer_lock_prevents_false_recovery(tmp_path):
    path=tmp_path/"one.sqlite3"; store=SqliteEventStore(path)
    value=task(); store.create(value["task_id"],value)
    with pytest.raises(ForgePilotError,match="storage_busy"): SqliteEventStore(path)
    assert store.get(value["task_id"])["state"]=="created"
    store.close()


def test_unversioned_foreign_database_rejected(tmp_path):
    path=tmp_path/"foreign.sqlite3"
    with sqlite3.connect(path) as db: db.execute("CREATE TABLE valuable(data TEXT)")
    with pytest.raises(ForgePilotError,match="storage_version"): SqliteEventStore(path)
    with sqlite3.connect(path) as db: assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[("valuable",)]


def test_atomic_snapshot_trace_tool_and_sse_retry(tmp_path):
    store=SqliteEventStore(tmp_path/"atomic.sqlite3"); value=task(); id=value["task_id"]
    store.create(id,value)
    traces=[TraceEvent(1,id,"tool_started",now(),{"name":"read_file","tool_call_id":"same-id","arguments":{"path":"main.cpp"}}),
            TraceEvent(2,id,"tool_finished",now(),{"name":"read_file","tool_call_id":"same-id","success":True,"output":"result","duration_ms":4})]
    events=project_events(traces,value)
    store.publish(id,value,events,traces=traces)
    store.publish(id,value,events,traces=traces)
    saved=store.get(id)
    assert saved["tool_calls"]==1 and saved["last_event_id"]==id+":2"
    assert saved["usage"]["total_tokens"] is None
    rows=store._db.execute("SELECT * FROM tool_calls").fetchall()
    assert len(rows)==1 and rows[0]["status"]=="succeeded" and rows[0]["duration_ms"]==4
    assert store._db.execute("PRAGMA foreign_key_check").fetchall()==[]
    assert len(store.read(id,0)[0])==2
    assert store.trace_page(id)["items"][0]["kind"]=="tool_started"
    assert store.trace_detail(id,1)["data"]["arguments"]=={"path":"main.cpp"}
    # Database failure between event insert and snapshot update rolls the entire publication back.
    store._db.execute("CREATE TEMP TRIGGER fail_publish BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'fixture'); END")
    new=TraceEvent(3,id,"reflection",now(),{"reason":"fixture"})
    with pytest.raises(sqlite3.IntegrityError): store.publish(id,value,project_events([new],value),traces=[new])
    assert store.get(id)==saved and len(store.trace_page(id)["items"])==2
    store.close()


def test_redaction_before_all_tables_and_database_bytes(tmp_path):
    path=tmp_path/"redacted.sqlite3"; secret="fixture-private-key-unique"
    store=SqliteEventStore(path,redactor=Redactor((secret,)))
    value=task(); id=value["task_id"]
    value["prompt"]="Authorization: Basic Zml4dHVyZS1jcmVkZW50aWFs\n"+secret
    value["task_diff"]={"files":[{"path":"main.cpp","diff":"+"+secret,"created":False}]}
    store.create(id,value)
    traces=[TraceEvent(1,id,"tool_started",now(),{"name":"read_file","arguments":json.dumps({"Authorization":"Basic opaque-auth","api_key":secret,"nested":{"refresh_token":"private-refresh"}})}),
        TraceEvent(2,id,"tool_finished",now(),{"name":"read_file","success":False,"output":{"Authorization":"Bearer token-secret","password":"pass-secret"},"error":secret,"duration_ms":12})]
    store.publish(id,value,project_events(traces,value),traces=traces)
    output=json.dumps(store.get(id))+json.dumps(store.trace_detail(id,1))+str(store._db.execute("SELECT result_json FROM tool_calls").fetchall()[0][0])
    for hidden in (secret,"Zml4dHVyZS1jcmVkZW50aWFs","opaque-auth","private-refresh","token-secret","pass-secret"): assert hidden not in output
    assert "REDACTED" in output
    store.close()
    raw=path.read_bytes()
    for hidden in (secret,"Zml4dHVyZS1jcmVkZW50aWFs","opaque-auth","private-refresh","token-secret","pass-secret"): assert hidden.encode() not in raw


def test_legacy_sqlite_rows_are_sanitized_at_every_task_read_boundary(tmp_path):
    store=SqliteEventStore(tmp_path/"legacy-redaction.sqlite3")
    value=task(); tid=value["task_id"]
    store.create(tid,value)
    trace=TraceEvent(1,tid,"tool_started",now(),{"name":"read_file","arguments":{"path":"main.cpp"}})
    store.publish(tid,value,project_events([trace],value),traces=[trace])
    secret="legacy-environment-secret"
    with store._transaction():
        snapshot=json.loads(store._db.execute("SELECT snapshot_json FROM tasks WHERE task_id=?",(tid,)).fetchone()[0])
        summary=json.loads(store._db.execute("SELECT summary_json FROM tasks WHERE task_id=?",(tid,)).fetchone()[0])
        snapshot["environment"]={"PRIVATE":secret}
        summary["api_key"]=secret
        store._db.execute("UPDATE tasks SET snapshot_json=?,summary_json=? WHERE task_id=?",
            (json.dumps(snapshot),json.dumps(summary),tid))
        store._db.execute("UPDATE task_events SET data_json=? WHERE task_id=? AND trace_sequence=1",
            (json.dumps({"environment":{"PRIVATE":secret}}),tid))
        store._db.execute("UPDATE task_events SET data_json=? WHERE task_id=? AND sse_sequence=1",
            (json.dumps({"api_key":secret}),tid))
    assert secret in store._db.execute("SELECT snapshot_json FROM tasks WHERE task_id=?",(tid,)).fetchone()[0]
    assert secret not in json.dumps(store.get(tid))
    assert secret not in json.dumps(store.list_page())
    assert secret not in "".join(item.frame() for item in store.read(tid,0)[0])
    assert secret not in json.dumps(store.trace_page(tid))
    assert secret not in json.dumps(store.trace_detail(tid,1))
    store.close()


def test_keyset_history_and_small_trace_pages(tmp_path):
    store=SqliteEventStore(tmp_path/"pages.sqlite3")
    for i in range(1,8):
        value=task(i); store.create(value["task_id"],value)
    page=store.list_page(3); assert len(page["items"])==3 and page["total"]==7
    assert "build_result" not in page["items"][0] and "task_diff" not in page["items"][0]
    inserted=task(8); store.create(inserted["task_id"],inserted)
    following=store.list_page(3,page["next_cursor"])
    final=store.list_page(3,following["next_cursor"])
    ids=[item["task_id"] for item in page["items"]+following["items"]+final["items"]]
    assert len(ids)==len(set(ids))==7 and inserted["task_id"] not in ids and final["next_cursor"] is None
    value=task(1); id=value["task_id"]
    traces=[TraceEvent(i,id,"llm_request",now(),{"messages":"开发测试"*20000}) for i in range(1,4)]
    store.publish(id,value,[],traces=traces)
    trace_page=store.trace_page(id,0,2)
    assert len(trace_page["items"])==2 and all(len(r["preview"])<=240 and "data" not in r for r in trace_page["items"])
    assert len(store.trace_detail(id,trace_page["items"][0]["sequence"])["data"]["messages"])==80000
    assert len(store.trace_page(id,trace_page["next_cursor"],2)["items"])==1
    with pytest.raises(ForgePilotError,match="invalid_page"): store.list_page(10,"absent")
    with pytest.raises(ForgePilotError,match="unknown_trace"): store.trace_detail(id,999)
    store.close()


@pytest.mark.asyncio
async def test_sqlite_wait_cancel_and_publish_cleanup(tmp_path):
    store=SqliteEventStore(tmp_path/"wait.sqlite3"); value=task(); id=value["task_id"]; store.create(id,value)
    waiting=asyncio.create_task(store.wait(id,0,10))
    for _ in range(100):
        if store.waiter_count(id): break
        await asyncio.sleep(0.01)
    assert store.waiter_count(id)==1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError): await waiting
    assert store.waiter_count(id)==0
    waiting=asyncio.create_task(store.wait(id,0,10))
    store.publish(id,value,[EventPayload(event="message",task_id=id,timestamp=now(),data={"content":"wake"})])
    await asyncio.wait_for(waiting,1); assert store.waiter_count(id)==0
    store.close()


@pytest.mark.asyncio
async def test_real_http_restart_history_diff_usage_and_trace(cpp_repo,rg_exe,git_exe,tmp_path):
    path=tmp_path/"http.sqlite3"
    def factory(trace): return TaskService(ScriptedLLM(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    store=SqliteEventStore(path); app=create_app(factory,store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1") as client:
            response=await client.post("/api/v1/tasks",json={"repository_path":str(cpp_repo),"prompt":"development fixture"})
            id=response.json()["task_id"]
            await until(client,id,{"waiting_approval"})
            await client.post(f"/api/v1/tasks/{id}/approve",json={"plan_version":1})
            saved=await until(client,id,{"completed"})
            old=frames((await client.get(f"/api/v1/tasks/{id}/events")).text)
            assert saved["task_diff"]["files"] and saved["diff_summary"] and saved["duration_ms"]>=0
            assert saved["tool_calls"]==2 and all(v is None for v in saved["usage"].values())
    store.close()
    store=SqliteEventStore(path); app=create_app(lambda trace: pytest.fail("History must not instantiate Runtime"),store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1") as client:
            historic=(await client.get(f"/api/v1/tasks/{id}")).json()
            assert historic=={**saved,"read_only":True}
            assert (await client.get("/api/v1/tasks?limit=1")).json()["items"][0]["read_only"]
            assert (await client.post(f"/api/v1/tasks/{id}/approve",json={"plan_version":1})).status_code==409
            assert (await client.post(f"/api/v1/tasks/{id}/cancel")).status_code==409
            replay=frames((await client.get(f"/api/v1/tasks/{id}/events",headers={"Last-Event-ID":old[3][0]})).text)
            assert replay==old[4:]
            page=(await client.get(f"/api/v1/tasks/{id}/trace?limit=100")).json()
            assert {"task_created","llm_request","llm_response","tool_started","tool_finished","task_finished"}<={r["kind"] for r in page["items"]}
            first=page["items"][0]["sequence"]
            assert (await client.get(f"/api/v1/tasks/{id}/trace/{first}")).json()["task_id"]==id
            assert (await client.get("/api/v1/tasks?limit=0")).status_code==422
            assert (await client.get("/api/v1/tasks?before=absent")).status_code==400
            assert (await client.get(f"/api/v1/tasks/{id}/trace/9999")).status_code==404
    store.close()


def test_actual_runtime_process_interruption_is_failed_once(cpp_repo,rg_exe,git_exe,tmp_path):
    path=tmp_path/"crash.sqlite3"
    script='''
import asyncio,os,sys
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.task_service import TaskService
class Waiting:
 async def chat(self,messages,tools=None): await asyncio.Future()
async def main():
 store=SqliteEventStore(sys.argv[1])
 host=TaskHost(lambda trace:TaskService(Waiting(),rg_executable=sys.argv[3],git_executable=sys.argv[4],trace=trace),store=store)
 await host.start()
 item=await host.create(sys.argv[2],"development crash fixture")
 while store.get(item["task_id"])["steps"]<1: await asyncio.sleep(.01)
 os._exit(17)
asyncio.run(main())
'''
    run=subprocess.run([sys.executable,"-c",script,str(path),str(cpp_repo),rg_exe,git_exe],timeout=15,capture_output=True,
                       cwd=Path(__file__).parents[1])
    assert run.returncode==17,run.stderr.decode(errors="replace")
    store=SqliteEventStore(path); saved=store.list()[0]
    assert saved["state"]=="failed" and saved["read_only"] and saved["error_code"]=="service_interrupted"
    assert not saved["duration_complete"] and saved["usage"]["total_tokens"] is None
    events,closed=store.read(saved["task_id"],0)
    assert closed and [e.payload.event for e in events[-2:]]==["task_state_changed","task_failed"]
    assert store.trace_page(saved["task_id"])["items"][-1]["kind"]=="service_interrupted"
    store.close()
    store=SqliteEventStore(path); assert store.list()[0]==saved; assert len(store.read(saved["task_id"],0)[0])==len(events); store.close()


@pytest.mark.asyncio
async def test_missing_response_usage_remains_unknown(cpp_repo,rg_exe,git_exe,tmp_path):
    store=SqliteEventStore(tmp_path/"usage.sqlite3")
    model=ScriptedLLM([reply(calls=[call()],usage={"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}),ForgePilotError("llm_http","offline credential fixture")])
    journal=TaskJournal(store,lambda:service.snapshot(),Redactor())
    service=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=journal)
    try:
        result=await service.analyze(str(cpp_repo),"fixture"); journal.flush()
        value=store.get(result.task_id)
        assert value["steps"]==2 and value["tool_calls"]==1 and value["usage"]["total_tokens"] is None
        assert value["state"]=="failed"
    finally: service.close(); store.close()


@pytest.mark.asyncio
async def test_real_repair_build_test_trace_survives_restart(build_repo,build_config,rg_exe,git_exe,tmp_path):
    path=tmp_path/"repair.sqlite3"; store=SqliteEventStore(path)
    model=ScriptedLLM(script(0,initial="int main(){return missing_identifier;}\n// existing user change\n")+
        [reflection(),reply(calls=[write("main.cpp","int main(){return 0;}\n// existing user change\n")]),reply("fixed")])
    journal=TaskJournal(store,lambda:service.snapshot(persist_diff=True),Redactor())
    service=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=journal,build_config=build_config)
    try:
        result=await finish(service,build_repo); journal.flush(); saved=store.get(result.task_id)
        assert saved["state"]=="completed" and saved["repair_round"]==1
        initial,repaired=saved["validation_history"]
        assert initial["stages"]["cmake_build"]=="failed" and initial["stages"]["run_tests"]=="not_run"
        assert repaired["tests"]["passed"]==1 and all(c["returncode"]==0 for c in repaired["commands"])
        assert "missing_identifier" in initial["commands"][-1]["stdout"]
        kinds={r[0] for r in store._db.execute("SELECT kind FROM task_events WHERE trace_sequence IS NOT NULL")}
        assert {"repair_started","reflection_started","reflection","build_command_finished","validation_finished"}<=kinds
        assert store._db.execute("SELECT COUNT(*) FROM tool_calls WHERE task_id=?",(result.task_id,)).fetchone()[0]==saved["tool_calls"]
        assert store._db.execute("PRAGMA foreign_key_check").fetchall()==[]
        print("REAL SQLITE REPAIR "+json.dumps({"task_id":result.task_id,"steps":saved["steps"],"tool_calls":saved["tool_calls"],
            "state":saved["state"],"repair_round":1,"validation_history":saved["validation_history"],"diff":saved["task_diff"]},ensure_ascii=False))
    finally: service.close(); store.close()
    store=SqliteEventStore(path); assert store.get(result.task_id)=={**saved,"read_only":True}; store.close()


@pytest.mark.asyncio
async def test_history_diff_is_complete_beyond_tool_excerpt(cpp_repo,rg_exe,git_exe,tmp_path):
    (cpp_repo/"main.cpp").write_text("// old source content\n"*1000+"int main(){return 1;}\n",encoding="utf-8")
    store=SqliteEventStore(tmp_path/"diff.sqlite3")
    journal=TaskJournal(store,lambda:service.snapshot(persist_diff=True),Redactor())
    service=TaskService(ScriptedLLM(messages()),rg_executable=rg_exe,git_executable=git_exe,trace=journal)
    try:
        pending=await service.start(str(cpp_repo),"development diff fixture"); journal.flush()
        service.approve(pending.task_id,1); result=await service.execute(pending.task_id); journal.flush()
        saved=store.get(result.task_id)
        assert saved["state"]=="completed" and not saved["task_diff"]["truncated"]
        assert len(saved["task_diff"]["files"][0]["diff"])>12000
        assert result.task_diff["truncated"]  # Model/tool budget remains bounded; persistence keeps full evidence.
    finally: service.close(); store.close()
