"""Protocol/ownership tests against actual loopback sockets and the locked SDK."""
import asyncio
from copy import deepcopy
import json
import logging
from time import monotonic
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import pytest_asyncio

from fixtures.mcp_http_peer import HttpPeer, INPUT, OUTPUT, SECRET
from forgepilot.errors import ForgePilotError, MCPTransportError, MCPRequestError
from forgepilot.mcp_config import MCPHttpConfig, create_mcp_connection
from forgepilot.mcp_http import MCPHttpClient, _error
from forgepilot.mcp_schema import checked_schema, validate
from forgepilot.tools.mcp_tools import MCPToolAdapter, inspect_tool, tool_alias
from forgepilot.tools.registry import ToolRegistry


@pytest_asyncio.fixture
async def peers():
    opened = []
    async def create(**kwargs):
        peer = await HttpPeer(**kwargs).start()
        opened.append(peer)
        return peer
    yield create
    for peer in opened:
        await peer.close()


def config(peer, timeout=2.0):
    return MCPHttpConfig(endpoint=peer.endpoint, auth_type=peer.auth,
        header_name="X-API-Key" if peer.auth == "api_key" else None,
        secret=SECRET if peer.auth != "none" else None, request_timeout=timeout)


def adapter(client, definition, server_id):
    return MCPToolAdapter(server_id=server_id, remote_tool_name=definition["name"],
        input_schema=definition["inputSchema"], output_schema=definition.get("outputSchema"),
        description=definition.get("description", ""), connection=client,
        annotations=definition.get("annotations"), execution=definition.get("execution"))


def closed(client):
    assert client.owner.done()
    assert client.transport.closed and client.http.is_closed
    assert not any(not task.done() and (task.get_name() == "mcp-http-owner" or
        "mcp." in repr(task.get_coro())) for task in asyncio.all_tasks())


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [False, True])
async def test_remote_rpc_error_keeps_owner_and_queued_call_with_sdk_timeout_code(peers, sse, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(auth="bearer", sse=sse)
    # A remote error may legitimately use the same number as an SDK-local timeout.
    peer.rpc_error_code = 408
    peer.rpc_error_tool = "search"
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        peer.mode = "rpc_error"
        failed, succeeded = await asyncio.gather(client.call_tool("search", {"query": "first"}),
            client.call_tool("fetch", {"query": "second"}), return_exceptions=True)
        assert isinstance(failed, MCPRequestError) and failed.code == "mcp_request"
        assert SECRET not in str(failed) and SECRET not in caplog.text
        assert succeeded["isError"] is False
        assert client.failure is None and not client.owner.done()
    finally:
        await client.close()
    closed(client)
    assert sum(r["body"].get("method") == "initialize" for r in peer.records) == 1
    assert [r["body"]["params"]["name"] for r in peer.records if r["body"].get("method") == "tools/call"] == ["search", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "tools/list"])
@pytest.mark.parametrize("sse", [False, True])
async def test_http_lifecycle_rpc_error_still_closes_session(peers, method, sse):
    peer = await peers(auth="bearer", sse=sse, mode="phase_rpc_error", fail_on=method)
    client = MCPHttpClient(config(peer))
    if method == "tools/list":
        await client.start()
    with pytest.raises(ForgePilotError, match="mcp_request") as failed:
        await (client.start() if method == "initialize" else client.list_tools())
    assert SECRET not in str(failed.value)
    closed(client)
    assert not any(r["body"].get("method") == "tools/call" for r in peer.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("500", "mcp_transient"), ("401", "mcp_auth"),
    ("disconnect", "mcp_disconnected"), ("malformed", "mcp_protocol"), ("bad_error", "mcp_protocol")])
