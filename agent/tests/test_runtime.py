import asyncio
from dataclasses import asdict
import json

import pytest

from forgepilot.context import TaskContext, TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.runtime import AgentRuntime, transition
from forgepilot.security import Redactor
from forgepilot.tools import Tool, ToolRegistry, ToolResult
from forgepilot.tools.repository_tools import ListArguments
from forgepilot.tools.registry import FunctionTool
from forgepilot.tools.repository_tools import Arguments
from forgepilot.tools.change_tools import WRITE_TOOLS
from forgepilot.build import BUILD_TOOLS
from forgepilot.trace import MemoryTrace
from conftest import ScriptedLLM, call, reply


class EchoTool(Tool):
    name = "repository_read_range"
    description = "test-only typed tool"
    arguments_model = ListArguments
    async def execute(self, arguments):
        return ToolResult(success=True, output=arguments["path"], error=None, duration_ms=0)


def setup(llm, *, max_steps=30, secret=""):
    redactor = Redactor((secret,))
    trace = MemoryTrace(redactor)
    registry = ToolRegistry(redactor=redactor)
    registry.register(EchoTool())
    context = TaskContext("fixture", "analyze")
    return AgentRuntime(llm, registry, trace, redactor=redactor, max_steps=max_steps), context, trace


@pytest.mark.asyncio
async def test_plain_reply_and_unknown_usage():
    runtime, context, trace = setup(ScriptedLLM([reply("找到 Counter")]))
    result = await runtime.run(context)
    assert result.state == TaskState.COMPLETED and result.steps == 1 and result.tool_calls == 0
    assert result.final_result == "找到 Counter"
    assert result.token_usage == dict(prompt_tokens=None, completion_tokens=None, total_tokens=None)
    assert [e.data["state"] for e in trace.events if e.kind == "task_state_changed"] == ["analyzing", "completed"]


@pytest.mark.asyncio
async def test_analysis_hides_and_blocks_both_writers_and_builds():
    forbidden = sorted(WRITE_TOOLS | set(BUILD_TOOLS))
    llm = ScriptedLLM([reply(calls=[call(name, "{}", name) for name in forbidden]), reply("done")])
    runtime, context, trace = setup(llm)
    executed = []

    async def mutation():
        executed.append(True)
        return "unexpected mutation"

    for name in forbidden:
        runtime.registry.register(FunctionTool(name, "forbidden during analysis", Arguments, mutation))
    assert not set(forbidden) & {s.name for s in runtime._model_schemas(analysis=True)}
    assert {"repository_apply_patch", "repository_create_file", "repository_delete_new_file"} <= {s.name for s in runtime._model_schemas()}
    await runtime.run(context)
    assert context.state == TaskState.COMPLETED and not executed
    assert [r["tool_call_id"] for r in context.tool_results] == forbidden
    assert all(r["error"].startswith("analysis_read_only:") for r in context.tool_results)
    assert [m["tool_call_id"] for m in llm.requests[1] if m["role"] == "tool"] == forbidden


