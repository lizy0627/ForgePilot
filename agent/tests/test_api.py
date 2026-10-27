import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
from threading import Event
import time

import httpx
import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.api import create_app
from forgepilot.api_models import EventPayload
from forgepilot.context import TaskContext, TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.recording import now, snapshot_context
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from test_changes import write
from test_planning import proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True], ids=["once", "persistent"])
@pytest.mark.parametrize("phase", ["task_created", "admission", "llm_request", "review", "approve", "file_changed", "cancel", "cancel_review", "reject", "delete", "delete_review", "final"])
async def test_storage_publication_failure_settles_and_releases(cpp_repo, rg_exe, git_exe, tmp_path, monkeypatch, phase, persistent):
    path = tmp_path / "publication-fault.sqlite3"
    store = SqliteEventStore(path)
    models, services, entries = [], [], []
    class Model(ScriptedLLM):
        closed = False
        async def chat(self, messages, tools=None):
            if phase in {"cancel", "delete"} and len(models) == 1:
                await asyncio.Future()
            return await super().chat(messages, tools)
        async def aclose(self):
            self.closed = True
    def factory(trace):
        script = messages()
        if phase == "file_changed":
            script[2] = reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-source"),
                write("main.cpp", "first write"), write("main.cpp", "must not execute", "later-write")])
        model = Model(script); models.append(model)
        service = TaskService(model, rg_executable=rg_exe, git_executable=git_exe, trace=trace)
        services.append(service)
        return service
    app = create_app(factory, store=store)
    host = app.state.task_host
    drive = host._drive
    async def capture(entry, **kwargs):
        entries.append(entry)
        await drive(entry, **kwargs)
    monkeypatch.setattr(host, "_drive", capture)
    publish = store._publish
    armed, failures, attempts_after_failure = True, [], []
    def fail(tid, snapshot, events, traces, closed, **kwargs):
        nonlocal armed
        kinds = {t.kind for t in traces}
        match = (phase in kinds or phase == "admission" and not traces and snapshot["state"] == "created"
            or phase == "review" and snapshot["state"] == "waiting_approval"
            or phase == "approve" and "plan_approved" in kinds
            or phase in {"cancel", "delete"} and snapshot["cancel_requested"]
            or phase in {"reject", "cancel_review", "delete_review"} and closed and snapshot["state"] == "cancelled"
            or phase == "final" and closed and snapshot["state"] == "completed")
        if failures:
            attempts_after_failure.append(tid)
        result = publish(tid, snapshot, events, traces, closed, **kwargs)
        if armed and (match or persistent and failures):
            failures.append((tid, store._db.execute("SELECT trace_sequence FROM tasks WHERE task_id=?", (tid,)).fetchone()[0]))
            if not persistent:
                armed = False
            raise sqlite3.OperationalError("private injected disk failure")
        return result
    monkeypatch.setattr(store, "_publish", fail)
    try:
        async with app.router.lifespan_context(app):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                    created = await asyncio.wait_for(client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fixture"}), 2)
                    assert created.status_code in {202, 503}, created.text
                    tid = entries[0].initial_context.task_id
                    if phase in {"approve", "file_changed", "reject", "cancel_review", "final"}:
                        await until(client, tid, {"waiting_approval"})
                        suffix, body = (("cancel", {"reason": "reject", "plan_version": 1}) if phase == "reject" else
                            ("cancel", {}) if phase == "cancel_review" else ("approve", {"plan_version": 1}))
                        response = await asyncio.wait_for(client.post(f"/api/v1/tasks/{tid}/{suffix}", json=body), 2)
                        assert response.status_code in {202, 503}, response.text
                    elif phase == "cancel":
                        response = await asyncio.wait_for(client.post(f"/api/v1/tasks/{tid}/cancel"), 2)
                        assert response.status_code == 503, response.text
                    elif phase in {"delete", "delete_review"}:
                        if phase == "delete_review": await until(client, tid, {"waiting_approval"})
                        cid = store._db.execute("SELECT conversation_id FROM conversation_requests WHERE task_id=?", (tid,)).fetchone()[0]
                        response = await asyncio.wait_for(client.delete(f"/api/v1/conversations/{cid}?stop_active=true"), 2)
                        assert response.status_code == 503, response.text
                    async with asyncio.timeout(2):
                        while tid in host._tasks:
                            await asyncio.sleep(.01)
                    entry = entries[0]
                    assert entry.admission.done() and entry.running.done()
                    assert entry.running.exception() is None
                    assert entry.persistence_error.code == "storage_unavailable"
                    assert services[0]._active is None and models[0].closed
                    assert services[0]._lock is None or services[0]._lock.handle is None
                    assert services[0].state == TaskState.FAILED
                    assert services[0]._context.final_result is None
                    assert not services[0]._context.approved_files
                    if services[0]._runtime:
                        assert services[0]._runtime._grant is None
                    assert not host._repositories and not host._tasks
                    assert len(failures) == 1 and not attempts_after_failure
                    if phase == "file_changed":
                        assert (cpp_repo / "main.cpp").read_bytes() == b"first write"
                        assert len(models[0].requests) == 3
                    if phase in {"review", "approve", "reject", "final"}:
                        assert entry.terminal_traces  # Uncommitted traces are not discarded.
                    for suffix in ("", "/events", "/trace"):
                        response = await client.get(f"/api/v1/tasks/{tid}{suffix}")
                        assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
                        assert "private injected" not in response.text
                    assert (await client.get("/api/v1/tasks")).status_code == 503
                    row = store._db.execute("SELECT closed,state,trace_sequence FROM tasks WHERE task_id=?", (tid,)).fetchone()
                    assert row["closed"] == 0 and row["state"] not in {"completed", "failed", "cancelled"}
                    seq = [r[0] for r in store._db.execute("SELECT trace_sequence FROM task_events WHERE task_id=? AND trace_sequence IS NOT NULL ORDER BY trace_sequence", (tid,))]
                    assert seq == list(range(1, row["trace_sequence"] + 1))
                    cid, rid = store._db.execute("SELECT conversation_id,request_id FROM conversation_requests WHERE task_id=?", (tid,)).fetchone()
                    for url in (f"/api/v1/conversations/{cid}", f"/api/v1/conversations/{cid}/requests/{rid}"):
                        assert (await client.get(url)).status_code == 503
                    armed = False
                    monkeypatch.setattr(store, "_publish", publish)
                    replacement = await asyncio.wait_for(client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "after recovery"}), 2)
                    assert replacement.status_code == 202, replacement.text
                    replacement_id = replacement.json()["task_id"]
                    await until(client, replacement_id, {"waiting_approval"})
                    assert (await client.post(f"/api/v1/tasks/{replacement_id}/cancel")).status_code == 200
                    assert (await client.get(f"/api/v1/tasks/{tid}")).status_code == 503
            finally:
                # Let teardown complete even against the pre-fix implementation.
                monkeypatch.setattr(store, "_publish", publish)
                async def settle_test_waiters():
                    for entry in entries:
                        if not entry.admission.done():
                            entry.admission.cancel()
                await host._submit(settle_test_waiters())
    finally:
        store.close()
    recovered = SqliteEventStore(path)
    try:
        value = recovered.get(tid)
        assert value["state"] == "failed" and value["error_code"] == "service_interrupted"
        assert recovered.read(tid, 0)[1] and value["read_only"]
        assert recovered._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_database_read_error_is_safe_503(monkeypatch, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe) as (client, app, _):
        def fail(*args, **kwargs): raise sqlite3.OperationalError("private database path and SQL")
        monkeypatch.setattr(app.state.task_host.store, "conversation_page", fail)
        response = await client.get("/api/v1/conversations")
        assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
        assert "private database" not in response.text


