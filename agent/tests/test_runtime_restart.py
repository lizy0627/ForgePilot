"""A desktop restart freezes new work only after the shared runtime is idle."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event
from uuid import uuid4

import httpx
import pytest

from forgepilot.api import create_app
from forgepilot.chat_service import ChatService
from forgepilot.mission_events import publish_mission_event
from forgepilot.sqlite_store import SqliteEventStore


STATUS = "/api/v2/runtime/status"
PREPARE = "/api/v2/runtime/prepare-restart"
TOKEN = "offline-runtime-control-token"


@asynccontextmanager
async def runtime_api(tmp_path, *, chat_factory=None):
    store = SqliteEventStore(tmp_path / "runtime.sqlite3")
    app = create_app(lambda trace: pytest.fail("Restart control must not create a task service"),
        store=store, chat_factory=chat_factory, api_token=TOKEN)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1", headers={"x-forgepilot-token": TOKEN}) as client:
                yield client, app
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["v1", "chat", "mission_admission"])
async def test_restart_refuses_reserved_work_before_durable_admission(tmp_path, kind):
    async with runtime_api(tmp_path) as (client, app):
        host = app.state.task_host
        entered, release = Event(), Event()

        async def reserve():
            host.capacity.acquire(kind, "pending-request", message="fixture capacity")
            try:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.002)
            finally:
                host.capacity.release(kind, "pending-request")

        pending = asyncio.create_task(host.runtime.submit(reserve()))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get(STATUS)).json() == {"active_count": 1, "restarting": False}
            refused = await client.post(PREPARE, json={})
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "runtime_busy"
            assert not host.runtime.stopping
        finally:
            release.set()
            await pending
        assert (await client.post(PREPARE, json={})).json() == {"active_count": 0, "restarting": True}


@pytest.mark.asyncio
async def test_restart_counts_persisted_mission_after_host_reconstruction(tmp_path):
    async with runtime_api(tmp_path) as (client, app):
        host = app.state.task_host
        task_id = str(uuid4())

        async def admit():
            host.store.missions.create_graph_run(task_id=task_id, graph_id="runtime-fixture", graph_version=1,
                                                status="awaiting_human")
            publish_mission_event(host.store, task_id, "task.created", {"request": "persisted fixture"})

        await host.runtime.submit(admit())
        assert not app.state.mission_host._services
        assert (await client.get(STATUS)).json() == {"active_count": 1, "restarting": False}
        refused = await client.post(PREPARE, json={})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "runtime_busy"

        async def finish():
            record = host.store.missions.get_graph_run(task_id)
            host.store.missions.update_graph_run(task_id, previous_revision=record["current_revision"], status="cancelled")

        await host.runtime.submit(finish())
        assert (await client.post(PREPARE, json={})).json() == {"active_count": 0, "restarting": True}


@pytest.mark.asyncio
async def test_active_chat_must_finish_or_cancel_before_restart(tmp_path):
    entered = Event()

    class BlockingModel:
        async def chat(self, messages, tools=None):
            entered.set()
            await asyncio.Future()

        async def aclose(self):
            pass

    async with runtime_api(tmp_path, chat_factory=lambda: ChatService(BlockingModel())) as (client, app):
        cid = (await client.post("/api/v1/conversations", json={})).json()["conversation_id"]
        rid = str(uuid4())
        submitted = await client.post(f"/api/v1/conversations/{cid}/requests", json={
            "request_id": rid, "mode": "chat", "tool_mode": "fast", "prompt": "offline fixture"})
        assert submitted.status_code == 202, submitted.text
        assert await asyncio.to_thread(entered.wait, 2)
        assert (await client.get(STATUS)).json() == {"active_count": 1, "restarting": False}
        refused = await client.post(PREPARE, json={})
        assert refused.status_code == 409 and not app.state.task_host.runtime.stopping
        stopped = await client.post(f"/api/v1/conversations/{cid}/requests/{rid}/stop")
        assert stopped.status_code == 200, stopped.text
        assert (await client.post(PREPARE, json={})).json() == {"active_count": 0, "restarting": True}
        saved = (await client.get(f"/api/v1/conversations/{cid}/requests/{rid}")).json()
        assert saved["status"] == "cancelled"


@pytest.mark.asyncio
async def test_idle_restart_is_idempotent_and_blocks_all_new_execution(tmp_path):
    async with runtime_api(tmp_path) as (client, app):
        cid = (await client.post("/api/v1/conversations", json={"title": "existing conversation"})).json()["conversation_id"]
        assert (await client.get(STATUS)).json() == {"active_count": 0, "restarting": False}
        for _ in range(2):
            response = await client.post(PREPARE, json={})
            assert response.status_code == 200, response.text
            assert response.json() == {"active_count": 0, "restarting": True}
        assert (await client.get(STATUS)).json() == {"active_count": 0, "restarting": True}
        bodies = (
            ("/api/v1/tasks", {"repository_path": str(tmp_path), "prompt": "must not start"}),
            (f"/api/v1/conversations/{cid}/requests", {
                "request_id": str(uuid4()), "mode": "chat", "prompt": "must not start"}),
            ("/api/v2/missions", {"repository_path": str(tmp_path), "request": "must not start"}),
        )
        for url, body in bodies:
            response = await client.post(url, json=body)
            assert response.status_code == 503, (url, response.text)
            assert response.json()["error"]["code"] == "server_stopping"
        assert (await client.get(f"/api/v1/conversations/{cid}")).json()["title"] == "existing conversation"
        assert app.state.task_host.capacity.active_count() == 0


@pytest.mark.asyncio
async def test_restart_control_requires_local_token_and_empty_request(tmp_path):
    async with runtime_api(tmp_path) as (client, app):
        for url, method in ((STATUS, "GET"), (PREPARE, "POST")):
            refused = await client.request(method, url, json={} if method == "POST" else None,
                                          headers={"x-forgepilot-token": "wrong-token"})
            assert refused.status_code == 401
        refused = await client.post(PREPARE, json={"active_count": 0})
        assert refused.status_code == 422, refused.text
        assert not app.state.task_host.runtime.stopping


@pytest.mark.asyncio
async def test_concurrent_restart_preparations_share_one_admission_fence(tmp_path):
    async with runtime_api(tmp_path) as (client, app):
        results = await asyncio.gather(*(client.post(PREPARE, json={}) for _ in range(4)))
        assert all(result.status_code == 200 for result in results)
        assert all(result.json() == {"active_count": 0, "restarting": True} for result in results)
        assert app.state.task_host.runtime.stopping
