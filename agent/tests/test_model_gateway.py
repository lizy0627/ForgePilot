"""The gateway's complete recovery state machine uses only in-process providers."""
import asyncio
from dataclasses import asdict
import json

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.llm import ContentDelta, StreamFinished, ToolCallDelta, ToolSpec
from forgepilot.models.accounting import Accounting, ModelBudget
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from conftest import call, reply


KEY = "private-offline-gateway-key"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
READ = ToolSpec(name="repository_read_range", description="Read", input_schema={
    "type": "object", "properties": {"path": {"type": "string"}},
    "required": ["path"], "additionalProperties": False})


def config(*, timeout=1.0, fallback=True, max_output_tokens=32):
    profiles = tuple(ModelProfile(profile_id=name, base_url="https://offline.test/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model=f"model-{name}", family=name,
        timeout_seconds=timeout, max_output_tokens=max_output_tokens, temperature=0.2)
        for name in ("primary", "backup"))
    return ModelConfiguration(profiles=profiles, routes=(ModelRoute(route_id="analysis",
        primary="primary", fallback="backup" if fallback else None),))


def completion(content="ok", calls=None, usage=None, finish=None):
    return {"choices": [{"message": {"role": "assistant", "content": content,
        "tool_calls": calls}, "finish_reason": finish or ("tool_calls" if calls else "stop")}],
        "usage": usage}


