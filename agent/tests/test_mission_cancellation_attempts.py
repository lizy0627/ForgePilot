"""Durable Mission cancellation stops gateway recovery before another request."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from forgepilot.agents.review import ReviewerService
from forgepilot.models.accounting import ModelBudget
from forgepilot.models.errors import ModelError
from forgepilot.mission_events import publish_mission_event
from forgepilot.orchestration.graph import validate_graph
from forgepilot.orchestration.models import NodeStatus
from conftest import reply
from test_analysis_workflow import request, service_for, spec_data
from test_model_gateway import SCHEMA, completion, gateway_for as transport_gateway, sse
from test_reviewer import gateway_for, inputs


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["schema", "network"])
@pytest.mark.parametrize("admitted", [False, True])
async def test_analysis_cancellation_at_provider_result_stops_internal_recovery(cpp_repo, tmp_path, failure, admitted):
    class CancelAtResult:
        calls = 0

        async def chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                current = service.runner.store.snapshot(task_id)
                service.cancel(task_id, expected_revision=current.revision)
                if failure != "schema":
                    raise ModelError("llm_network", "Offline fault", category=failure)
                return reply("invalid JSON", usage={"prompt_tokens": 1, "completion_tokens": 1})
            return reply(json.dumps(spec_data()), usage={"prompt_tokens": 1, "completion_tokens": 1})

        async def aclose(self):
            pass

    provider = CancelAtResult()
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    task_id = str(uuid4())
    try:
        if admitted:
            service.runner.store.create_run(task_id, validate_graph(service.graph(request())))
            publish_mission_event(events, task_id, "task.created", {"request": "Offline cancellation fixture"})
        result = await service.run(task_id, request())
        assert result.status is NodeStatus.CANCELLED
        assert provider.calls == 1
        assert len(gateway.accounting.snapshot()) == 1
        assert not events.artifacts.list_for_task(task_id)
        assert all(node.attempt == 0 for node in result.nodes if node.node_id != "draft_task_spec")
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["schema", "network", "server"])
async def test_cancelled_gateway_stops_retry_fallback_and_schema_repair(failure, stream):
    cancelled = False
    requests = []

    def respond(request):
        nonlocal cancelled
        requests.append(request)
        cancelled = True
        if failure == "network":
            raise httpx.ConnectError("Offline fault", request=request)
        if failure == "server":
            return httpx.Response(503, text="Offline unavailable")
        if stream:
            return httpx.Response(200, content=sse({"content": "invalid JSON"}) + sse(finish="stop"),
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=completion("invalid JSON"))

    budget = ModelBudget(max_model_calls=4, max_output_tokens=256)
    async with transport_gateway(respond) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        with pytest.raises(asyncio.CancelledError):
            if stream:
                async for _ in client.stream_chat([], output_schema=SCHEMA, is_cancelled=lambda: cancelled):
                    pytest.fail("Cancelled malformed stream must not publish a response")
            else:
                await client.chat([], output_schema=SCHEMA, is_cancelled=lambda: cancelled)
        assert len(requests) == budget.model_calls == 1
        assert len(gateway.accounting.snapshot()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cancelled_gateway_never_reserves_or_sends_first_request(stream):
    budget = ModelBudget()
    async with transport_gateway(lambda _: pytest.fail("Cancelled request reached transport")) as gateway:
        client = gateway.for_route("analysis", budget=budget)
        with pytest.raises(asyncio.CancelledError):
            if stream:
                async for _ in client.stream_chat([], is_cancelled=lambda: True):
                    pytest.fail("Cancelled request published a stream event")
            else:
                await client.chat([], is_cancelled=lambda: True)
        assert budget.model_calls == 0
        assert gateway.accounting.snapshot() == ()


@pytest.mark.asyncio
async def test_reviewer_cancellation_at_invalid_result_stops_schema_repair():
    spec, changes, validation, evidence, expected = inputs()
    cancelled = False

    class CancelAtResult:
        calls = 0

        async def chat(self, messages, tools=None):
            nonlocal cancelled
            self.calls += 1
            cancelled = True
            return reply("invalid JSON" if self.calls == 1 else expected.model_dump_json(),
                         usage={"prompt_tokens": 1, "completion_tokens": 1})

        async def aclose(self):
            pass

    provider = CancelAtResult()
    async with gateway_for(provider) as gateway:
        with pytest.raises(asyncio.CancelledError):
            await ReviewerService(gateway).review(spec, changes, validation, evidence=(evidence,),
                                                  is_cancelled=lambda: cancelled)
        assert provider.calls == 1
        assert len(gateway.accounting.snapshot()) == 1