async def test_http_fault_after_business_rpc_error_is_still_fatal(peers, mode, code, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(auth="bearer", mode="rpc_error")
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        with pytest.raises(MCPRequestError):
            await client.call_tool("search", {"query": "first"})
        peer.mode, peer.fail_on = mode, "tools/call"
        with pytest.raises(ForgePilotError, match=code) as failed:
            await client.call_tool("fetch", {"query": "second"})
        assert not isinstance(failed.value, MCPRequestError)
        assert SECRET not in str(failed.value) and SECRET not in caplog.text
        closed(client)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_http_cancel_after_rpc_error_keeps_current_request_id(peers, cancel):
    peer = await peers(auth="bearer", mode="rpc_error")
    client = MCPHttpClient(config(peer, timeout=0.4 if not cancel else 3.0))
    try:
        await client.start()
        with pytest.raises(MCPRequestError):
            await client.call_tool("search", {"query": "first"})
        peer.mode, peer.fail_on = "hang", "tools/call"
        pending = asyncio.create_task(client.call_tool("fetch", {"query": "second"}))
        await peer.streaming.wait()
        if cancel:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ForgePilotError):
            await asyncio.wait_for(pending, 3)
        closed(client)
    finally:
        await client.close()
    calls = [r["body"] for r in peer.records if r["body"].get("method") == "tools/call"]
    cancelled = [r["body"] for r in peer.records if r["body"].get("method") == "notifications/cancelled"]
    assert len(calls) == 2 and len(cancelled) == 1
    assert cancelled[0]["params"]["requestId"] == calls[1]["id"] != calls[0]["id"]


@pytest.mark.asyncio
async def test_sdk_local_timeout_after_rpc_error_is_fatal_not_a_business_error(peers, monkeypatch):
    import forgepilot.mcp_http as module
    original = module.ClientSession
    class ShortReadSession(original):
        async def send_request(self, request, result_type, **kwargs):
            if request.root.method == "tools/call":
                kwargs["request_read_timeout_seconds"] = timedelta(seconds=0.1)
            return await super().send_request(request, result_type, **kwargs)
    monkeypatch.setattr(module, "ClientSession", ShortReadSession)
    peer = await peers(auth="bearer", mode="rpc_error")
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        with pytest.raises(MCPRequestError):
            await client.call_tool("search", {"query": "business failure"})
        assert not client.owner.done()
        peer.mode, peer.fail_on = "hang", "tools/call"
        with pytest.raises(ForgePilotError) as failed:
            await client.call_tool("fetch", {"query": "SDK read deadline"})
        assert not isinstance(failed.value, MCPRequestError)
        assert SECRET not in str(failed.value)
        closed(client)
    finally:
        await client.close()
    assert sum(r["body"].get("method") == "initialize" for r in peer.records) == 1
    assert [r["body"]["params"]["name"] for r in peer.records if r["body"].get("method") == "tools/call"] == ["search", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['seconds','date','past','invalid','missing'])