def gateway_for(handler, **kwargs):
    return ModelGateway(config(**kwargs), secret_resolver=lambda _: KEY,
                        transport_factory=lambda _: httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_concurrent_task_accounting_scopes_do_not_mix_or_leak():
    arrived = 0
    both = asyncio.Event()
    ledgers = {"one": [], "two": []}

    async def handler(request):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await both.wait()
        return httpx.Response(200, json=completion(usage={
            "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}))

    async with gateway_for(handler) as gateway:
        async def invoke(name):
            with gateway.accounting.scoped_sink(ledgers[name].append):
                await gateway.for_route("analysis").chat([{"role": "user", "content": name}])
        await asyncio.gather(invoke("one"), invoke("two"))
        await gateway.for_route("analysis").chat([])
        assert len(gateway.accounting.snapshot()) == 3
        assert len(ledgers["one"]) == len(ledgers["two"]) == 1
        assert ledgers["one"][0]["input_hash"] != ledgers["two"][0]["input_hash"]
        assert all(row["input_tokens"] == 3 and row["output_tokens"] == 2
                   for ledger in ledgers.values() for row in ledger)


@pytest.mark.parametrize("url,timeout", [("https://user:secret@model.test", 60),
                                        ("https://model.test", 0)])
def test_legacy_invalid_configuration_keeps_safe_error_contract(url, timeout):
    with pytest.raises(ForgePilotError, match="llm_config") as exc:
        ModelGateway.from_legacy(url, KEY, {"chat": "test"}, timeout=timeout)
    assert url not in str(exc.value) and KEY not in str(exc.value)


@pytest.mark.asyncio
async def test_network_retry_fallback_schema_repair_four_attempts_accounted():
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) <= 2:
            raise httpx.ConnectError(KEY)
        return httpx.Response(200, json=completion('not json' if len(seen) == 3 else '{"ok":true}',
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
                   "private_context": KEY, "reasoning": "hidden thought"}))
    budget = ModelBudget(max_model_calls=4, max_input_tokens=10000, max_output_tokens=200)
    async with gateway_for(handler) as gateway:
        client = gateway.for_route("analysis", role="root_cause_analyst", budget=budget,
                                   prompt_version="analyst-prompt.v1")
        result = await client.chat([{"role": "user", "content": "private context"}],
                                   output_schema=SCHEMA)
        assert result.message.content == '{"ok":true}'
        assert result.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        records = gateway.accounting.snapshot()
        assert [r.attempt for r in records] == ["primary", "retry", "fallback", "schema_repair"]
        assert [r.status for r in records] == ["network", "network", "schema", "success"]
        assert records[-1].fallback_count == records[-1].retry_count == 1
        assert len({r.call_id for r in records}) == 1
        assert all(r.duration_ms >= 0 and len(r.input_hash) == 64 for r in records)
        assert len(records[-1].response_hash) == 64
        serialized = json.dumps([asdict(r) for r in records])
        assert all(s not in serialized for s in (KEY, "private context", "hidden thought", "api_key_ref", "base_url"))
        assert budget.model_calls == 4
    assert [p["model"] for p in seen] == ["model-primary", "model-primary", "model-backup", "model-backup"]
    assert all(p["max_tokens"] == 32 and p["temperature"] == 0.2 for p in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_http_transients_go_to_fallback_without_primary_retry(status):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(status, text=KEY) if len(seen) == 1 else httpx.Response(200, json=completion())
    async with gateway_for(handler) as gateway:
        assert (await gateway.for_route("analysis").chat([])).message.content == "ok"
        assert [r.attempt for r in gateway.accounting.snapshot()] == ["primary", "fallback"]
    assert seen == ["model-primary", "model-backup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 400, 451])
async def test_auth_permission_policy_request_errors_are_terminal(status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, text=KEY)
    async with gateway_for(handler) as gateway:
        with pytest.raises(ForgePilotError) as exc:
            await gateway.for_route("analysis").chat([])
        assert KEY not in str(exc.value)
        assert len(gateway.accounting.snapshot()) == 1
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_content_filter_never_retries_or_falls_back():
    async with gateway_for(lambda _: httpx.Response(200, json=completion(finish="content_filter"))) as gateway:
        with pytest.raises(ForgePilotError):
            await gateway.for_route("analysis").chat([])
        assert [r.status for r in gateway.accounting.snapshot()] == ["policy"]


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_fallback_attempt_has_no_retry():
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content)["model"])
        await asyncio.sleep(10)
    async with gateway_for(handler, timeout=0.01) as gateway:
        with pytest.raises(ForgePilotError, match="llm_timeout"):
            await gateway.for_route("analysis").chat([])
        assert [r.attempt for r in gateway.accounting.snapshot()] == ["primary", "retry", "fallback"]
    assert seen == ["model-primary", "model-primary", "model-backup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b'{"choices":[]}'])
async def test_invalid_response_json_repairs_once_and_fails_without_fallback(body):
    async with gateway_for(lambda _: httpx.Response(200, content=body)) as gateway:
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            await gateway.for_route("analysis").chat([])
        assert [r.attempt for r in gateway.accounting.snapshot()] == ["primary", "schema_repair"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,attempts,code", [
    ("shell", "{}", 1, "llm_tool_unknown"),
    (READ.name, "{}", 2, "llm_tool_schema"),
    (READ.name, '{"path":', 2, "llm_tool_schema"),
])
async def test_unknown_tool_and_missing_invalid_arguments(name, arguments, attempts, code):
    async with gateway_for(lambda _: httpx.Response(200, json=completion(None,
            [call(name=name, arguments=arguments)]))) as gateway:
        with pytest.raises(ForgePilotError, match=code):
            await gateway.for_route("analysis").chat([], tools=[READ])
        assert len(gateway.accounting.snapshot()) == attempts


@pytest.mark.asyncio
async def test_model_and_token_budgets_stop_before_next_network_attempt():
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ConnectError("offline")
    async with gateway_for(handler) as gateway:
        budget = ModelBudget(max_model_calls=1)
        with pytest.raises(ForgePilotError, match="llm_budget"):
            await gateway.for_route("analysis", budget=budget).chat([])
        assert budget.model_calls == len(seen) == 1
        budget = ModelBudget(max_output_tokens=31)
        with pytest.raises(ForgePilotError, match="llm_budget"):
            await gateway.for_route("analysis", budget=budget).chat([])
        assert budget.model_calls == 0 and len(seen) == 1


@pytest.mark.asyncio
async def test_cancelled_attempt_records_and_closes_response_without_retry():
    entered, closed = asyncio.Event(), asyncio.Event()
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.sleep(10)
            yield b"{}"
        async def aclose(self):
            closed.set()
    async with gateway_for(lambda _: httpx.Response(200, stream=SlowBody())) as gateway:
        task = asyncio.create_task(gateway.for_route("analysis").chat([]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert [r.status for r in gateway.accounting.snapshot()] == ["cancelled"]


def sse(delta=None, finish=None):
    return f'data: {json.dumps({"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]})}\n\n'.encode()


@pytest.mark.asyncio
async def test_stream_truncation_does_not_restart_or_finish_partial_response():
    body = sse({"content": "partial"})
    async with gateway_for(lambda _: httpx.Response(200, content=body,
            headers={"Content-Type": "text/event-stream"})) as gateway:
        events = []
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            async for event in gateway.for_route("analysis").stream_chat([]):
                events.append(event)
        assert events == [ContentDelta("partial")]
        assert len(gateway.accounting.snapshot()) == 1
        assert gateway.accounting.snapshot()[0].response_hash is not None


@pytest.mark.asyncio
async def test_stream_schema_repair_buffers_invalid_attempt_and_emits_only_valid_result():
    seen = []
    def handler(request):
        seen.append(request)
        content = "bad" if len(seen) == 1 else '{"ok":true}'
        return httpx.Response(200, content=sse({"content": content}) + sse(finish="stop") + b"data: [DONE]\n\n",
                              headers={"Content-Type": "text/event-stream"})
    async with gateway_for(handler) as gateway:
        events = [e async for e in gateway.for_route("analysis").stream_chat([], output_schema=SCHEMA)]
        assert events == [ContentDelta('{"ok":true}'), StreamFinished("stop", None)]
        assert len(seen) == 2


@pytest.mark.asyncio
async def test_stream_tool_schema_repair_never_exposes_invalid_call():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        arguments = "{}" if len(requests) == 1 else '{"path":"safe.txt"}'
        body = (sse({"tool_calls": [{"index": 0, "id": "read", "type": "function",
            "function": {"name": READ.name, "arguments": arguments}}]})
            + sse(finish="tool_calls") + b"data: [DONE]\n\n")
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})
    async with gateway_for(handler) as gateway:
        events = [e async for e in gateway.for_route("analysis").stream_chat([], tools=[READ])]
        assert events == [ToolCallDelta(0, "read", READ.name, '{"path":"safe.txt"}'),
                          StreamFinished("tool_calls", None)]
        assert [r.attempt for r in gateway.accounting.snapshot()] == ["primary", "schema_repair"]
        assert "Previous response to correct" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_fallback_has_identical_tools_and_cannot_gain_permission():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=completion(None, [call(name="repository_apply_patch", arguments="{}")]))
    async with gateway_for(handler) as gateway:
        client = gateway.for_route("analysis", role="reviewer", capabilities=["repository.read"])
        with pytest.raises(ForgePilotError, match="llm_tool_unknown"):
            await client.chat([], tools=[READ])
        assert requests[0]["tools"] == requests[1]["tools"]
        assert requests[0]["messages"] == requests[1]["messages"]
        assert client.role == "reviewer" and client.capabilities == frozenset({"repository.read"})


@pytest.mark.asyncio
async def test_different_secrets_and_hidden_provider_fields_never_enter_accounting(tmp_path):
    from forgepilot.models.accounting import configure_accounting_logging, close_accounting_logging
    path = tmp_path / "accounting.jsonl"
    configure_accounting_logging(path)
    try:
        async with gateway_for(lambda _: httpx.Response(200, json=completion("sensitive answer",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "reasoning_content": KEY}))) as gateway:
            await gateway.for_route("analysis").chat([{"role": "user", "content": "secret context"}])
        raw = path.read_text(encoding="utf-8")
        assert all(value not in raw for value in (KEY, "sensitive answer", "secret context", "reasoning_content"))
        record = json.loads(raw)
        assert record["input_tokens"] == 10 and record["output_tokens"] == 2
    finally:
        close_accounting_logging()