@pytest.mark.asyncio
async def test_failed_admission_finishes_while_cleanup_is_joined(cpp_repo, rg_exe, git_exe, monkeypatch):
    closing, release = Event(), Event()
    class SlowClose(ScriptedLLM):
        closes = 0
        async def aclose(self):
            self.closes += 1
            closing.set()
            while not release.is_set(): await asyncio.sleep(.01)
    async with client_for(rg_exe, git_exe, factory=lambda: SlowClose(messages())) as (client, app, services):
        host = app.state.task_host
        publish = host.store.publish
        def fail(*args, **kwargs): raise sqlite3.OperationalError("fixture")
        monkeypatch.setattr(host.store, "publish", fail)
        deleting = None
        try:
            response = await asyncio.wait_for(client.post('/api/v1/tasks', json=dict(repository_path=str(cpp_repo), prompt='fixture')), 2)
            assert response.status_code == 503
            tid = response.json()['error']['task_id']
            assert await asyncio.to_thread(closing.wait, 2)
            entry = host._tasks[tid]
            assert entry.admission.done() and not entry.running.done()
            assert (await client.get(f'/api/v1/tasks/{tid}')).status_code == 503
            cid = host.store._db.execute('SELECT conversation_id FROM conversation_requests WHERE task_id=?', (tid,)).fetchone()[0]
            deleting = asyncio.create_task(client.delete(f'/api/v1/conversations/{cid}?stop_active=true'))
            await asyncio.sleep(.05)
            assert not deleting.done() and services[0].llm.closes == 1
            release.set()
            assert (await asyncio.wait_for(deleting, 2)).status_code == 503
            assert entry.running.done() and not host._tasks and not host._repositories
            assert services[0].llm.closes == 1
        finally:
            release.set()
            monkeypatch.setattr(host.store, "publish", publish)
            if deleting: await deleting


