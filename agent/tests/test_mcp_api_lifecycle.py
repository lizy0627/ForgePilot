"""HTTP disconnects, worker lifecycle, admission and optimistic update races."""
import asyncio
import json
import os
from threading import Event
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from test_mcp_http import peers
from test_mcp_service import candidate, SECRET, assert_no_plaintext
from test_mcp_api import serving, saved, eventually, error, PREFIX
from forgepilot.chat_service import ChatService
from test_conversations import RecallModel, send


class Gate:
    """Test-only delayed delivery of real discovery, safe across both loops."""
    def __init__(self, trace=None):
        self.trace = trace
        self.enabled = False
        self.arrivals = []
        self.release = Event()

    def configure(self, app):
        service = app.state.task_host.mcp_service
        factory = service.connection_factory
        gate = self

        def connect(config):
            client = factory(config)
            class Connection:
                @property
                def server_info(self):
                    return client.server_info

                async def start(self):
                    await client.start()

                async def list_tools(self):
                    try:
                        return await client.list_tools()
                    finally:
                        if gate.enabled:
                            if gate.trace:
                                gate.trace.event("gate.arrived")
                            gate.arrivals.append(True)
                            while not gate.release.is_set():
                                await asyncio.sleep(0.005)
                            if gate.trace:
                                gate.trace.event("gate.released")

                async def close(self):
                    await client.close()
            return Connection()
        service.connection_factory = connect


@pytest.fixture
def editor_trace():
    path = os.environ.get("FORGEPILOT_MCP_EDITOR_TRACE")
    if not path:
        yield None
        return
    from mcp_editor_trace import EditorTrace
    trace = EditorTrace(path)
    try:
        yield trace
    finally:
        trace.write()


