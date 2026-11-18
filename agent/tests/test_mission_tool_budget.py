"""Mission tool continuations track usage and share call and time limits."""
import asyncio
import json

import httpx
import pytest

from conftest import call, reply
from forgepilot.agents.registry import AgentRegistry
from forgepilot.errors import ForgePilotError
from forgepilot.models.accounting import ModelBudget
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.repository import Repository
from forgepilot.tools.registry import ToolRegistry
from forgepilot.tools.repository_tools import register_repository_tools


def configuration():
    return ModelConfiguration(profiles=(ModelProfile(
        profile_id="offline", base_url="https://offline.invalid/v1",
        api_key_ref="env:OFFLINE", model="fixture", family="fixture"),),
        routes=(ModelRoute(route_id="analysis", primary="offline"),))


def completion(content="done", *, calls=None, output=30, usage=True):
    result = {"choices": [{"message": {"role": "assistant", "content": content,
        "tool_calls": calls}, "finish_reason": "tool_calls" if calls else "stop"}]}
    if usage:
        result["usage"] = {"prompt_tokens": 100, "completion_tokens": output, "total_tokens": output + 100}
    return result


@pytest.mark.asyncio
async def test_default_analyst_sends_no_output_limit_across_real_repository_tools(cpp_repo, rg_exe):
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json=completion(None,
                calls=[call("repository_read_range", '{"path":"Counter.cpp"}', id="read-source")]))
        observation = json.loads(next(message["content"] for message in reversed(body["messages"])
                                      if message["role"] == "tool"))
        assert observation["success"] and "Counter" in observation["output"]
        if len(seen) == 2:
            return httpx.Response(200, json=completion(None,
                calls=[call("repository_search_text", '{"query":"Counter"}', id="search-source")]))
        return httpx.Response(200, json=completion())
    config = configuration()
    definition = AgentRegistry().get("root-cause-analyst.v1")
    budget = ModelBudget.from_limits(definition.limits)
    registry = ToolRegistry()
    register_repository_tools(registry, Repository(cpp_repo, rg_exe))
    async with ModelGateway(config, secret_resolver=lambda _: "offline-key",
            transport_factory=lambda _: httpx.MockTransport(handler)) as gateway:
        result = await gateway.for_agent(definition, budget=budget).run_tool_loop(
            [{"role": "user", "content": "Inspect Counter"}], registry)
        assert result.message.content == "done"
        assert all("max_tokens" not in request and "max_completion_tokens" not in request for request in seen)
        assert budget.model_calls == 3 and budget.tool_calls == 2 and budget.output_tokens == 90
        assert all(not record.usage_estimated for record in gateway.accounting.snapshot())


@pytest.mark.asyncio
async def test_streaming_requests_use_explicit_provider_limit_without_decrementing_it():
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        delta = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                              "finish_reason": None}]}
        finish = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        usage = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}
        data = "".join("data: " + json.dumps(value) + "\n\n" for value in (delta, finish, usage)) + "data: [DONE]\n\n"
        return httpx.Response(200, content=data.encode(), headers={"Content-Type": "text/event-stream"})
    budget = ModelBudget(max_output_tokens=1)
    config = configuration()
    profile = config.profiles[0].model_copy(update={"max_output_tokens": 12_000})
    async with ModelGateway(config.model_copy(update={"profiles": (profile,)}), secret_resolver=lambda _: "offline-key",
            transport_factory=lambda _: httpx.MockTransport(handler)) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        for _ in range(2):
            assert [event async for event in client.stream_chat([])]
        assert [body["max_tokens"] for body in seen] == [12_000, 12_000]
        assert budget.output_tokens == 14
        fresh = gateway.for_route("analysis", budget=ModelBudget(max_output_tokens=12_000))
        assert [event async for event in fresh.stream_chat([])]
        assert seen[-1]["max_tokens"] == 12_000


@pytest.mark.asyncio
async def test_concurrent_requests_exceed_old_token_limits_and_share_the_call_limit():
    seen = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        output = 8_000 if len(seen) == 1 else 1_000
        if len(seen) == 2:
            entered.set()
            await release.wait()
        return httpx.Response(200, json=completion(output=output))
    budget = ModelBudget(max_model_calls=3, max_output_tokens=1)
    async with ModelGateway(configuration(), secret_resolver=lambda _: "offline-key",
            transport_factory=lambda _: httpx.MockTransport(handler)) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        await client.chat([])
        running = asyncio.create_task(client.chat([]))
        await entered.wait()
        try:
            await client.chat([])
            with pytest.raises(ForgePilotError, match="llm_budget"):
                await client.chat([])
            assert budget.model_calls == len(seen) == 3 and budget.output_tokens == 9000
        finally:
            release.set()
            await running
        assert all("max_tokens" not in body for body in seen)
        assert budget.output_tokens == 10_000 and budget.model_calls == 3
        await gateway.for_route("analysis", budget=ModelBudget(max_output_tokens=12_000)).chat([])
        assert len(seen) == 4 and "max_tokens" not in seen[-1]


@pytest.mark.asyncio
async def test_missing_usage_is_estimated_for_each_request_and_allows_continuation():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion(usage=False))
    budget = ModelBudget(max_output_tokens=12_000)
    async with ModelGateway(configuration(), secret_resolver=lambda _: "offline-key",
            transport_factory=lambda _: httpx.MockTransport(handler)) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        await client.chat([])
        await client.chat([])
        assert len(seen) == budget.model_calls == 2 and budget.output_tokens == 2 * len("done")
        assert all(r.usage_estimated and r.input_tokens > 0 for r in gateway.accounting.snapshot())


@pytest.mark.asyncio
async def test_unknown_provider_keeps_its_existing_chat_signature_across_calls():
    requests = []
    class Provider:
        async def chat(self, messages, tools=None):
            requests.append(messages)
            return reply(usage={"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130})
        async def aclose(self):
            pass
    budget = ModelBudget(max_output_tokens=12_000)
    async with ModelGateway(configuration(), secret_resolver=lambda _: "offline-key",
            provider_factory=lambda *_: Provider()) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        await client.chat([])
        await client.chat([])
        assert len(requests) == budget.model_calls == 2 and budget.output_tokens == 60


@pytest.mark.asyncio
async def test_agent_budget_can_use_legacy_token_fields_above_its_definition():
    seen = []
    definition = AgentRegistry().get("root-cause-analyst.v1")
    budget = ModelBudget(**(definition.limits.model_dump() | {"max_input_tokens": 10**9, "max_output_tokens": 10**9}))
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion())
    async with ModelGateway(configuration(), secret_resolver=lambda _: "offline-key",
            transport_factory=lambda _: httpx.MockTransport(handler)) as gateway:
        await gateway.for_agent(definition, budget=budget).chat([])
        assert len(seen) == budget.model_calls == 1 and budget.output_tokens == 30
