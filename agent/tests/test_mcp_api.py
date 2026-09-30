"""Real loopback FastAPI + real MCP sockets/stdio + temporary SQLite/DPAPI."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
from pathlib import Path
import socket
import sys
from threading import Thread, current_thread
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn

from test_mcp_http import peers
from test_mcp_service import candidate, SECRET, REPLACEMENT, MODEL_SECRET, assert_no_plaintext, probe_only
from forgepilot.api import create_app
from forgepilot.mcp_config import create_mcp_connection
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from test_mcp import PEER, closed
import forgepilot.mcp_service as service_module

PREFIX = "/api/v1/mcp"


@contextmanager
def serving(tmp_path, *, disabled=False, configure=None, chat_factory=None):
    store = SqliteEventStore(tmp_path / "api.sqlite3", redactor=Redactor((MODEL_SECRET,)))
    def no_task(trace):
        raise AssertionError("MCP management must not construct an Agent or LLM")
    app = create_app(no_task, store=store, mcp_disabled=disabled, chat_factory=chat_factory)
    host = app.state.task_host
    clients, threads = [], []
    def connection(config):
        threads.append(current_thread().name)
        client = create_mcp_connection(config)
        clients.append(client)
        return client
    host.mcp_service.connection_factory = connection
    app.state.mcp_clients, app.state.mcp_threads = clients, threads
    if configure:
        configure(app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", timeout_graceful_shutdown=1))
    app.state.http_server = server
    thread = Thread(target=lambda: server.run(sockets=[sock]), name="MCP-Test-HTTP", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield url, app, store
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive() and not host._thread.is_alive()
        store.close()


async def saved(client, peer, *, config=None, sid=None):
    config = config or candidate(peer)
    secrets = {"api_key": peer.expected_secret} if peer.auth != "none" else {}
    probe = await client.post(PREFIX + "/probe", json={**config, "secret_updates": secrets})
    assert probe.status_code == 200, probe.text
    sid = sid or str(uuid4())
    response = await client.put(PREFIX + "/servers/" + sid, json={**config, "base_revision": 0,
        "catalog_hash": probe.json()["catalog_hash"], "secret_updates": secrets})
    assert response.status_code == 201, response.text
    return response.json()


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert set(body["error"]) == {"code", "message", "details", "task_id"}
    for secret in (SECRET, REPLACEMENT, MODEL_SECRET):
        assert secret not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("auth,sse", [("none", False), ("bearer", True), ("api_key", False)])
async def test_real_http_crud_reopen_and_safe_contract(peers, tmp_path, auth, sse):
    peer = await peers(auth=auth, sse=sse)
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            presets = (await client.get(PREFIX + "/presets")).json()["items"]
            assert len(presets) == 1 and presets[0]["real_service_verified"] is False
            empty = (await client.get(PREFIX + "/servers")).json()
            assert empty == {"items": [], "disabled": False}
            record = await saved(client, peer)
            sid = record["server_id"]
            endpoint = PREFIX + "/servers/" + sid
            assert record["revision"] == 1 and record["has_secret"] == (auth != "none")
            assert "secret_updates" not in record and "secrets_dpapi" not in record
            assert "secret" not in record["connection"]
            assert (await client.get(endpoint)).json() == record
            listing = (await client.get(PREFIX + "/servers")).json()
            assert "tools" not in listing["items"][0] and "connection" not in listing["items"][0]
            checked = await client.post(endpoint + "/test", json={"revision": 1})
            assert checked.status_code == 200 and checked.json()["revision"] == 1
            assert checked.json()["updated_at"] == record["updated_at"]
            changed = await client.put(endpoint, json={**candidate(peer, name="changed"), "base_revision": 1,
                "catalog_hash": record["catalog_hash"]})  # Omission preserves credentials.
            assert changed.status_code == 200 and changed.json()["revision"] == 2
            disabled = await client.patch(endpoint, json={"revision": 2, "enabled": False})
            assert disabled.status_code == 200 and disabled.json()["revision"] == 3
            enabled = await client.patch(endpoint, json={"revision": 3, "enabled": True})
            assert enabled.status_code == 200 and enabled.json()["revision"] == 4
            assert_no_plaintext(store, SECRET, MODEL_SECRET)
            assert all(thread == "ForgePilot-Runtime" for thread in app.state.mcp_threads)
            assert all(c.owner.done() and c.transport.closed and c.owner.get_loop() is app.state.task_host._loop
                       for c in app.state.mcp_clients)
            count = len(peer.records)
            # Retrying the same creation UUID after a lost response cannot duplicate.
            retried = await client.put(endpoint, json={**candidate(peer), "base_revision": 0,
                "catalog_hash": record["catalog_hash"]})
            error(retried, 409, "mcp_config_conflict")
            assert len(peer.records) == count
    with serving(tmp_path) as (url, _, store):
        async with httpx.AsyncClient(base_url=url) as client:
            assert (await client.get(endpoint)).json() == enabled.json()
            error(await client.delete(endpoint + "?revision=3"), 409, "mcp_config_conflict")
            deleted = await client.delete(endpoint + "?revision=4")
            assert deleted.status_code == 200 and deleted.json() == {"server_id": sid, "deleted": True}
            error(await client.get(endpoint), 404, "unknown_mcp_server")
            error(await client.post(endpoint + "/test", json={"revision": 4}), 404, "unknown_mcp_server")
            assert (await client.get(PREFIX + "/servers")).json()["items"] == []
    probe_only(peer)


@pytest.mark.asyncio
async def test_real_http_key_replacement_clear_and_failed_save_keep_old_values(peers, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(auth="bearer")
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            record = await saved(client, peer)
            endpoint = PREFIX + "/servers/" + record["server_id"]
            old = app.state.task_host.mcp_service.store.get(record["server_id"])
            config = {**candidate(peer), "base_revision": 1, "catalog_hash": record["catalog_hash"]}
            failed = await client.put(endpoint, json={**config, "secret_updates": {"api_key": REPLACEMENT}})
            error(failed, 422, "mcp_auth")
            assert app.state.task_host.mcp_service.store.get(record["server_id"]) == old
            assert_no_plaintext(store, SECRET, REPLACEMENT, MODEL_SECRET)
            peer.expected_secret = REPLACEMENT
            changed = await client.put(endpoint, json={**config, "secret_updates": {"api_key": REPLACEMENT}})
            assert changed.status_code == 200 and changed.json()["revision"] == 2
            checked = await client.post(endpoint + "/test", json={"revision": 2})
            assert checked.status_code == 200
            error(await client.put(endpoint, json={**config, "base_revision": 2,
                "secret_updates": {"api_key": None}}), 422, "mcp_secret_required")
            peer.auth = "none"
            cleared = await client.put(endpoint, json={**candidate(peer), "base_revision": 2,
                "catalog_hash": record["catalog_hash"], "secret_updates": {"api_key": None}})
            assert cleared.status_code == 200 and not cleared.json()["has_secret"]
            start = len(peer.records)
            assert (await client.post(endpoint + "/test", json={"revision": 3})).status_code == 200
            assert all("authorization" not in row["headers"] for row in peer.records[start:])
            assert_no_plaintext(store, SECRET, REPLACEMENT, MODEL_SECRET)
    assert SECRET not in caplog.text and REPLACEMENT not in caplog.text and MODEL_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_api_cannot_forward_old_key_to_new_endpoint(peers, tmp_path):
    a, b = await peers(auth="bearer"), await peers(auth="bearer")
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            record = await saved(client, a)
            sid = record["server_id"]
            before = app.state.task_host.mcp_service.store.get(sid)
            changed = {**candidate(b), "base_revision": 1}
            error(await client.post(PREFIX + "/probe", json={**changed, "server_id": sid}), 422, "mcp_secret_required")
            error(await client.put(PREFIX + "/servers/" + sid, json={**changed, "catalog_hash": record["catalog_hash"]}), 422, "mcp_secret_required")
            assert b.records == []
            assert app.state.task_host.mcp_service.store.get(sid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status,code", [("401",422,"mcp_auth"), ("403",422,"mcp_auth"),
    ("429",429,"mcp_capacity"), ("html",422,"mcp_not_mcp"), ("version",422,"mcp_version"),
    ("malformed",422,"mcp_protocol"), ("catalog_limit",422,"mcp_limit"), ("hang",504,"mcp_timeout")])
async def test_real_http_errors_no_candidate_persistence_or_key_echo(peers, tmp_path, mode, status, code):
    peer = await peers(mode=mode, auth="bearer")
    with serving(tmp_path, configure=lambda app: setattr(app.state.task_host.mcp_service, "probe_timeout", 0.5)) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            response = await client.post(PREFIX + "/probe", json={**candidate(peer), "secret_updates": {"api_key": SECRET}})
            error(response, status, code)
            assert (await client.get(PREFIX + "/servers")).json()["items"] == []
            assert not app.state.task_host._mcp_active
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)
            assert_no_plaintext(store, SECRET)
            if mode == "hang":
                async with asyncio.timeout(2):
                    await peer.disconnected.wait()
    probe_only(peer)


@pytest.mark.asyncio
async def test_real_http_input_security_and_json_request_limit(peers, tmp_path):
    peer = await peers()
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            path = PREFIX + "/probe"
            body = candidate(peer)
            error(await client.post(path, json=body, headers={"Origin": "https://evil.invalid"}), 403, "origin_not_allowed")
            error(await client.get(PREFIX + "/servers", headers={"Host": "evil.invalid"}), 400, "host_not_allowed")
            error(await client.post(path, content=json.dumps(body)), 415, "invalid_request")
            error(await client.post(path, content='{bad ' + SECRET, headers={"Content-Type": "application/json"}), 422, "invalid_request")
            for extra in ({SECRET: SECRET}, {"secret_updates": {SECRET: 123}}, {"effect": "write"},
                          {"enabled": 1}, {"base_revision": True}, {"connection": {"command": "x.exe"}},
                          {"selected_tools": [123]}, {"catalog_hash": SECRET}):
                error(await client.post(path, json={**body, **extra}), 422, "invalid_request")
            for raw in ('{"transport":"stdio","transport":"streamable_http"}', '{"base_revision":NaN}'):
                error(await client.post(path, content=raw, headers={"Content-Type":"application/json"}), 422, "invalid_request")
            async def chunks():
                for _ in range(66):
                    yield b"x" * 8192
            error(await client.post(path, content=chunks(), headers={"Content-Type":"application/json"}), 413, "mcp_limit")
            error(await client.get(PREFIX + "/servers/" + SECRET), 422, "invalid_request")
            assert not peer.records and not app.state.mcp_clients
            assert_no_plaintext(store, SECRET)


@pytest.mark.asyncio
async def test_real_http_stdio_arrays_environment_and_missing_executable(tmp_path):
    log = tmp_path / "本地 参数记录.jsonl"
    config = {"name": "本地查询", "transport": "stdio", "modes": ["chat"], "selected_tools": ["search"],
        "connection": {"command": sys.executable, "args": ["-I", "-u", str(PEER), "generic", str(log)],
                       "env_names": ["MCP_TEST_TOKEN"]}}
    with serving(tmp_path) as (url, app, store):
        async with httpx.AsyncClient(base_url=url) as client:
            probe = await client.post(PREFIX + "/probe", json={**config, "secret_updates": {"env:MCP_TEST_TOKEN": SECRET}})
            assert probe.status_code == 200, probe.text
            assert probe.json()["connection"]["cwd"] == str(Path(sys.executable).parent)
            endpoint = PREFIX + "/servers/" + str(uuid4())
            saved_response = await client.put(endpoint, json={**config, "base_revision": 0,
                "catalog_hash": probe.json()["catalog_hash"], "secret_updates": {"env:MCP_TEST_TOKEN": SECRET}})
            assert saved_response.status_code == 201, saved_response.text
            assert saved_response.json()["connection"]["env_names"] == ["MCP_TEST_TOKEN"]
            assert SECRET not in saved_response.text
            assert (await client.post(endpoint + "/test", json={"revision": 1})).status_code == 200
            # Removing the environment declaration also clears the encrypted value.
            config["connection"]["env_names"] = []
            cleared = await client.put(endpoint, json={**config, "base_revision": 1, "catalog_hash": probe.json()["catalog_hash"]})
            assert cleared.status_code == 200 and not cleared.json()["has_secret"]
            before = (await client.get(endpoint)).json()
            config["connection"]["command"] = str(tmp_path / "missing.exe")
            error(await client.put(endpoint, json={**config, "base_revision": 2, "catalog_hash": probe.json()["catalog_hash"]}),
                  422, "mcp_executable_missing")
            assert (await client.get(endpoint)).json() == before
            for connection in app.state.mcp_clients:
                closed(connection)
            assert set(app.state.mcp_threads) == {"ForgePilot-Runtime"}
            assert_no_plaintext(store, SECRET)
    wire = log.read_text(encoding="utf-8")
    assert "tools/call" not in wire and SECRET not in wire


@pytest.mark.asyncio
async def test_real_http_incompatible_tool_metadata_remains_displayable(peers, tmp_path):
    peer = await peers(auth="bearer")
    template = deepcopy(peer.tools[0])
    peer.tools += [{**template, "name": "bad-task", "execution": {"taskSupport": {"bad": True}}},
                   {**template, "name": "bad-annotations", "annotations": False},
                   {**template, "name": "writes", "annotations": {"readOnlyHint": False}},
                   {**template, "name": "sensitive", "inputSchema": {"type": "object", "description": SECRET}}]
    peer.tools[0]["description"] += " " + SECRET + " " + MODEL_SECRET
    with serving(tmp_path) as (url, _, store):
        async with httpx.AsyncClient(base_url=url) as client:
            config = {**candidate(peer), "secret_updates": {"api_key": SECRET}}
            probe = await client.post(PREFIX + "/probe", json=config)
            assert probe.status_code == 200, probe.text
            assert len(probe.json()["tools"]) == 6
            for tool in probe.json()["tools"]:
                assert tool["compatible"] == (tool["name"] in ("search", "fetch"))
            assert SECRET not in probe.text and MODEL_SECRET not in probe.text
            endpoint = PREFIX + "/servers/" + str(uuid4())
            for selection in ("writes", "bad-task", "sensitive", "unknown"):
                error(await client.put(endpoint, json={**config, "selected_tools": [selection], "base_revision": 0,
                    "catalog_hash": probe.json()["catalog_hash"]}), 422, "mcp_selection")
            good = await client.put(endpoint, json={**config, "base_revision": 0, "catalog_hash": probe.json()["catalog_hash"]})
            assert good.status_code == 201 and len(good.content) < 524288
            assert SECRET not in good.text and MODEL_SECRET not in good.text
            assert_no_plaintext(store, SECRET, MODEL_SECRET)


@pytest.mark.asyncio
async def test_real_http_response_limit_failure_preserves_old_save(peers, tmp_path, monkeypatch):
    peer = await peers()
    with serving(tmp_path) as (url, app, _):
        async with httpx.AsyncClient(base_url=url) as client:
            record = await saved(client, peer)
            endpoint = PREFIX + "/servers/" + record["server_id"]
            before = app.state.task_host.mcp_service.store.get(record["server_id"])
            peer.tools[0]["description"] = "x" * 3000
            probe = await client.post(PREFIX + "/probe", json=candidate(peer))
            assert probe.status_code == 200
            monkeypatch.setattr(service_module, "MAX_RESPONSE_BYTES", len(probe.content) + 1)
            error(await client.put(endpoint, json={**candidate(peer), "base_revision": 1,
                "catalog_hash": probe.json()["catalog_hash"]}), 422, "mcp_limit")
            assert app.state.task_host.mcp_service.store.get(record["server_id"]) == before
            assert (await client.get(endpoint)).json() == record


def test_server_entrypoint_forwards_disable_without_reading_legacy_or_connecting(tmp_path, monkeypatch):
    from forgepilot import server
    monkeypatch.setenv("FORGEPILOT_API_KEY", MODEL_SECRET)
    monkeypatch.setenv("FORGEPILOT_MCP_CONFIG", str(tmp_path / "missing-legacy.json"))
    observed = []
    def uvicorn_run(app, **options):
        host = app.state.task_host
        observed.append(host.mcp_disabled)
        assert host._loop is None and not host._mcp_active
        assert host.mcp_service.list_servers() == {"items": []}
    monkeypatch.setattr(server.uvicorn, "run", uvicorn_run)
    assert server.main(["--disable-mcp", "--base-url", "http://127.0.0.1:9/v1", "--model", "offline-fixture",
                        "--database", str(tmp_path / "entry.sqlite3")]) == 0
    assert observed == [True]


def test_mcp_openapi_and_wire_examples_are_fixed_and_valid():
    from pydantic import TypeAdapter
    from forgepilot.api_models import (MCPProbeInput, MCPSaveInput, MCPProbeResponse, MCPServerDetail,
        MCPServerList, MCPEnableInput, MCPTestInput, MCPDeleted, ErrorResponse,
        ConversationMCPTools, ConversationRequestState)
    root = Path(__file__).parents[2] / "protocol"
    examples = json.loads((root / "examples.json").read_text(encoding="utf-8"))
    for name, model in (("mcp_probe_request", MCPProbeInput), ("mcp_probe_response", MCPProbeResponse),
            ("mcp_stdio_public_env_probe_request", MCPProbeInput),
            ("mcp_save_request", MCPSaveInput), ("mcp_edit_keep_secret_request", MCPSaveInput),
            ("mcp_clear_secret_request", MCPSaveInput), ("mcp_list_response", MCPServerList),
            ("mcp_detail_response", MCPServerDetail), ("mcp_disable_request", MCPEnableInput),
            ("mcp_test_request", MCPTestInput), ("mcp_deleted_response", MCPDeleted),
            ("mcp_conflict_error", ErrorResponse), ("mcp_disabled_error", ErrorResponse),
            ("conversation_mcp_tools", ConversationMCPTools),
            ("conversation_mcp_running", ConversationRequestState),
            ("conversation_mcp_cancelled", ConversationRequestState),
            ("conversation_mcp_legacy_empty", ConversationMCPTools)):
        TypeAdapter(model).validate_python(examples[name])
    schema = json.loads((root / "openapi.json").read_text(encoding="utf-8"))
    app = create_app(lambda trace: None)
    try:
        assert schema == app.openapi()
    finally:
        app.state.task_host.store.close()
    required = {"/presets": ["get"], "/servers": ["get"], "/probe": ["post"],
                "/servers/{server_id}": ["get", "put", "patch", "delete"], "/servers/{server_id}/test": ["post"]}
    for path, methods in required.items():
        for method in methods:
            operation = schema["paths"][PREFIX + path][method]
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]
            for status in ("403", "404", "409", "422", "429", "503", "504"):
                assert operation["responses"][status]["content"]["application/json"]["schema"] == {"$ref":"#/components/schemas/ErrorResponse"}
    put = schema["paths"][PREFIX + "/servers/{server_id}"]["put"]
    assert "201" in put["responses"]
    body = put["requestBody"]["content"]["application/json"]["schema"]
    assert body["discriminator"]["propertyName"] == "transport"
    for name in ("MCPHTTPSave", "MCPStdioSave", "MCPHTTPProbe", "MCPStdioProbe"):
        properties = schema["components"]["schemas"][name]["properties"]
        assert properties["secret_updates"]["writeOnly"] is True
    for name in ("MCPHTTPDetail", "MCPStdioDetail", "MCPServerSummary", "MCPProbeResponse"):
        properties = schema["components"]["schemas"][name]["properties"]
        assert not {"secret_updates", "secrets_dpapi", "secret", "api_key"} & properties.keys()