@pytest.mark.asyncio
async def test_real_http_two_editors_one_revision_and_capacity_is_shared(peers, tmp_path, editor_trace, monkeypatch):
    peer = await peers()
    gate = Gate(editor_trace)

    def configure(app):
        if editor_trace:
            from mcp_editor_server_trace import install_server_trace
            editor_trace.install_asgi(app)
            install_server_trace(editor_trace, monkeypatch, app)
        gate.configure(app)

    with serving(tmp_path, configure=configure) as (url, app, store):
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            record = await saved(client, peer)
            initial_connections = len(app.state.mcp_clients)
            assert all(connection.owner.done() and connection.transport.closed
                       for connection in app.state.mcp_clients)
            # Client close can finish before this loop records its DELETE.
            # Observe old-session cleanup before forbidding any new non-GET.
            await eventually(lambda: sum(r["http"] == "DELETE" for r in peer.records) == initial_connections)
            endpoint = PREFIX + "/servers/" + record["server_id"]
            gate.enabled = True
            if editor_trace:
                editor_trace.track("gate-coordinator", asyncio.current_task())
            tasks = []
            for name in ("editor-a", "editor-b"):
                options = editor_trace.request_options(name) if editor_trace else {}
                body = {**candidate(peer, name=name), "base_revision": 1, "catalog_hash": record["catalog_hash"]}
                tasks.append(asyncio.create_task(client.put(endpoint, json=body, **options)))
            try:
                await eventually(lambda: len(gate.arrivals) == 2)
                assert len(app.state.task_host._mcp_active) == 2 and not store._db.in_transaction
                count = sum(r['http'] != 'GET' for r in peer.records)
                connections = len(app.state.mcp_clients)
                assert connections == initial_connections + 2
                # The SDK retries the optional GET stream after a 405. Let that
                # happen while both checks are held; it does not open a new MCP session.
                await asyncio.sleep(1.1)
                for method, path, body in (("POST", PREFIX + "/probe", candidate(peer)),
                        ("POST", endpoint + "/test", {"revision": 1}),
                        ("PATCH", endpoint, {"revision": 1, "enabled": True})):
                    error(await client.request(method, path, json=body), 429, "mcp_capacity")
                assert sum(r['http'] != 'GET' for r in peer.records) == count
                assert len(app.state.mcp_clients) == connections  # No third connection or RPC dispatch.
                assert (await client.get(endpoint)).status_code == 200
                assert (await client.post("/api/v1/conversations", json={"title": "during checks"})).status_code == 201
            finally:
                if editor_trace:
                    editor_trace.event("gate.release_set")
                gate.release.set()
                # Observe both terminal results even if a capacity assertion or one PUT fails.
                # task.result() below still raises the original PUT exception.
                await asyncio.gather(*tasks, return_exceptions=True)
            results = [task.result() for task in tasks]
            assert sorted(response.status_code for response in results) == [200, 409]
            winner = next(response.json() for response in results if response.status_code == 200)
            error(next(response for response in results if response.status_code == 409), 409, "mcp_config_conflict")
            assert (await client.get(endpoint)).json() == winner and winner["revision"] == 2
            assert not app.state.task_host._mcp_active
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "save", "test", "failed_test", "enable"])
async def test_real_http_stale_network_results_cannot_overwrite_update_or_delete(peers, tmp_path, operation):
    peer = await peers()
    gate = Gate()
    with serving(tmp_path, configure=gate.configure) as (url, app, _):
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            record = await saved(client, peer, config=candidate(peer, enabled=operation != "enable"))
            endpoint = PREFIX + "/servers/" + record["server_id"]
            gate.enabled = True
            if operation == "probe":
                action = client.post(PREFIX + "/probe", json={**candidate(peer), "server_id": record["server_id"], "base_revision": 1})
            elif operation == "save":
                action = client.put(endpoint, json={**candidate(peer, name="stale"), "base_revision": 1, "catalog_hash": record["catalog_hash"]})
            elif operation == "enable":
                action = client.patch(endpoint, json={"revision": 1, "enabled": True})
            else:
                if operation == "failed_test":
                    peer.mode, peer.fail_on = "401", "tools/list"
                action = client.post(endpoint + "/test", json={"revision": 1})
            pending = asyncio.create_task(action)
            try:
                await eventually(lambda: gate.arrivals)
                if operation == "enable":
                    assert (await client.delete(endpoint + "?revision=1")).status_code == 200
                else:
                    assert (await client.patch(endpoint, json={"revision": 1, "enabled": False})).status_code == 200
                before = app.state.task_host.mcp_service.store.get(record["server_id"])
            finally:
                gate.release.set()
            error(await pending, 409, "mcp_config_conflict")
            assert app.state.task_host.mcp_service.store.get(record["server_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "save", "test", "enable"])
async def test_real_http_disconnect_cancels_remote_session_and_retains_old_config(peers, tmp_path, operation):
    peer = await peers(auth="bearer")
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            record = await saved(client, peer, config=candidate(peer, enabled=operation != "enable"))
            endpoint = PREFIX + "/servers/" + record["server_id"]
            old = app.state.task_host.mcp_service.store.get(record["server_id"])
            peer.mode, peer.fail_on = "hang", "tools/list"
            if operation == "probe":
                method, path, body = "POST", PREFIX + "/probe", {**candidate(peer), "secret_updates": {"api_key": SECRET}}
            elif operation == "save":
                method, path, body = "PUT", endpoint, {**candidate(peer, name="abandoned"), "base_revision": 1, "catalog_hash": record["catalog_hash"]}
            elif operation == "enable":
                method, path, body = "PATCH", endpoint, {"revision": 1, "enabled": True}
            else:
                method, path, body = "POST", endpoint + "/test", {"revision": 1}
            authority = urlsplit(url).netloc
            reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(url).port)
            raw = json.dumps(body).encode()
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw)
            await writer.drain()
            async with asyncio.timeout(6):
                await peer.streaming.wait()
                writer.close()  # Explicit TCP FIN, independent of client-library cancellation.
                await writer.wait_closed()
                await peer.disconnected.wait()
                await eventually(lambda: not app.state.task_host._mcp_active)
            assert app.state.task_host.mcp_service.store.get(record["server_id"]) == old
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)
            assert_no_plaintext(store, SECRET)
            peer.mode = "normal"
            assert (await client.post(endpoint + "/test", json={"revision": 1})).status_code == 200