async def test_retry_after_safe_metadata_survives_transport(peers,kind):
    peer=await peers(mode='429')
    peer.retry_after={'seconds':'3','date':format_datetime(datetime.now(timezone.utc)+timedelta(seconds=20),usegmt=True),
        'past':'Wed, 01 Jan 2020 00:00:00 GMT','invalid':'not-a-date '+SECRET,'missing':None}[kind]
    client=MCPHttpClient(config(peer))
    with pytest.raises(MCPTransportError) as failure: await client.start()
    error=failure.value
    assert error.code=='mcp_capacity' and error.status_code==429 and _error(error) is error
    if kind=='seconds': assert error.retry_after_seconds==3
    elif kind=='date': assert 15 < error.retry_after_seconds <= 20
    elif kind=='past': assert error.retry_after_seconds==0
    else: assert error.retry_after_seconds is None
    assert set(vars(error))=={'code','message','status_code','retry_after_seconds'}
    assert SECRET not in str(error)
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("session", [False, True])
@pytest.mark.parametrize("auth", ["none", "bearer", "api_key"])
async def test_http_roundtrip_pagination_shared_session_and_headers(peers, sse, session, auth):
    peer = await peers(sse=sse, session=session, auth=auth)
    client = create_mcp_connection(config(peer))
    assert isinstance(client, MCPHttpClient)
    try:
        # Context owner differs from every caller, including normal close.
        await asyncio.create_task(client.start())
        tools = await asyncio.create_task(client.list_tools())
        registry = ToolRegistry()
        server_id = uuid4()
        adapters = [adapter(client, tool, server_id) for tool in tools]
        for tool in adapters:
            await tool.register(registry)
        results = await asyncio.gather(*(registry.execute(tool.name, {"query": "public test"}) for tool in adapters))
        assert all(result.success for result in results)
        assert all(json.loads(result.output)["structuredContent"]["count"] == 1 for result in results)
        assert len({tool.name for tool in adapters}) == 2
        assert all(len(tool.name) <= 64 for tool in adapters)
        assert "instructions" not in json.dumps([s.model_dump() for s in registry.get_specs()])
        if auth != "none":
            assert all(SECRET not in result.model_dump_json() for result in results)
        # Wrong/unknown arguments never reach the wire, and don't supply metadata.
        prior = len([r for r in peer.records if r["body"].get("method") == "tools/call"])
        denied = await registry.execute(adapters[0].name, {"query": "q", "endpoint": "https://evil.invalid"})
        assert not denied.success and denied.error.startswith("invalid_arguments:")
        assert len([r for r in peer.records if r["body"].get("method") == "tools/call"]) == prior
    finally:
        await asyncio.create_task(client.close())
    closed(client)
    bodies = [r["body"] for r in peer.records]
    assert len([b for b in bodies if b.get("method") == "initialize"]) == 1
    assert len([b for b in bodies if b.get("method") == "tools/list"]) == 2
    init = next(b for b in bodies if b.get("method") == "initialize")
    assert init["params"]["capabilities"] == {} and init["params"]["protocolVersion"] == "2025-11-25"
    assert len([r for r in peer.records if r["http"] == "DELETE"]) == int(session)
    for row in peer.records:
        assert row["path"] == "/mcp"
        if row["body"].get("method") != "initialize":
            assert row["headers"].get("mcp-protocol-version") == "2025-11-25"
            assert row["headers"].get("mcp-session-id") == ("fixture-session" if session else None)
        assert row["headers"].get("accept-encoding") == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("401", "mcp_auth"), ("403", "mcp_auth"), ("429", "mcp_capacity"),
    ("404", "mcp_not_mcp"), ("500", "mcp_transient"), ("html", "mcp_not_mcp"),
    ("version", "mcp_version"), ("capability", "mcp_capability"), ("redirect", "mcp_redirect"),
    ("malformed", "mcp_protocol"), ("compressed", "mcp_transport_unsupported"),
    ("duplicate_json", "mcp_protocol"), ("nan", "mcp_protocol"), ("bad_result", "mcp_protocol"),
    ("bad_error", "mcp_protocol"), ("legacy_sse", "mcp_transport_unsupported")])
async def test_http_failures_are_explicit_redacted_and_closed(peers, mode, code, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(mode=mode, auth="bearer")
    client = MCPHttpClient(config(peer))
    with pytest.raises(ForgePilotError, match=code) as failure:
        await client.start()
    assert SECRET not in str(failure.value) and SECRET not in caplog.text
    closed(client)
    assert all(r["path"] == "/mcp" for r in peer.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("rpc_error", "mcp_request"), ("tool_error", "mcp_tool_error"),
    ("invalid_output", "mcp_output_schema"), ("bad_content", "mcp_content"), ("null_content", "mcp_content"),
    ("nontext", "mcp_unsupported_content")])
