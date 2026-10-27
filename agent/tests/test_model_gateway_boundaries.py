"""Independent fake-provider checks of cleanup, shared budgets and privacy."""
import asyncio
from dataclasses import asdict
import json

import pytest

from forgepilot.agents import AgentRegistry
from forgepilot.errors import ForgePilotError
from forgepilot.models.accounting import ModelBudget
from forgepilot.models.contracts import ChatReply, ContentDelta, StreamFinished
from forgepilot.models.errors import ModelError
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.policy import Principal, Role
from forgepilot.security import Redactor
from forgepilot.tools.registry import FunctionTool, ToolRegistry
from forgepilot.tools.repository_tools import ReadArguments

KEY = "offline-model-key-never-log"


def configuration(*, fallback=False, route="chat"):
    profiles = [ModelProfile(profile_id="primary", base_url="https://fake.invalid/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model="test-model", family="one", max_output_tokens=16)]
    if fallback:
        profiles.append(ModelProfile(profile_id="fallback", base_url="https://fake.invalid/v1",
            api_key_ref="env:FORGEPILOT_API_KEY", model="second-model", family="two", max_output_tokens=16))
    return ModelConfiguration(profiles=profiles, routes=[ModelRoute(route_id=route, primary="primary",
        fallback="fallback" if fallback else None)])


def reply(content="ok", usage=None):
    return ChatReply.model_validate({"message": {"role": "assistant", "content": content}, "usage": usage})


class FakeProvider:
    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_consumer_closing_stream_closes_provider_and_accounts_cancellation():
    closed = asyncio.Event()
    class Provider(FakeProvider):
        async def stream_chat(self, messages, tools=None):
            try:
                yield ContentDelta("first")
                await asyncio.Event().wait()
            finally:
                closed.set()
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        stream = gateway.for_route("chat").stream_chat([])
        assert await anext(stream) == ContentDelta("first")
        await stream.aclose()
        assert closed.is_set()
        assert gateway.accounting.snapshot()[-1].status == "cancelled"


@pytest.mark.asyncio
async def test_shared_budget_reservation_blocks_concurrent_overspend():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            calls.append(messages)
            started.set()
            await release.wait()
            return reply(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})
    budget = ModelBudget(max_model_calls=1, max_input_tokens=10000, max_output_tokens=32)
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        client = gateway.for_route("chat", budget=budget)
        first = asyncio.create_task(client.chat([]))
        await started.wait()
        try:
            with pytest.raises(ForgePilotError, match="llm_budget"):
                await client.chat([])
            assert len(calls) == 1
        finally:
            release.set()
            await first
        assert (budget.model_calls, budget.input_tokens, budget.output_tokens) == (1, 2, 3)
        assert len(gateway.accounting.snapshot()) == 1


@pytest.mark.asyncio
async def test_cancelled_attempt_is_accounted_once_without_retry_or_fallback():
    started, cancelled = asyncio.Event(), asyncio.Event()
    profiles = []
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    def provider(profile, _):
        profiles.append(profile.profile_id)
        return Provider()
    async with ModelGateway(configuration(fallback=True), secret_resolver=lambda _: KEY,
                            provider_factory=provider) as gateway:
        task = asyncio.create_task(gateway.for_route("chat").chat([]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert profiles == ["primary"]
        records = gateway.accounting.snapshot()
        assert len(records) == 1 and records[0].status == "cancelled"


@pytest.mark.asyncio
async def test_truncated_stream_keeps_partial_hash_and_does_not_replay_output():
    profiles = []
    class Provider(FakeProvider):
        async def stream_chat(self, messages, tools=None):
            yield ContentDelta("partial")
    def provider(profile, _):
        profiles.append(profile.profile_id)
        return Provider()
    async with ModelGateway(configuration(fallback=True), secret_resolver=lambda _: KEY,
                            provider_factory=provider) as gateway:
        emitted = []
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            async for event in gateway.for_route("chat").stream_chat([]):
                emitted.append(event)
        assert emitted == [ContentDelta("partial")]
        assert profiles == ["primary"]
        record = gateway.accounting.snapshot()[0]
        assert record.status == "incomplete" and len(record.response_hash) == 64


@pytest.mark.asyncio
async def test_stream_schema_repair_publishes_only_validated_attempt():
    calls = []
    class Provider(FakeProvider):
        async def stream_chat(self, messages, tools=None):
            calls.append(messages)
            yield ContentDelta("invalid-secret-output" if len(calls) == 1 else '{"ok":true}')
            yield StreamFinished("stop", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        output = [event async for event in gateway.for_route("chat").stream_chat([], output_schema={
            "type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"],
            "additionalProperties": False,
        })]
        assert output == [ContentDelta('{"ok":true}'),
                          StreamFinished("stop", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})]
        assert [record.attempt for record in gateway.accounting.snapshot()] == ["primary", "schema_repair"]
        assert "invalid-secret-output" not in json.dumps([asdict(item) for item in gateway.accounting.snapshot()])


@pytest.mark.asyncio
async def test_provider_error_metadata_cannot_enter_accounting_as_sensitive_text():
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            raise ModelError("llm_protocol", KEY, category=KEY)
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        with pytest.raises(ForgePilotError):
            await gateway.for_route("chat").chat([])
        assert KEY not in json.dumps([asdict(item) for item in gateway.accounting.snapshot()])


@pytest.mark.asyncio
async def test_accounting_drops_provider_extras_context_and_reasoning():
    secret_context = "private repository context should not be recorded"
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            return {
                "message": {"role": "assistant", "content": "ok", "reasoning_content": secret_context},
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5,
                          "reasoning": secret_context, "api_key": KEY},
            }
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        result = await gateway.for_route("chat").chat([{"role": "user", "content": secret_context}])
        assert result.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        serialized = json.dumps([asdict(item) for item in gateway.accounting.snapshot()])
        assert KEY not in serialized and secret_context not in serialized
        assert len(gateway.accounting.snapshot()[0].response_hash) == 64


def test_agent_supplied_budget_cannot_exceed_registered_limits():
    gateway = ModelGateway(configuration(route="implementation"), secret_resolver=lambda _: KEY,
                           provider_factory=lambda *_: FakeProvider())
    definition = AgentRegistry().get("implementer.v1")
    excessive = ModelBudget(**(definition.limits.model_dump() | {"max_model_calls": definition.limits.max_model_calls + 1}))
    with pytest.raises(ForgePilotError, match="budget|llm_config"):
        gateway.for_agent(definition, budget=excessive)


def tool_reply(*paths, name="repository_read_range"):
    return ChatReply.model_validate({"message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": f"call-{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"path": path}),
        }} for index, path in enumerate(paths)]},
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})