@pytest.mark.asyncio
async def test_multiple_calls_and_errors_are_observations_with_matching_ids():
    llm = ScriptedLLM([
        reply(None, [call("repository_read_range", '{"path":"ok"}', "one"), call("shell", '{}', "two"),
                     call("repository_read_range", '{"limit":"wrong"}', "three"), call("repository_read_range", '{bad-json', "four")],
              {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
        reply("done", usage={"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}),
    ])
    runtime, context, trace = setup(llm)
    await runtime.run(context)
    assert context.state == TaskState.COMPLETED and context.steps == 2 and context.tool_calls == 4
    messages = llm.requests[1]
    assert messages[2]["role"] == "assistant"
    observations = messages[3:]
    assert [m["tool_call_id"] for m in observations] == ["one", "two", "three", "four"]
    results = [json.loads(m["content"]) for m in observations]
    assert all(set(r) == {"success", "output", "error", "duration_ms"} for r in results)
    assert results[0]["success"] and results[0]["output"] == "ok"
    assert results[1]["error"].startswith("unknown_tool")
    assert all(r["error"].startswith("invalid_arguments") for r in results[2:])
    assert context.token_usage == dict(prompt_tokens=8, completion_tokens=10, total_tokens=18)
    assert len([e for e in trace.events if e.kind == "tool_finished"]) == 4


@pytest.mark.asyncio
async def test_exactly_30_attempts_and_no_31st_request():
    llm = ScriptedLLM([reply(None, [call("unknown", "{}")])] * 31)
    runtime, context, trace = setup(llm)
    await runtime.run(context)
    assert context.state == TaskState.FAILED
    assert context.steps == context.tool_calls == len(llm.requests) == 30
    assert context.error.startswith("step_limit")
    assert len(context.tool_results) == 30


@pytest.mark.asyncio
async def test_final_text_on_step_30_succeeds_and_partial_usage_is_unknown():
    llm = ScriptedLLM([reply(None, [call("unknown", "{}")], {"total_tokens": 1})] * 29 + [reply("done")])
    runtime, context, trace = setup(llm)
    await runtime.run(context)
    assert context.state == TaskState.COMPLETED and context.steps == 30
    assert context.token_usage["total_tokens"] is None


@pytest.mark.asyncio
async def test_llm_failure_is_terminal_and_counted():
    runtime, context, trace = setup(ScriptedLLM([ForgePilotError("llm_timeout", "Timed out")]))
    await runtime.run(context)
    assert context.state == TaskState.FAILED and context.steps == 1
    assert context.error.startswith("llm_timeout")
    assert context.token_usage["total_tokens"] is None


@pytest.mark.asyncio
async def test_cancellation_during_llm_and_tool():
    for during_tool in [False, True]:
        started = asyncio.Event()
        class SlowLLM:
            async def chat(self, messages, tools=None):
                if during_tool:
                    return reply(None, [call("repository_read_range", "{}")])
                started.set()
                await asyncio.sleep(10)
        runtime, context, trace = setup(SlowLLM())
        if during_tool:
            async def slow(arguments):
                started.set()
                await asyncio.sleep(10)
            runtime.registry.get("repository_read_range").execute = slow
        task = asyncio.create_task(runtime.run(context))
        await started.wait()
        task.cancel()
        result = await task
        assert result.state == TaskState.CANCELLED
        assert trace.events[-1].data["state"] == "cancelled"
        if during_tool:
            assert result.tool_results[-1]["error"].startswith("cancelled")


@pytest.mark.asyncio
async def test_registry_validation_duplicate_schema_timeout_and_safe_failure():
    registry = ToolRegistry(timeout=0.01)
    tool = EchoTool()
    registry.register(tool)
    with pytest.raises(ValueError):
        registry.register(tool)
    assert registry.get("missing") is None
    schema = registry.get_specs()
    assert schema[0].input_schema["additionalProperties"] is False
    schema[0].input_schema["properties"].clear()
    assert registry.get_specs()[0].input_schema["properties"]
    for args in [[], {"limit": True}, {"limit": 0}, {"extra": "x"}, '{', '"text"']:
        result = await registry.execute("repository_read_range", args)
        assert not result.success and result.error.startswith("invalid_arguments")
    async def slow(arguments):
        await asyncio.sleep(10)
    tool.execute = slow
    assert (await registry.execute("repository_read_range", {})).error.startswith("tool_timeout")
    async def broken(arguments):
        raise RuntimeError("sensitive-internal-value")
    tool.execute = broken
    result = await registry.execute("repository_read_range", {})
    assert result.error.startswith("tool_internal") and "sensitive" not in result.error
    async def broken_value(arguments):
        raise ValueError("internal value error")
    tool.execute = broken_value
    assert (await registry.execute("repository_read_range", {})).error.startswith("tool_internal")


@pytest.mark.asyncio
async def test_redaction_in_context_observations_trace_and_callbacks():
    secret = "fake-api-key-SHOULD-NEVER-BE-STORED"
    llm = ScriptedLLM([reply(None, [call("repository_read_range", json.dumps({"path": secret}))]), reply(secret)])
    runtime, context, trace = setup(llm, secret=secret)
    await runtime.run(context)
    assert secret not in json.dumps(asdict(context), default=str)
    assert secret not in json.dumps([asdict(e) for e in trace.events])
    assert secret not in json.dumps(llm.requests)
    assert "[REDACTED]" in context.final_result
    snapshot = trace.events
    snapshot[0].data["state"] = "corrupted"
    assert trace.events[0].data["state"] == "analyzing"


@pytest.mark.asyncio
async def test_state_and_context_budget_enforced(monkeypatch):
    assert {s.value for s in TaskState} == {
        "created", "analyzing", "planning", "waiting_approval", "modifying", "building",
        "testing", "reflecting", "completed", "failed", "cancelled",
    }
    runtime, context, trace = setup(ScriptedLLM([reply()]))
    with pytest.raises(ForgePilotError, match="invalid_state"):
        transition(context, TaskState.MODIFYING, trace)
    monkeypatch.setattr("forgepilot.runtime.MAX_CONTEXT_CHARS", 10)
    await runtime.run(context)
    assert context.error.startswith("context_limit") and context.steps == 0
    with pytest.raises(ForgePilotError, match="invalid_state"):
        await runtime.run(context)
    with pytest.raises(ValueError):
        setup(ScriptedLLM([]), max_steps=31)


def test_nested_json_and_structured_credentials_are_redacted_before_observers():
    secret = 'fake"key\\with-special+characters'
    observed = []
    trace = MemoryTrace(Redactor((secret,)), on_event=observed.append)
    encoded = secret
    for _ in range(3):
        encoded = json.dumps(encoded)[1:-1]
        trace.emit("task", "tool_finished", {"output": encoded, "Authorization": "another secret"})
    for event in trace.events + observed:
        assert "fake" not in event.data["output"]
        assert event.data["Authorization"] == "[REDACTED]"