async def test_tool_results_validate_full_output_and_errors(peers, mode, code, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(mode=mode, auth="bearer", sse=True)
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        registry = ToolRegistry()
        tool = adapter(client, (await client.list_tools())[0], uuid4())
        await tool.register(registry)
        result = await registry.execute(tool.name, {"query": "test"})
        assert not result.success and result.error.startswith(code + ":")
        assert SECRET not in result.model_dump_json() and SECRET not in caplog.text
        if mode in {"tool_error", "nontext"}:
            assert json.loads(result.output)["content"]
    finally:
        await client.close()
    closed(client)


@pytest.mark.asyncio
async def test_json_truncation_and_redaction_preserve_structure(peers):
    peer = await peers(mode="long", auth="bearer")
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        tool = adapter(client, (await client.list_tools())[0], uuid4())
        registry = ToolRegistry()
        await tool.register(registry)
        result = await registry.execute(tool.name, {"query": "test"})
        assert result.success, result.error
        data = json.loads(result.output)
        assert data["truncation"] and len(result.output) <= 16_384
        assert data["structuredContent"]["count"] == 1 and "[TRUNCATED]" in data["content"][0]["text"]
        assert SECRET not in result.output
        # An invalid field beyond the excerpt still fails full output validation.
        peer.mode = "invalid_output"
        assert (await registry.execute(tool.name, {"query": "test"})).error.startswith("mcp_output_schema:")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_http_timeout_cancel_reclaim_actual_socket_and_owner(peers, method, cancel):
    peer = await peers(mode="hang", fail_on=method)
    client = MCPHttpClient(config(peer, timeout=0.3 if not cancel else 3.0))
    if method != "initialize":
        await client.start()
    if method == "tools/call":
        await client.list_tools()
    operation = {"initialize": client.start, "tools/list": client.list_tools,
                 "tools/call": lambda: client.call_tool("search", {"query": "test"})}[method]
    pending = asyncio.create_task(operation())
    await peer.streaming.wait()
    started = monotonic()
    if cancel:
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ForgePilotError):
        await asyncio.wait_for(pending, 3)
    await asyncio.wait_for(peer.disconnected.wait(), 1)
    assert monotonic() - started < 3
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["oversized", "json_oversized", "traffic"])
async def test_limits_interrupt_stream_before_fixture_finishes(peers, mode):
    peer = await peers(mode=mode, fail_on="tools/call")
    client = MCPHttpClient(config(peer, timeout=5.0))
    await client.start()
    await client.list_tools()
    with pytest.raises(ForgePilotError, match="mcp_limit"):
        await client.call_tool("search", {"query": "test"})
    await asyncio.wait_for(peer.disconnected.wait(), 1)
    assert peer.bytes_sent < (4_500_000 if mode == "traffic" else 400_000)
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["delete_hang", "delete_drip"])
async def test_close_repeated_cancellation_and_hanging_delete(peers, mode):
    peer = await peers(mode=mode)
    client = MCPHttpClient(config(peer))
    await client.start()
    closing = asyncio.create_task(client.close())
    await peer.streaming.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 3)
    await asyncio.wait_for(peer.disconnected.wait(), 1)
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("cursor", "mcp_catalog"), ("duplicate", "mcp_catalog"),
    ("page_limit", "mcp_limit"), ("tool_limit", "mcp_limit"), ("catalog_limit", "mcp_limit")])
async def test_discovery_catalog_limits_reclaim_session(peers, mode, code):
    peer = await peers(mode=mode)
    client = MCPHttpClient(config(peer))
    await client.start()
    with pytest.raises(ForgePilotError, match=code):
        await client.list_tools()
    closed(client)
    assert len([r for r in peer.records if r["body"].get("method") == "tools/list"]) <= 8


@pytest.mark.asyncio
async def test_cancelled_call_drains_bounded_queue(peers):
    peer = await peers(mode="hang", fail_on="tools/call")
    client = MCPHttpClient(config(peer, timeout=5.0))
    await client.start()
    active = asyncio.create_task(client.call_tool("search", {"query": "test"}))
    await peer.streaming.wait()
    waiting = [asyncio.create_task(client.call_tool("fetch", {"query": "test"})) for _ in range(8)]
    await asyncio.sleep(0)
    with pytest.raises(ForgePilotError, match="mcp_capacity"):
        await client.call_tool("fetch", {"query": "test"})
    active.cancel()
    results = await asyncio.gather(active, *waiting, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert all(isinstance(result, ForgePilotError) for result in results[1:])
    assert client.queue.empty()
    closed(client)


@pytest.mark.asyncio
async def test_close_without_start_and_immediate_start_cancel(peers):
    peer = await peers()
    client = MCPHttpClient(config(peer))
    await client.close()
    await client.close()
    assert client.transport.closed and not peer.records
    with pytest.raises(ForgePilotError, match="mcp_state"):
        await client.start()
    client = MCPHttpClient(config(peer))
    pending = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 3)
    assert client.owner.done() and client.transport.closed


