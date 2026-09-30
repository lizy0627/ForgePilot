"""Generic adapters also work with the original real Windows stdio transport."""
import asyncio
import sys
from uuid import uuid4

import pytest

from forgepilot.errors import ForgePilotError, MCPRequestError
from forgepilot.mcp_config import MCPConfig, create_mcp_connection
from forgepilot.tools.mcp_tools import MCPToolAdapter
from forgepilot.tools.registry import ToolRegistry
from test_mcp import EXAMPLE, PEER, closed, wire, config


@pytest.mark.asyncio
async def test_stdio_rpc_error_keeps_session_and_next_call_succeeds(tmp_path):
    client = create_mcp_connection(MCPConfig(command=sys.executable,
        args=("-I", "-u", str(PEER), "generic_rpc", str(tmp_path / "wire.jsonl")), cwd=str(tmp_path)))
    try:
        await client.start()
        await client.list_tools()
        with pytest.raises(MCPRequestError) as error:
            await client.call_tool("search", {"query": "first"})
        assert "sk-development-secret" not in str(error.value)
        assert client.process.returncode is None
        assert (await client.call_tool("fetch", {"query": "second"}))["isError"] is False
    finally:
        await client.close()
    closed(client)
    records = wire(tmp_path)
    assert sum(r.get("method") == "initialize" for r in records) == 1
    assert [r["params"]["name"] for r in records if r.get("method") == "tools/call"] == ["search", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,method,code", [("init_rpc_error", "initialize", "mcp_request"),
    ("list_rpc_error", "tools/list", "mcp_request"), ("bad_rpc_error", "tools/call", "mcp_protocol")])
async def test_stdio_lifecycle_and_malformed_rpc_errors_still_reap_process(tmp_path, mode, method, code):
    client = create_mcp_connection(config(tmp_path, mode))
    if method != "initialize":
        await client.start()
    operation = {"initialize": client.start, "tools/list": client.list_tools,
        "tools/call": lambda: client.call_tool("search", {"query": "first"})}[method]
    with pytest.raises(ForgePilotError, match=code) as failed:
        await operation()
    if method == "tools/call":
        assert not isinstance(failed.value, MCPRequestError)
    assert "sk-development-secret" not in str(failed.value)
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stdio_cancel_after_rpc_error_keeps_current_request_id(tmp_path, cancel):
    client = create_mcp_connection(config(tmp_path, "generic_rpc", timeout=0.4 if not cancel else 3.0))
    try:
        await client.start()
        with pytest.raises(MCPRequestError):
            await client.call_tool("search", {"query": "first"})
        pending = asyncio.create_task(client.call_tool("fetch", {"query": "hang"}))
        async with asyncio.timeout(3):
            while sum(row.get("method") == "tools/call" for row in wire(tmp_path)) < 2:
                await asyncio.sleep(0.01)
        if cancel:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ForgePilotError):
            await asyncio.wait_for(pending, 3)
        closed(client)
    finally:
        await client.close()
    records = wire(tmp_path)
    calls = [row for row in records if row.get("method") == "tools/call"]
    cancelled = [row for row in records if row.get("method") == "notifications/cancelled"]
    assert len(calls) == 2 and len(cancelled) == 1
    assert cancelled[0]["params"]["requestId"] == calls[1]["id"] != calls[0]["id"]


@pytest.mark.asyncio
async def test_legacy_adapter_public_flags_do_not_corrupt_schema_or_output(tmp_path):
    from forgepilot.mcp_config import TOOL_NAME
    config = MCPConfig(command=sys.executable, args=("-I", "-u", str(EXAMPLE)), cwd=str(tmp_path),
        env=(("DEBUG", "true"), ("PYTHONUNBUFFERED", "1")), public_env_names=("DEBUG", "PYTHONUNBUFFERED"))
    adapter = MCPToolAdapter(config)
    registry = ToolRegistry()
    try:
        await adapter.register(registry)
        result = await registry.execute(TOOL_NAME, {"topic": "architecture"})
        assert result.success
        assert adapter.redactor.text("true 1") == "true 1"
        assert adapter.client.redactor.text("true 1") == "true 1"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_multiple_generic_stdio_tools_share_process_and_redact_explicit_environment(tmp_path, monkeypatch):
    secret = "fixture-explicit-stdio-credential"
    monkeypatch.setenv("FORGEPILOT_API_KEY", "model-key-must-not-be-inherited")
    config = MCPConfig(command=sys.executable, args=("-I", "-u", str(PEER), "generic", str(tmp_path / "wire.jsonl")),
                       cwd=str(tmp_path), env=(("MCP_TEST_TOKEN", secret),))
    client = create_mcp_connection(config)
    try:
        await client.start()
        catalog = await client.list_tools()
        registry = ToolRegistry()
        server_id = uuid4()
        for tool in catalog:
            adapter = MCPToolAdapter(server_id=server_id, remote_tool_name=tool["name"],
                input_schema=tool["inputSchema"], connection=client)
            await adapter.register(registry)
            result = await registry.execute(adapter.name, {"query": "generic non-topic query"})
            assert result.success and "[REDACTED]" in result.output and secret not in result.model_dump_json()
        assert len(registry.get_specs()) == 2
    finally:
        await client.close()
    closed(client)
    records = wire(tmp_path)
    assert not records[0]["environment_has_secret"]
    assert len([r for r in records if r.get("method") == "initialize"]) == 1
    assert {r["params"]["name"] for r in records if r.get("method") == "tools/call"} == {"search", "fetch"}
    assert secret not in (tmp_path / "wire.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_example_script_path_with_spaces_and_chinese(tmp_path):
    directory = tmp_path / "中文 MCP example"
    directory.mkdir()
    script = directory / "项目 说明.py"
    script.write_bytes(EXAMPLE.read_bytes())
    client = create_mcp_connection(MCPConfig(command=sys.executable, args=("-I", "-u", str(script)), cwd=str(directory)))
    try:
        await client.start()
        assert (await client.list_tools())[0]["name"] == "project_info"
        result = await client.call_tool("project_info", {"topic": "architecture"})
        assert "Qt Quick" in result["content"][0]["text"]
    finally:
        await client.close()
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_direct_connection_timeout_or_cancel_reclaims_stdio_without_adapter(tmp_path, cancel):
    config = MCPConfig(command=sys.executable, args=("-I", "-u", str(PEER), "hang", str(tmp_path / "wire.jsonl")),
        cwd=str(tmp_path), request_timeout=0.4 if not cancel else 3.0)
    client = create_mcp_connection(config)
    await client.start()
    pending = asyncio.create_task(client.call_tool("project_info", {"topic": "architecture"}))
    async with asyncio.timeout(3):
        while not any(r.get("method") == "tools/call" for r in wire(tmp_path)):
            await asyncio.sleep(0.005)
    if cancel:
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ForgePilotError):
        await asyncio.wait_for(pending, 3)
    closed(client)
