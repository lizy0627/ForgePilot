"""No provider requests: bounded wire parsing and program-owned tool authority."""
import asyncio
import json

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.models.tool_call_parser import (
    execute_tool_calls, parse_arguments, preflight_tool_calls, validate_tool_calls,
)
from forgepilot.mcp_manager import MCPManager
from forgepilot.policy import Principal, Role
from forgepilot.repository import Repository
from forgepilot.security import Redactor
from forgepilot.tools.registry import FunctionTool, ToolRegistry, ToolResult
from forgepilot.tools.repository_tools import Arguments, ReadArguments, register_repository_tools
from conftest import call


def registry_with_read(executions):
    registry = ToolRegistry()
    async def read(path, start_line=1, end_line=200):
        executions.append(path)
        return path
    registry.register(FunctionTool("repository_read_range", "read", ReadArguments, read,
                                   read_only=True))
    return registry


@pytest.mark.parametrize("arguments", ['{', '[]', '{"path":"x","path":"y"}',
                                       '{"path":NaN}', '{"path":1e999}', 'x' * 16_385])
def test_arguments_are_strict_bounded_json_without_echo(arguments):
    with pytest.raises(ForgePilotError, match="llm_tool_schema") as error:
        parse_arguments(arguments)
    assert arguments not in str(error.value)


@pytest.mark.parametrize("arguments", ['{}', '{"path":17}', '{"path":"x","extra":true}'])
def test_missing_wrong_and_extra_arguments_are_schema_errors(arguments):
    registry = registry_with_read([])
    with pytest.raises(ForgePilotError, match="llm_tool_schema"):
        validate_tool_calls([call("repository_read_range", arguments)], registry.get_specs())


def test_unknown_unexposed_calls_and_duplicate_ids_fail_closed():
    specs = registry_with_read([]).get_specs()
    with pytest.raises(ForgePilotError, match="llm_tool_unknown"):
        validate_tool_calls([call("shell", "{}")], specs)
    with pytest.raises(ForgePilotError, match="llm_tool_unknown"):
        validate_tool_calls([call()], None)
    with pytest.raises(ForgePilotError, match="llm_tool_protocol"):
        validate_tool_calls([call(), call()], specs)
    assert validate_tool_calls([call()], specs)[0].name == "read_file"


@pytest.mark.asyncio
async def test_invalid_batch_has_no_partial_tool_effects():
    executions = []
    registry = registry_with_read(executions)
    calls = [call("repository_read_range", '{"path":"valid"}', "a"),
             call("repository_read_range", '{}', "b")]
    with pytest.raises(ForgePilotError, match="llm_tool_schema"):
        await execute_tool_calls(calls, registry.get_specs(), registry)
    assert executions == []


@pytest.mark.asyncio
async def test_budget_checked_before_first_execution_and_order_is_stable():
    executions = []
    registry = registry_with_read(executions)
    calls = [call("repository_read_range", json.dumps({"path": name}), name)
             for name in ("second", "first")]
    with pytest.raises(ForgePilotError, match="model_budget"):
        await execute_tool_calls(calls, registry.get_specs(), registry, max_tool_calls=1)
    assert executions == []
    results = await execute_tool_calls(calls, registry.get_specs(), registry, max_tool_calls=2)
    assert executions == ["second", "first"]
    assert [result["tool_call_id"] for result in results] == executions
    assert [json.loads(result["content"])["output"] for result in results] == executions


@pytest.mark.asyncio
async def test_capabilities_narrow_role_and_do_not_leak_after_call():
    executions = []
    registry = registry_with_read(executions)
    with pytest.raises(ForgePilotError, match="policy_denied"):
        await execute_tool_calls([call()], registry.get_specs(), registry,
            principal=Principal(Role.REVIEWER), capabilities=("repository.search",))
    assert executions == []
    with registry.as_capabilities(("repository.search",)):
        assert registry.get_specs() == []
        with registry.as_capabilities(("repository.read",)):
            assert not (await registry.execute("read_file", '{"path":"x"}')).success
    assert (await registry.execute("read_file", '{"path":"x"}')).success


@pytest.mark.asyncio
async def test_explicit_principal_cannot_inherit_mcp_approval():
    registry = ToolRegistry()
    async def read():
        return "data"
    tool = FunctionTool("mcp_selected_read", "read", Arguments, read, read_only=True)
    tool.mcp_approved = True
    registry.register_approved_mcp(tool)
    assert (await registry.execute(tool.name, {})).success
    with registry.as_principal(Principal(Role.ANALYST, mcp_approved=False)):
        assert not (await registry.execute(tool.name, {})).success


@pytest.mark.asyncio
async def test_repository_path_preflight_rejects_escape_before_execution(cpp_repo, rg_exe):
    repository = Repository(cpp_repo, rg_exe)
    registry = ToolRegistry()
    register_repository_tools(registry, repository)
    executions = []
    tool = registry.get("repository_read_range")
    original = tool._execute
    async def tracked(**kwargs):
        executions.append(kwargs["path"])
        return await original(**kwargs)
    tool._execute = tracked
    calls = [call("repository_read_range", '{"path":"main.cpp"}', "a"),
             call("repository_read_range", '{"path":"../private.txt"}', "b")]
    with pytest.raises(ForgePilotError, match="repository_denied"):
        await execute_tool_calls(calls, registry.get_specs(), registry)
    assert executions == []


@pytest.mark.asyncio
async def test_outputs_are_sanitized_and_bounded():
    registry = registry_with_read([])
    registry.redactor = Redactor(("private-key-value",))
    tool = registry.get("repository_read_range")
    async def result(_):
        return ToolResult(success=False, output="private-key-value\x00" + "x" * 20_000,
                          error="private-key-value" + "e" * 10_000, duration_ms=0)
    tool.execute = result
    observations = await execute_tool_calls([call()], registry.get_specs(), registry)
    encoded = observations[0]["content"]
    parsed = json.loads(encoded)
    assert "private-key-value" not in encoded and "\x00" not in encoded
    assert len(parsed["output"]) < 16_450 and "TRUNCATED" in parsed["output"]
    assert len(parsed["error"]) == 4_096


@pytest.mark.asyncio
async def test_cancellation_does_not_execute_following_tool_or_leak_authority():
    registry = registry_with_read([])
    entered = asyncio.Event()
    calls = []
    async def wait(path, **kwargs):
        calls.append(path)
        entered.set()
        await asyncio.Event().wait()
    registry.get("repository_read_range")._execute = wait
    task = asyncio.create_task(execute_tool_calls([
        call("read_file", '{"path":"a"}', "a"), call("read_file", '{"path":"b"}', "b")],
        registry.get_specs(), registry, principal=Principal(Role.REVIEWER),
        capabilities=("repository.read",)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["a"]
    assert registry._principal().role == Role.ANALYST


@pytest.mark.asyncio
async def test_mcp_preflight_failure_cannot_fabricate_successful_evidence():
    executions = []
    registry = registry_with_read(executions)
    manager = MCPManager()
    with pytest.raises(ForgePilotError, match="mcp_state"):
        await manager.execute(registry, "read_file", '{"path":"x"}', "call",
            preflight_result=ToolResult(success=True, output="fabricated", error=None, duration_ms=0))
    assert executions == [] and manager.calls == 0