def messages():
    return [reply("Inspected main.cpp"), reply(json.dumps(proposal("main.cpp"))),
            reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-source"),
                         write("main.cpp", "int main(){return 0;}\n")]), reply("Implemented change")]


@asynccontextmanager
async def client_for(rg, git, *, replies=None, factory=None, max_tasks=32):
    instances = []
    def service_factory(trace):
        llm = factory() if factory else ScriptedLLM(replies if replies is not None else messages())
        svc = TaskService(llm, rg_executable=rg, git_executable=git, trace=trace)
        instances.append(svc)
        return svc
    app = create_app(service_factory, heartbeat_seconds=0.05, max_tasks=max_tasks)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            yield client, app, instances


async def until(client, task_id, states):
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/api/v1/tasks/{task_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            if data["state"] in states:
                return data
            await asyncio.sleep(0.01)


def frames(text):
    result = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "data" in lines:
            payload = json.loads(lines["data"])
            assert payload["event"] == lines["event"]
            assert set(payload) == {"event", "task_id", "timestamp", "data"}
            result.append((lines["id"], payload))
    return result


@pytest.mark.asyncio
async def test_create_query_list_approve_late_sse_and_replay(cpp_repo, rg_exe, git_exe, monkeypatch):
    snapshots = []
    original_snapshot = TaskService.snapshot
    def capture(service, **kwargs):
        snapshots.append(service.state)
        return original_snapshot(service, **kwargs)
    monkeypatch.setattr(TaskService, "snapshot", capture)
    async with client_for(rg_exe, git_exe) as (client, app, services):
        created = await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        pending = await until(client, task_id, {"waiting_approval"})
        assert pending["plan_version"] == 1 and pending["approved_files"] == []
        # Review traces wait for cleanup; only the settled publication needs a snapshot.
        assert snapshots.count(TaskState.WAITING_APPROVAL) == 1
        listed = (await client.get("/api/v1/tasks")).json()
        assert listed["total"] == 1 and listed["items"][0]["task_id"] == task_id
        approved = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        assert approved.status_code == 202, approved.text
        completed = await until(client, task_id, {"completed"})
        assert completed["approved_files"] == ["main.cpp"] and completed["task_diff"]["files"]
        response = await client.get(f"/api/v1/tasks/{task_id}/events")
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        stream = frames(response.text)
        assert [int(i.split(":")[1]) for i, _ in stream] == list(range(1, len(stream) + 1))
        assert stream[-1][1]["event"] == "task_completed"
        kinds = {p["event"] for _, p in stream}
        assert {"message", "plan_created", "approval_required", "tool_started", "tool_finished", "file_changed"} <= kinds
        replay = await client.get(f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": stream[3][0]})
        assert frames(replay.text) == stream[4:]
        assert frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text) == stream
        end = await client.get(f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": stream[-1][0]})
        assert end.status_code == 204 and end.content == b""
        stored = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        assert stored["last_event_id"] == stream[-1][0] and stored["finished_at"]
        assert snapshots.count(TaskState.COMPLETED) == 1
        assert services[0]._lock.handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "reject"])
async def test_cancel_waiting_and_reject_mapping(cpp_repo, rg_exe, git_exe, action):
    async with client_for(rg_exe, git_exe) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        body = {} if action == "cancel" else {"reason": "reject", "plan_version": 1}
        result = await client.post(f"/api/v1/tasks/{task_id}/cancel", json=body)
        assert result.status_code == 200 and result.json()["state"] == "cancelled"
        assert result.json()["outcome"] == ("cancelled" if action == "cancel" else "rejected")
        again = await client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert again.status_code == 200 and again.json() == result.json()
        stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        cancelled = [p for _, p in stream if p["event"] == "task_state_changed" and p["data"]["state"] == "cancelled"]
        assert len(cancelled) == 1
        assert not any(p["event"] in {"task_failed", "task_completed"} for _, p in stream)
        assert services[0]._lock.handle is None
        # Cancellation released the repository reservation and actual mutex.
        assert (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "again"})).status_code == 202