def tool_registry(read, *, write=None):
    registry = ToolRegistry(principal=Principal(Role.IMPLEMENTER), redactor=Redactor((KEY,)))
    registry.register(FunctionTool("repository_read_range", "read", ReadArguments,
        read, read_only=True))
    if write is not None:
        registry.register(FunctionTool("repository_create_file", "write", ReadArguments, write))
    return registry


@pytest.mark.asyncio
async def test_tool_loop_preserves_multiple_call_result_order_and_sanitizes_observations():
    calls, requests = [], []
    async def read(path, **kwargs):
        calls.append(path)
        return path + KEY + "\x00" + "x" * 20_000
    registry = tool_registry(read)
    # A caller-created registry has no access to gateway credential resolution.
    # The gateway still must keep its known keys out of tool observations.
    registry.redactor = Redactor()
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            requests.append(messages)
            return tool_reply("second.cpp", "first.cpp") if len(requests) == 1 else reply()
    async with ModelGateway(configuration(route="review"), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        result = await gateway.for_route("review", role="reviewer",
            capabilities=("repository.read",)).run_tool_loop([], registry)
        assert result.message.content == "ok"
    assert calls == ["second.cpp", "first.cpp"]
    continuation = requests[1]
    assert [call["id"] for call in continuation[0]["tool_calls"]] == ["call-0", "call-1"]
    assert [item["tool_call_id"] for item in continuation[1:]] == ["call-0", "call-1"]
    for item in continuation[1:]:
        output = json.loads(item["content"])["output"]
        assert KEY not in output and "\x00" not in output and "TRUNCATED" in output
    assert registry._principal().role == Role.IMPLEMENTER


@pytest.mark.asyncio
async def test_tool_loop_fallback_preserves_reviewer_read_only_authority():
    executions, advertised, primary_calls = [], [], []
    async def read(path, **kwargs):
        executions.append((path, registry._principal().role))
        return path
    async def write(**kwargs):
        pytest.fail("Reviewer fallback must never execute writes")
    registry = tool_registry(read, write=write)
    class Provider(FakeProvider):
        def __init__(self, profile):
            self.profile = profile
        async def chat(self, messages, tools=None):
            advertised.append((self.profile.profile_id, [tool.name for tool in tools]))
            if self.profile.profile_id == "primary":
                primary_calls.append(messages)
                if len(primary_calls) == 1:
                    raise ModelError("llm_http", "Rate limited", category="rate_limit", status_code=429)
                return reply()
            return tool_reply("review.cpp")
    async with ModelGateway(configuration(fallback=True, route="review"), secret_resolver=lambda _: KEY,
                            provider_factory=lambda profile, _: Provider(profile)) as gateway:
        result = await gateway.for_route("review", role="reviewer",
            capabilities=("repository.read",)).run_tool_loop([], registry)
        assert result.message.content == "ok"
        assert [record.attempt for record in gateway.accounting.snapshot()] == ["primary", "fallback", "primary"]
    assert executions == [("review.cpp", Role.REVIEWER)]
    assert [profile for profile, _ in advertised] == ["primary", "fallback", "primary"]
    assert all(names == ["repository_read_range"] for _, names in advertised)
    assert registry._principal().role == Role.IMPLEMENTER


@pytest.mark.asyncio
async def test_tool_loop_rejects_fallback_attempt_to_call_unexposed_write():
    effects, profiles = [], []
    async def execute(path, **kwargs):
        effects.append(path)
        return path
    registry = tool_registry(execute, write=execute)
    class Provider(FakeProvider):
        def __init__(self, profile):
            self.profile = profile
        async def chat(self, messages, tools=None):
            profiles.append(self.profile.profile_id)
            if self.profile.profile_id == "primary":
                raise ModelError("llm_http", "Service unavailable", category="server", status_code=503)
            return tool_reply("forbidden.cpp", name="repository_create_file")
    async with ModelGateway(configuration(fallback=True, route="review"), secret_resolver=lambda _: KEY,
                            provider_factory=lambda profile, _: Provider(profile)) as gateway:
        with pytest.raises(ForgePilotError, match="llm_tool_unknown"):
            await gateway.for_route("review", role="reviewer").run_tool_loop([], registry)
    assert profiles == ["primary", "fallback"] and effects == []


@pytest.mark.asyncio
async def test_tool_loop_checks_whole_batch_budget_before_effects():
    effects = []
    async def read(path, **kwargs):
        effects.append(path)
        return path
    registry = tool_registry(read)
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            return tool_reply("a.cpp", "b.cpp")
    budget = ModelBudget(max_model_calls=4, max_tool_calls=1)
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        with pytest.raises(ForgePilotError, match="llm_budget"):
            await gateway.for_route("chat", budget=budget).run_tool_loop([], registry)
    assert effects == [] and budget.tool_calls == 0 and budget.model_calls == 1


@pytest.mark.asyncio
async def test_tool_loop_cancellation_stops_batch_and_restores_registry_scope():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    effects, requests = [], []
    async def read(path, **kwargs):
        effects.append((path, registry._principal().role))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    registry = tool_registry(read)
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            requests.append(messages)
            return tool_reply("a.cpp", "b.cpp")
    async with ModelGateway(configuration(route="review"), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        task = asyncio.create_task(gateway.for_route("review", role="reviewer",
            capabilities=("repository.read",)).run_tool_loop([], registry))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert len(requests) == 1
    assert effects == [("a.cpp", Role.REVIEWER)]
    assert registry._principal().role == Role.IMPLEMENTER
    assert registry._capabilities_override.get() is None


@pytest.mark.asyncio
async def test_tool_loop_agent_capability_subset_filters_each_request():
    requests = []
    async def read(path, **kwargs):
        assert registry._capabilities_override.get() == frozenset({"repository.read"})
        return path
    async def write(**kwargs):
        pytest.fail("Undeclared patch capability must remain unavailable")
    registry = tool_registry(read, write=write)
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            requests.append([tool.name for tool in tools])
            return tool_reply("a.cpp") if len(requests) == 1 else reply()
    async with ModelGateway(configuration(route="implementation"), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        await gateway.for_route("implementation", role="implementer",
            capabilities=("repository.read",)).run_tool_loop([], registry)
    assert requests == [["repository_read_range"], ["repository_read_range"]]
    assert {spec.name for spec in registry.get_specs()} == {"repository_read_range", "repository_create_file"}


@pytest.mark.asyncio
async def test_tool_loop_path_preflight_blocks_all_effects_in_invalid_batch():
    effects = []
    async def read(path, **kwargs):
        effects.append(path)
        return path
    registry = tool_registry(read)
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None):
            return tool_reply("allowed.cpp", "../secret.cpp")
    def check_path(name, arguments):
        if arguments["path"].startswith("../"):
            raise ForgePilotError("repository_denied", "Path outside approved scope")
    async with ModelGateway(configuration(), secret_resolver=lambda _: KEY,
                            provider_factory=lambda *_: Provider()) as gateway:
        with pytest.raises(ForgePilotError, match="repository_denied"):
            await gateway.for_route("chat").run_tool_loop([], registry, path_validator=check_path)
    assert effects == []