@pytest.mark.asyncio
async def test_bad_schema_is_per_tool_and_cross_server_names_do_not_conflict(peers):
    first, second = await peers(), await peers()
    bad = deepcopy(first.tools[0])
    bad["name"] = "bad"
    bad["inputSchema"]["properties"]["query"] = {"$ref": "https://example.invalid/secret"}
    first.tools.append(bad)
    clients = [MCPHttpClient(config(peer)) for peer in (first, second)]
    registry = ToolRegistry()
    names = []
    try:
        for client in clients:
            await client.start()
            catalog = await client.list_tools()
            entries = [inspect_tool(tool) for tool in catalog]
            if client is clients[0]:
                assert not entries[-1]["compatible"] and "External references" in entries[-1]["reason"]
            tool = adapter(client, catalog[0], uuid4())
            await tool.register(registry)
            names.append(tool.name)
            # Colliding registration does not terminate the connection.
            with pytest.raises(ForgePilotError, match="mcp_name_conflict"):
                await tool.register(registry)
        assert len(set(names)) == 2
        for name in names:
            assert (await registry.execute(name, {"query": "test"})).success
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize("change", [{"endpoint": "http://example.com/mcp"}, {"endpoint": "https://user:pass@host/mcp"},
    {"endpoint": "https://host/mcp?key=x"}, {"endpoint": "https://host/mcp#x"},
    {"endpoint": "https://host/\n"}, {"endpoint": "ftp://host/mcp"},
    {"auth_type": "api_key", "header_name": "MCP-Session-ID", "secret": "x"},
    {"auth_type": "api_key", "header_name": "Host", "secret": "x"},
    {"auth_type": "api_key", "header_name": "Bad\r\nName", "secret": "x"},
    {"auth_type": "bearer", "secret": "x\r\ny"}, {"auth_type": "bearer"}])
def test_connection_settings_cannot_weaken_http_boundary(change):
    with pytest.raises(ForgePilotError, match="mcp_config"):
        MCPHttpConfig(**{"endpoint": "https://example.com/mcp", **change}).checked()


def test_schema_combinations_local_refs_optional_values_and_closed_objects():
    schema = checked_schema({**deepcopy(INPUT), "allOf": [{"if": {"required": ["limit"]},
        "then": {"properties": {"query": {"minLength": 2}}}}]})
    validate(schema, {"query": "hello", "limit": None})
    validate(schema, {"query": "q"})
    for value in ({"query": "q", "limit": None}, {"query": "ok", "limit": True}, {"query": "ok", "limit": 0},
                  {"query": ""}, {"query": "ok", "x": 1}):
        with pytest.raises(ValueError):
            validate(schema, value)
    combinations = checked_schema({"type": "object", "properties": {"value": {
        "oneOf": [{"type": "string", "enum": ["x"]}, {"type": "integer", "minimum": 1}]}},
        "required": ["value"], "additionalProperties": False})
    validate(combinations, {"value": "x"})
    validate(combinations, {"value": 2})
    with pytest.raises(ValueError):
        validate(combinations, {"value": False})


@pytest.mark.parametrize("schema", [{"type": "object", "$ref": "#"},
    {"type": "object", "$ref": "#/$defs/missing"},
    {"type": "object", "allOf": [{"$ref": "file:///private.json"}]},
    {"type": "object", "$defs": {"x": {"$ref": "https://example.invalid"}}},
    {"type": "object", "description": "x" * 16_384},
    {"type": "object", "properties": {"x": {"pattern": "(a+)+$"}}}])
def test_schema_rejects_external_recursive_invalid_and_unbounded_patterns(schema):
    with pytest.raises(ForgePilotError, match="mcp_schema"):
        checked_schema(schema)