@pytest.mark.asyncio
async def test_missing_conflict_versions_and_wrong_state(cpp_repo, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe) as (client, app, services):
        for method, tail, kwargs in [("GET", "", {}), ("GET", "/events", {}),
             ("POST", "/approve", {"json": {"plan_version": 1}}), ("POST", "/cancel", {})]:
            missing = await client.request(method, "/api/v1/tasks/absent" + tail, **kwargs)
            assert missing.status_code == 404 and missing.json()["error"]["code"] == "unknown_task"
        body = {"repository_path": str(cpp_repo), "prompt": "fix"}
        task_id = (await client.post("/api/v1/tasks", json=body)).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        duplicate = await client.post("/api/v1/tasks", json={**body, "repository_path": str(cpp_repo / ".")})
        assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "repository_busy"
        assert duplicate.json()["error"]["task_id"] == task_id
        for endpoint, data in [("approve", {"plan_version": 2}), ("cancel", {"reason": "reject", "plan_version": 2})]:
            result = await client.post(f"/api/v1/tasks/{task_id}/{endpoint}", json=data)
            assert result.status_code == 409 and result.json()["error"]["code"] == "stale_plan"
        await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        await until(client, task_id, {"completed"})
        repeated = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        assert repeated.status_code == 409
        assert (await client.post(f"/api/v1/tasks/{task_id}/cancel")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"repository_path": "", "prompt": "x"},
    {"repository_path": "x", "prompt": " "}, {"repository_path": 42, "prompt": "x"},
    {"repository_path": "x", "prompt": "x", "api_key": "never-echo-this-secret"}])
async def test_invalid_create_schema_does_not_echo_input(rg_exe, git_exe, body):
    async with client_for(rg_exe, git_exe) as (client, _, _):
        result = await client.post("/api/v1/tasks", json=body)
        assert result.status_code == 422 and result.json()["error"]["code"] == "invalid_request"
        assert "never-echo-this-secret" not in result.text


@pytest.mark.asyncio
async def test_invalid_approval_cancel_and_event_cursor(cpp_repo, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe) as (client, _, _):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        for body in ({"plan_version": True}, {"plan_version": "1"}, {"plan_version": 0}, {"plan_version": 1, "approved_files": []}):
            assert (await client.post(f"/api/v1/tasks/{task_id}/approve", json=body)).status_code == 422
        for body in ({"reason": "reject"}, {"plan_version": 1}, {"reason": "untrusted"}):
            assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", json=body)).status_code == 422
        for value, status in [("1", 400), ("wrong:1", 400), (f"{task_id}:-1", 400), (f"{task_id}:0001", 400), (f"{task_id}:999999", 409)]:
            result = await client.get(f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": value})
            assert result.status_code == status


@pytest.mark.asyncio
async def test_approval_and_cancel_race(cpp_repo, rg_exe, git_exe):
    class PausingLLM(ScriptedLLM):
        async def chat(self, messages, tools=None):
            if len(self.requests) >= 2:
                await asyncio.Future()
            return await super().chat(messages, tools)
    async with client_for(rg_exe, git_exe, factory=lambda: PausingLLM(messages())) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        responses = await asyncio.gather(client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1}),
                                         client.post(f"/api/v1/tasks/{task_id}/cancel"))
        assert responses[0].status_code in {202, 409} and responses[1].status_code == 200
        result = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        assert result["state"] == "cancelled" and result["cancel_requested"]
        assert services[0]._lock.handle is None
        stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        assert sum(p["event"] == "task_state_changed" and p["data"]["state"] == "cancelled" for _, p in stream) == 1


@pytest.mark.asyncio
async def test_worker_sync_work_does_not_block_http_loop(cpp_repo, rg_exe, git_exe):
    entered, release = Event(), Event()
    class BlockingLLM:
        async def chat(self, messages, tools=None):
            entered.set()
            release.wait(3)  # Simulates bounded synchronous I/O inside the existing runtime.
            return reply("done")
    async with client_for(rg_exe, git_exe, factory=BlockingLLM) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            result = await asyncio.wait_for(client.get(f"/api/v1/tasks/{task_id}"), 0.5)
            assert result.status_code == 200
            assert (await asyncio.wait_for(client.get("/api/v1/tasks"), 0.5)).status_code == 200
        finally:
            release.set()


@pytest.mark.asyncio
async def test_failed_task_sse_and_shutdown_cleanup(cpp_repo, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe, replies=[ForgePilotError("llm_http", "HTTP 401. Check credentials")]) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        result = await until(client, task_id, {"failed"})
        assert result["error_code"] == "llm_http"
        stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        assert stream[-1][1]["event"] == "task_failed"
        assert services[0]._lock.handle is None
    assert not app.state.task_host._thread.is_alive()