@pytest.mark.asyncio
async def test_cleanup_holds_both_admission_slots_until_owners_finish(peers, tmp_path):
    peer = await peers(mode="delete_hang")
    with serving(tmp_path) as (url, app, _):
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            tasks = [asyncio.create_task(client.post(PREFIX + "/probe", json=candidate(peer))) for _ in range(2)]
            await eventually(lambda: sum(r["http"] == "DELETE" for r in peer.records) == 2)
            assert len(app.state.task_host._mcp_active) == 2
            error(await client.post(PREFIX + "/probe", json=candidate(peer)), 429, "mcp_capacity")
            started = time_now()
            results = await asyncio.gather(*tasks)
            assert time_now() - started < 3.1 and all(r.status_code == 200 for r in results)
            assert not app.state.task_host._mcp_active
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)


def time_now():
    return asyncio.get_running_loop().time()


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_and_joins_management_connections(peers, tmp_path):
    peer = await peers(mode="hang", auth="bearer")
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            pending = asyncio.create_task(client.post(PREFIX + "/probe", json={**candidate(peer), "secret_updates": {"api_key": SECRET}}))
            await peer.streaming.wait()
            start = time_now()
            await app.state.task_host._submit(app.state.task_host._shutdown())
            assert time_now() - start < 3.2
            error(await pending, 503, "server_stopping")
            assert not app.state.task_host._mcp_active
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)
            assert_no_plaintext(store, SECRET)


@pytest.mark.asyncio
async def test_actual_http_server_shutdown_joins_hanging_probe(peers, tmp_path, caplog):
    peer = await peers(mode="hang", auth="bearer")
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url, timeout=8) as client:
            pending = asyncio.create_task(client.post(PREFIX + "/probe", json={**candidate(peer), "secret_updates": {"api_key": SECRET}}))
            await peer.streaming.wait()
            app.state.http_server.should_exit = True
            try:
                response = await pending
                # A transport forced down by Uvicorn need not deliver a response.
                assert response.status_code >= 499
            except httpx.TransportError:
                pass
            await eventually(lambda: not app.state.task_host._thread.is_alive(), timeout=6)
            assert not app.state.task_host._mcp_active
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)
            assert_no_plaintext(store, SECRET)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_disabled_startup_allows_read_disable_delete_but_no_network(peers, tmp_path):
    peer = await peers()
    with serving(tmp_path) as (url, _, _):
        async with httpx.AsyncClient(base_url=url) as client:
            record = await saved(client, peer)
    count = len(peer.records)
    with serving(tmp_path, disabled=True) as (url, app, _):
        async with httpx.AsyncClient(base_url=url) as client:
            endpoint = PREFIX + "/servers/" + record["server_id"]
            assert (await client.get(PREFIX + "/servers")).json()["disabled"] is True
            assert (await client.get(endpoint)).json() == record
            assert (await client.get(PREFIX + "/presets")).status_code == 200
            requests = [("POST", PREFIX + "/probe", candidate(peer)),
                ("POST", endpoint + "/test", {"revision": 1}),
                ("PATCH", endpoint, {"revision": 1, "enabled": True}),
                ("PUT", endpoint, {**candidate(peer, enabled=False), "base_revision": 1, "catalog_hash": record["catalog_hash"]})]
            for method, path, body in requests:
                error(await client.request(method, path, json=body), 403, "mcp_disabled")
            assert (await client.patch(endpoint, json={"revision": 1, "enabled": False})).status_code == 200
            assert (await client.delete(endpoint + "?revision=2")).status_code == 200
            assert not app.state.mcp_clients and len(peer.records) == count


@pytest.mark.asyncio
async def test_mcp_failure_does_not_block_plain_chat_or_expose_management_tools(peers, tmp_path):
    peer = await peers(mode="hang")
    records = []
    with serving(tmp_path, chat_factory=lambda: ChatService(RecallModel(records)),
                 configure=lambda app: setattr(app.state.task_host.mcp_service, "probe_timeout", 1.0)) as (url, app, _):
        async with httpx.AsyncClient(base_url=url) as client:
            assert not app.state.mcp_clients
            pending = asyncio.create_task(client.post(PREFIX + "/probe", json=candidate(peer)))
            await peer.streaming.wait()
            cid = (await client.post("/api/v1/conversations", json={})).json()["conversation_id"]
            answer = await send(client, cid, "普通问答仍然可用")
            assert answer["status"] == "completed" and records and not records[0][1]
            error(await pending, 504, "mcp_timeout")