def test_write_and_task_augmented_tools_are_incompatible():
    base = {"name": "fetch", "inputSchema": INPUT, "outputSchema": OUTPUT}
    assert not inspect_tool(base)["compatible"]  # No implicit read capability.
    base["annotations"] = {"readOnlyHint": True}
    assert inspect_tool(base)["compatible"]  # User selection still required.
    for change in ({"annotations": {"readOnlyHint": False}}, {"execution": {"taskSupport": "required"}},
                   {"execution": []}, {"outputSchema": {"type": "invalid"}}):
        entry = inspect_tool({**base, **change})
        assert not entry["compatible"] and entry["reason"]
    assert tool_alias(uuid4(), "工具名/with spaces").isascii()


@pytest.mark.asyncio
async def test_sensitive_schema_is_rejected_without_closing_other_tools(peers):
    peer = await peers(auth="bearer")
    peer.tools[0]["inputSchema"]["description"] = SECRET
    # Fixture definitions originally share INPUT; isolate the second definition.
    peer.tools[1]["inputSchema"] = deepcopy(INPUT)
    peer.tools[1]["description"] = "description " + SECRET
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        tools = await client.list_tools()
        registry = ToolRegistry()
        with pytest.raises(ForgePilotError, match="sensitive content"):
            await adapter(client, tools[0], uuid4()).register(registry)
        good = adapter(client, tools[1], uuid4())
        await good.register(registry)
        assert SECRET not in json.dumps([s.model_dump() for s in registry.get_specs()])
        assert (await registry.execute(good.name, {"query": "test"})).success
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_output_constraint_is_checked_before_body_is_shortened(peers):
    peer = await peers(mode="long")
    peer.tools[0]["outputSchema"]["properties"]["body"]["maxLength"] = 5000
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        registry = ToolRegistry()
        tool = adapter(client, (await client.list_tools())[0], uuid4())
        await tool.register(registry)
        result = await registry.execute(tool.name, {"query": "test"})
        assert not result.success and result.error.startswith("mcp_output_schema:")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cross_origin_redirect_never_forwards_credentials(peers):
    target = await peers()
    peer = await peers(mode="redirect", auth="bearer")
    peer.redirect_to = target.endpoint
    client = MCPHttpClient(config(peer))
    with pytest.raises(ForgePilotError, match="mcp_redirect"):
        await client.start()
    closed(client)
    assert not target.records


@pytest.mark.asyncio
async def test_tls_verification_rejects_real_self_signed_peer(peers, tmp_path):
    from datetime import datetime, timedelta, timezone
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    peer = await peers(ssl_context=context)
    client = MCPHttpClient(config(peer))
    with pytest.raises(ForgePilotError, match="mcp_disconnected"):
        await client.start()
    closed(client)
    assert not peer.records


@pytest.mark.asyncio
async def test_server_requests_do_not_grant_roots_or_other_capabilities(peers):
    peer = await peers(mode="requests", sse=True)
    client = MCPHttpClient(config(peer))
    try:
        await client.start()
        await client.list_tools()
        async with asyncio.timeout(2):
            while not all(any(r["body"].get("id") == identity for r in peer.records)
                          for identity in ("untrusted-roots", "server-ping")):
                await asyncio.sleep(0.005)
        responses = {r["body"]["id"]: r["body"] for r in peer.records if "id" in r["body"]}
        assert "error" in responses["untrusted-roots"]
        assert responses["server-ping"]["result"] == {}
    finally:
        await client.close()


def test_schema_depth_and_validation_work_are_bounded():
    schema = {"type": "object"}
    node = schema
    for _ in range(9):
        node["properties"] = {"nested": {"type": "object"}}
        node = node["properties"]["nested"]
    with pytest.raises(ForgePilotError, match="depth"):
        checked_schema(schema)
    schema = checked_schema({"type": "object", "properties": {"values": {"type": "array", "uniqueItems": True}}})
    started = monotonic()
    with pytest.raises(ValueError, match="budget"):
        validate(schema, {"values": [{"value": i} for i in range(300)]})
    assert monotonic() - started < 0.5