@pytest.mark.asyncio
async def test_shutdown_cancels_review_and_closes_llm(cpp_repo, rg_exe, git_exe):
    class ClosableLLM(ScriptedLLM):
        closed = False
        async def aclose(self):
            self.closed = True
    async with client_for(rg_exe, git_exe, factory=lambda: ClosableLLM(messages())) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
    assert services[0].snapshot().state == TaskState.CANCELLED
    assert services[0]._lock.handle is None and services[0].llm.closed
    assert app.state.task_host.store.read(task_id, 0)[1]


@pytest.mark.asyncio
async def test_invalid_repository_capacity_and_origin(cpp_repo, tmp_path, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe, max_tasks=1) as (client, app, services):
        bad = await client.post("/api/v1/tasks", json={"repository_path": str(tmp_path / "absent"), "prompt": "fix"})
        assert bad.status_code == 422 and bad.json()["error"]["task_id"]
        assert (await client.get("/api/v1/tasks")).json()["items"][0]["state"] == "failed"
        accepted = await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})
        assert accepted.status_code == 202  # Failed admission no longer consumes lifetime capacity.
        await until(client,accepted.json()['task_id'],{'waiting_approval'})
        capacity = await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})
        assert capacity.status_code == 503 and capacity.json()["error"]["code"] == "task_capacity"
        origin = await client.post("/api/v1/tasks", headers={"Origin": "https://untrusted.invalid"}, json={})
        assert origin.status_code == 403


@pytest.mark.asyncio
async def test_event_store_waiter_cleanup_and_publish_race():
    store = SqliteEventStore()
    try:
        value = snapshot_context(TaskContext("D:/fixture", "fixture", task_id="task"), created_at=now(), updated_at=now())
        store.create("task", value)
        waiting = asyncio.create_task(store.wait("task", 0, 10))
        async with asyncio.timeout(2):
            while not store.waiter_count("task"):
                await asyncio.sleep(0.01)
        event = EventPayload(event="message", task_id="task", timestamp=now(), data={"content": "line1\nline2"})
        store.publish("task", value, [event])
        await asyncio.wait_for(waiting, 1)
        assert store.waiter_count("task") == 0
        assert "line1\\nline2" in store.read("task", 0)[0][0].frame()
        waiting = asyncio.create_task(store.wait("task", 1, 10))
        async with asyncio.timeout(2):
            while not store.waiter_count("task"):
                await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert store.waiter_count("task") == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_concurrent_approvals_execute_once(cpp_repo, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        responses = await asyncio.gather(*(client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1}) for _ in range(2)))
        assert sorted(r.status_code for r in responses) == [202, 409]
        result = await until(client, task_id, {"completed"})
        assert result["steps"] == 4
        stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        assert sum(p["event"] == "file_changed" for _, p in stream) == 1


@pytest.mark.asyncio
async def test_http_revised_plan_stale_approval_and_reject(cpp_repo, rg_exe, git_exe):
    sequence = messages()[:2] + [reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-source"),
        write("main.cpp", "int main(){return 1;}"), write("helper.cpp", "new", "write-helper")]),
        reply(json.dumps(proposal("main.cpp", "helper.cpp")))]
    async with client_for(rg_exe, git_exe, replies=sequence) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        async with asyncio.timeout(5):
            while True:
                result = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                if result["state"] == "waiting_approval" and result["plan_version"] == 2:
                    break
                await asyncio.sleep(0.01)
        stale = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_plan"
        cancel = await client.post(f"/api/v1/tasks/{task_id}/cancel", json={"reason": "reject", "plan_version": 2})
        assert cancel.status_code == 200 and cancel.json()["outcome"] == "rejected"
        assert cancel.json()["task_diff"]["files"] and not (cpp_repo / "helper.cpp").exists()


@pytest.mark.asyncio
async def test_conflict_with_existing_cli_service(cpp_repo, rg_exe, git_exe):
    cli = TaskService(ScriptedLLM(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=MemoryTrace())
    await cli.start(str(cpp_repo), "CLI task")
    try:
        async with client_for(rg_exe, git_exe) as (client, app, services):
            response = await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "HTTP task"})
            assert response.status_code == 409 and response.json()["error"]["code"] == "repository_busy"
            task_id = response.json()["error"]["task_id"]
            stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
            assert stream[-1][1]["event"] == "task_failed"
    finally:
        cli.close()


def test_wire_schemas_match_exported_contract():
    from pathlib import Path
    from pydantic import TypeAdapter
    from forgepilot.api_models import ConversationEvent
    root = Path(__file__).parents[2] / "protocol"
    assert json.loads((root / "openapi.json").read_text(encoding="utf-8")) == create_app(lambda trace: None).openapi()
    assert json.loads((root / "events.schema.json").read_text(encoding="utf-8")) == EventPayload.model_json_schema()
    assert json.loads((root / "conversation-events.schema.json").read_text(encoding="utf-8")) == TypeAdapter(ConversationEvent).json_schema()


@pytest.mark.asyncio
async def test_shutdown_cancels_active_request_and_records_terminal(cpp_repo, rg_exe, git_exe):
    class WaitingLLM:
        async def chat(self, messages, tools=None):
            await asyncio.Future()
    async with client_for(rg_exe, git_exe, factory=WaitingLLM) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"analyzing"})
    assert services[0].snapshot().state == TaskState.CANCELLED and services[0]._lock.handle is None
    events, closed = app.state.task_host.store.read(task_id, 0)
    assert closed and events[-1].payload.data["state"] == "cancelled"
    assert not app.state.task_host._thread.is_alive()


@pytest.mark.asyncio
async def test_loopback_host_validation_including_ipv6(rg_exe, git_exe):
    async with client_for(rg_exe, git_exe) as (client, app, services):
        for value in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            assert (await client.get("/api/v1/tasks", headers={"Host": value})).status_code == 200
        for value in ("attacker.invalid", "localhost@attacker.invalid", "attacker@localhost", "localhost:bad", "localhost/other"):
            result = await client.get("/api/v1/tasks", headers={"Host": value})
            assert result.status_code == 400 and result.json()["error"]["code"] == "host_not_allowed"


@pytest.mark.asyncio
async def test_terminal_sse_drains_more_than_one_storage_batch():
    store = SqliteEventStore()
    try:
        value = snapshot_context(TaskContext("D:/fixture", "fixture", task_id="fixture", state=TaskState.COMPLETED), created_at=now(), updated_at=now())
        store.create("fixture", value)
        payloads = [EventPayload(event="message", task_id="fixture", timestamp=now(), data={"index": i}) for i in range(100)]
        store.publish("fixture", value, payloads, closed=True)
        app = create_app(lambda trace: None, store=store)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                result = frames((await client.get("/api/v1/tasks/fixture/events")).text)
                assert len(result) == 100 and [p["data"]["index"] for _, p in result] == list(range(100))
                tail = frames((await client.get("/api/v1/tasks/fixture/events", headers={"Last-Event-ID": "fixture:64"})).text)
                assert tail == result[64:]
    finally:
        store.close()


def test_protocol_examples_validate():
    from pathlib import Path
    from pydantic import TypeAdapter
    from forgepilot.api_models import (CreateTask, ApproveTask, PlanFeedback, CancelTask,
        TaskList, TaskResponse, ErrorResponse, TracePage, TraceDetail, ConversationEvent)
    examples = json.loads((Path(__file__).parents[2] / "protocol/examples.json").read_text(encoding="utf-8"))
    for name, model in [("create_request", CreateTask), ("approve_request", ApproveTask), ("feedback_request", PlanFeedback), ("cancel_request", CancelTask),
        ("feedback_step_limit_error", ErrorResponse), ("feedback_busy_error", ErrorResponse), ("feedback_invalid_request_error", ErrorResponse),
        ("reject_via_cancel_request", CancelTask), ("list_response", TaskList), ("create_or_query_response", TaskResponse),
        ("cancelled_event", EventPayload), ("failed_event", EventPayload), ("not_found_error", ErrorResponse),
        ("stale_plan_error", ErrorResponse), ("invalid_request_error", ErrorResponse), ("repository_busy_error", ErrorResponse),
        ("trace_page_response", TracePage), ("trace_detail_response", TraceDetail), ("interrupted_event", EventPayload)]:
        model.model_validate(examples[name])
    event_adapter=TypeAdapter(ConversationEvent)
    for name in ("conversation_answer_delta", "conversation_answer_snapshot", "conversation_answer_completed"):
        event_adapter.validate_python(examples[name],strict=True)
    for name in ("conversation_stream_cursor_error", "conversation_stream_unavailable_error"):
        ErrorResponse.model_validate(examples[name])


@pytest.mark.asyncio
async def test_feedback_http_202_old_version_redacted_trace_and_new_approval(cpp_repo, rg_exe, git_exe):
    from forgepilot.security import Redactor
    entered, release = Event(), Event()
    class FeedbackLLM(ScriptedLLM):
        def __init__(self):
            super().__init__(messages()[:2] + [reply("revised analysis"), messages()[1]] + messages()[2:])
            self.redactor = Redactor(("feedback-private-secret",))
        async def chat(self, history, tools=None):
            if len(self.requests) == 2:
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
            return await super().chat(history, tools)
    async with client_for(rg_exe, git_exe, factory=FeedbackLLM) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        original = (cpp_repo / "main.cpp").read_bytes()
        try:
            accepted = await asyncio.wait_for(client.post(f"/api/v1/tasks/{task_id}/feedback", json={
                "plan_version": 1, "feedback": "  Keep API\n\nfeedback-private-secret  "}), 2)
            assert accepted.status_code == 202
            assert accepted.json()["task_id"] == task_id and accepted.json()["state"] == "planning"
            assert accepted.json()["plan_version"] == 1 and accepted.json()["approved_plan_version"] is None
            assert await asyncio.to_thread(entered.wait, 2)
            assert not release.is_set() and (cpp_repo / "main.cpp").read_bytes() == original
            assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["state"] == "planning"
            for endpoint, body in [("feedback", {"plan_version": 1, "feedback": "Duplicate"}), ("approve", {"plan_version": 1})]:
                busy = await client.post(f"/api/v1/tasks/{task_id}/{endpoint}", json=body)
                assert busy.status_code == 409 and busy.json()["error"]["code"] == "task_busy"
        finally:
            release.set()
        reviewed = await until(client, task_id, {"waiting_approval"})
        assert reviewed["plan_version"] == 2 and reviewed["steps"] == 4
        assert reviewed["analysis_result"] == accepted.json()["analysis_result"]
        stale = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_plan"
        page = (await client.get(f"/api/v1/tasks/{task_id}/trace?limit=100")).json()
        feedbacks = [row for row in page["items"] if row["kind"] == "plan_feedback"]
        assert len(feedbacks) == 1 and sum(row["kind"] == "plan_created" for row in page["items"]) == 2
        detail = (await client.get(f'/api/v1/tasks/{task_id}/trace/{feedbacks[0]["sequence"]}')).json()
        assert detail["data"] == {"plan_version": 1, "feedback": "Keep API\n\n[REDACTED]"}
        assert "feedback-private-secret" not in json.dumps(page) + json.dumps(detail)
        assert (await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 2})).status_code == 202
        result = await until(client, task_id, {"completed"})
        assert result["task_diff"]["files"] and services[0]._lock.handle is None
        stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
        assert not any(p["event"] == "plan_feedback" for _, p in stream)
        assert [p["data"]["plan_version"] for _, p in stream if p["event"] == "plan_created"] == [1, 2]


@pytest.mark.asyncio
async def test_feedback_invalid_http_requests_and_origin_leave_task_unchanged(cpp_repo, rg_exe, git_exe):
    async with client_for(rg_exe, git_exe, replies=messages()[:2] + [reply("analysis"), messages()[1]]) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        before = await until(client, task_id, {"waiting_approval"})
        service_before = services[0].snapshot()
        trace_before = app.state.task_host.store.trace_page(task_id, limit=100)
        body = {"plan_version": 1, "feedback": "never-echo-this-feedback"}
        invalid = [{}, {"plan_version": 1}, {"feedback": "missing version"}]
        invalid += [{**body, "plan_version": value} for value in [True, False, "1", 1.0, 0, -1, None]]
        invalid += [{**body, "feedback": value} for value in [None, 42, [], "", " \t\n", "x\x00y", "x" * 4001, " " * 4000 + "x"]]
        invalid += [{**body, "approved_files": []}, {**body, "plan": {}}, {**body, "unexpected": "secret"}]
        for value in invalid:
            response = await client.post(f"/api/v1/tasks/{task_id}/feedback", json=value)
            assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
            assert "never-echo-this-feedback" not in response.text
        for headers, expected in [({"Origin": "https://untrusted.invalid"}, 403), ({"Host": "untrusted.invalid"}, 400)]:
            assert (await client.post(f"/api/v1/tasks/{task_id}/feedback", json=body, headers=headers)).status_code == expected
        missing = await client.post("/api/v1/tasks/absent/feedback", json=body)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "unknown_task"
        assert (await client.get(f"/api/v1/tasks/{task_id}")).json() == before
        assert services[0].snapshot() == service_before and len(services[0].llm.requests) == 2
        assert app.state.task_host.store.trace_page(task_id, limit=100) == trace_before
        boundary = await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": " " * 2000 + "x" + " " * 1999})
        assert boundary.status_code == 202
        assert (await until(client, task_id, {"waiting_approval"}))["plan_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,status,code", [
    ("version", 409, "stale_plan"), ("budget", 409, "step_limit"),
    ("approved", 409, "already_approved"), ("state", 409, "invalid_state"),
    ("cancel", 409, "invalid_state"), ("stop", 503, "server_stopping"),
    ("storage", 503, "storage_unavailable"),
])
async def test_feedback_http_rejections_do_not_mutate_or_call_model(cpp_repo, rg_exe, git_exe, scenario, status, code):
    async with client_for(rg_exe, git_exe) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        host = app.state.task_host
        async def prepare():
            entry = host._tasks[task_id]
            if scenario == "budget": entry.service._context.steps = 30
            elif scenario == "approved": entry.service.approve(task_id, 1)
            elif scenario == "state": entry.service._context.state = TaskState.PLANNING
            elif scenario == "cancel": entry.cancel_requested = True
            elif scenario == "stop": host._stopping = True
            elif scenario == "storage": host.store.mark_task_unavailable(task_id)
            return entry.service.snapshot()
        before = await host._submit(prepare())
        response = await client.post(f"/api/v1/tasks/{task_id}/feedback", json={
            "plan_version": 2 if scenario == "version" else 1, "feedback": "private feedback must not appear in error"})
        assert response.status_code == status and response.json()["error"]["code"] == code
        assert "private feedback" not in response.text
        assert services[0].snapshot() == before and len(services[0].llm.requests) == 2
        if scenario == "storage":
            # This case injected only the availability guard, not a real failed publication.
            host.store._unavailable_tasks.discard(task_id)


@pytest.mark.asyncio
async def test_feedback_http_waiter_cancel_does_not_cancel_accepted_work(cpp_repo, rg_exe, git_exe, monkeypatch):
    accepted, release = Event(), Event()
    async with client_for(rg_exe, git_exe, replies=messages()[:2] + [reply("analysis"), messages()[1]]) as (client, app, services):
        task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
        await until(client, task_id, {"waiting_approval"})
        host = app.state.task_host
        feedback = host._feedback
        async def delayed_response(*args):
            result = await feedback(*args)
            accepted.set()
            assert await asyncio.to_thread(release.wait, 10)
            return result
        monkeypatch.setattr(host, "_feedback", delayed_response)
        posting = asyncio.create_task(client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Revise"}))
        try:
            assert await asyncio.to_thread(accepted.wait, 2)
            posting.cancel()
            with pytest.raises(asyncio.CancelledError): await posting
            reviewed = await until(client, task_id, {"waiting_approval"})
            assert reviewed["plan_version"] == 2 and not reviewed["cancel_requested"]
            assert len(services[0].llm.requests) == 4
        finally:
            release.set()


@pytest.mark.asyncio
async def test_feedback_live_http_and_sse_disconnect_then_reconnect(cpp_repo, rg_exe, git_exe):
    from test_api_network import serving, next_block
    entered, release = Event(), Event()
    class FeedbackLLM(ScriptedLLM):
        async def chat(self, history, tools=None):
            if len(self.requests) == 2:
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
            return await super().chat(history, tools)
    def factory(trace):
        return TaskService(FeedbackLLM(messages()[:2] + [reply("analysis"), messages()[1]]),
            rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    with serving(factory) as (url, app):
        try:
            async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
                task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
                await until(client, task_id, {"waiting_approval"})
                response = await client.post(f"/api/v1/tasks/{task_id}/feedback", json={"plan_version": 1, "feedback": "Revise"})
                assert response.status_code == 202
                assert await asyncio.to_thread(entered.wait, 2)
                async with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as stream:
                    lines = stream.aiter_lines()
                    cursor = None
                    while cursor is None:
                        items = frames(await next_block(lines))
                        if items and items[-1][1]["event"] == "task_state_changed" and items[-1][1]["data"]["state"] == "planning":
                            cursor = items[-1][0]
            # Both real HTTP and SSE connections are closed while the model is held.
            release.set()
            async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
                reviewed = await until(client, task_id, {"waiting_approval"})
                assert reviewed["plan_version"] == 2 and not reviewed["cancel_requested"]
                async with client.stream("GET", f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": cursor}) as stream:
                    lines, seen = stream.aiter_lines(), []
                    while not any(p["event"] == "approval_required" and p["data"]["plan_version"] == 2 for _, p in seen):
                        seen += frames(await next_block(lines))
                    assert sum(p["event"] == "plan_created" and p["data"]["plan_version"] == 2 for _, p in seen) == 1
                await client.post(f"/api/v1/tasks/{task_id}/cancel")
        finally:
            release.set()
