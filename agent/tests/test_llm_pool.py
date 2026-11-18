"""Connection-pool ownership and request-isolation tests; no external network."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.chat_service import ChatService
from forgepilot.llm import ContentDelta, OpenAIClientPool, OpenAICompatibleClient
from forgepilot.stream_metrics import StreamMetrics


KEY = "offline-pool-test-key"


def event(delta=None, *, finish=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    return f"data: {json.dumps(value)}\n\n".encode()


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, rendezvous=None, entered=None, release=None):
        self.chunks = chunks
        self.rendezvous = rendezvous
        self.entered = entered
        self.release = release
        self.closed = False

    async def __aiter__(self):
        if self.entered is not None:
            self.entered.set()
        if self.rendezvous is not None:
            await self.rendezvous()
        if self.release is not None:
            await self.release.wait()
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self):
        self.closed = True


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.closed = False
        self.requests = 0

    async def handle_async_request(self, request):
        self.requests += 1
        return await self.handler(request)

    async def aclose(self):
        self.closed = True


def completion(text="ok"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                          "finish_reason": "stop"}]}


@pytest.mark.asyncio
async def test_pool_reuses_one_client_per_model_and_only_owner_closes():
    transports = {}

    async def handler(request):
        return httpx.Response(200, json=completion())

    def transport_factory(model):
        transports[model] = RecordingTransport(handler)
        return transports[model]

    pool = OpenAIClientPool("https://model.test/v1", KEY, transport_factory=transport_factory)
    chat_one = pool.lease("chat-model")
    chat_two = pool.lease("chat-model")
    task = pool.lease("task-model")
    assert chat_one._client is chat_two._client
    assert chat_one._client is not task._client

    await chat_one.aclose()
    assert (await chat_two.chat([])).message.content == "ok"
    assert not transports["chat-model"].closed

    await pool.aclose()
    assert all(transport.closed for transport in transports.values())
    await pool.aclose()
    with pytest.raises(ForgePilotError, match="closed") as exc:
        pool.lease("chat-model")
    assert exc.value.code == "server_stopping"


@pytest.mark.asyncio
async def test_concurrent_streams_keep_text_and_metrics_isolated():
    arrivals = set()
    ready = asyncio.Event()

    async def meet(name):
        arrivals.add(name)
        if len(arrivals) == 2:
            ready.set()
        await ready.wait()

    async def handler(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        chunks = ([event({"content": "A"}), event(finish="stop"), b"data: [DONE]\n\n"]
                  if prompt == "alpha" else
                  [event({"content": "B"}), event({"content": "2"}),
                   event(finish="stop"), b"data: [DONE]\n\n"])
        stream = AsyncChunks(chunks, rendezvous=lambda: meet(prompt))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    transport = RecordingTransport(handler)
    pool = OpenAIClientPool("https://model.test/v1", KEY,
                            transport_factory=lambda model: transport)
    one, two = pool.lease("shared"), pool.lease("shared")
    metrics_one, metrics_two = StreamMetrics(), StreamMetrics()

    async def collect(client, prompt, metrics):
        metrics.llm_started()
        started = monotonic()
        items = [item async for item in client.stream_chat(
            [{"role": "user", "content": prompt}], diagnostics=metrics,
            model_started=started)]
        metrics.model_finished(started)
        return "".join(item.text for item in items if isinstance(item, ContentDelta))

    try:
        first, second = await asyncio.gather(
            collect(one, "alpha", metrics_one), collect(two, "beta", metrics_two))
        assert (first, second) == ("A", "B2")
        assert metrics_one.snapshot()["provider_chunk_count"] == 3
        assert metrics_two.snapshot()["provider_chunk_count"] == 4
        assert metrics_one.snapshot()["llm_round_count"] == 1
        assert metrics_two.snapshot()["llm_round_count"] == 1
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_concurrent_chat_services_have_request_local_stream_redactors():
    arrivals = 0
    ready = asyncio.Event()

    async def meet():
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()

    async def handler(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        pieces = (["left offline-pool-", "test-key right"] if prompt == "alpha"
                  else ["top offline-", "pool-test-key bottom"])
        chunks = [event({"content": piece}) for piece in pieces]
        chunks.extend((event(finish="stop"), b"data: [DONE]\n\n"))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=AsyncChunks(chunks, rendezvous=meet))

    pool = OpenAIClientPool("https://model.test/v1", KEY,
                            transport_factory=lambda model: RecordingTransport(handler))
    first = ChatService(pool.lease("shared"))
    second = ChatService(pool.lease("shared"))
    first_events, second_events = [], []

    async def capture(target, event):
        target.append(event)

    try:
        first_answer, second_answer = await asyncio.gather(
            first.answer([], "alpha", on_event=lambda item: capture(first_events, item),
                         diagnostics=StreamMetrics(), tool_mode="fast"),
            second.answer([], "beta", on_event=lambda item: capture(second_events, item),
                          diagnostics=StreamMetrics(), tool_mode="fast"))
        assert first_answer == "left [REDACTED] right"
        assert second_answer == "top [REDACTED] bottom"
        assert KEY not in "".join(item.text or "" for item in first_events + second_events)
    finally:
        await first.close()
        await second.close()
        await pool.aclose()


@pytest.mark.asyncio
async def test_cancelled_stream_closes_response_without_poisoning_shared_pool():
    entered, release = asyncio.Event(), asyncio.Event()
    held_stream = AsyncChunks([event({"content": "late"}), event(finish="stop"),
                               b"data: [DONE]\n\n"], entered=entered, release=release)

    async def handler(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if prompt == "hold":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  stream=held_stream)
        return httpx.Response(200, json=completion("still healthy"))

    transport = RecordingTransport(handler)
    pool = OpenAIClientPool("https://model.test/v1", KEY,
                            transport_factory=lambda model: transport)
    client = pool.lease("shared")

    async def consume():
        return [item async for item in client.stream_chat(
            [{"role": "user", "content": "hold"}])]

    pending = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert held_stream.closed
    assert (await client.chat([{"role": "user", "content": "next"}])).message.content == "still healthy"
    await pool.aclose()


@pytest.mark.asyncio
async def test_model_error_does_not_close_or_contaminate_other_request():
    async def handler(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if prompt == "fail":
            return httpx.Response(500, text="private provider body")
        return httpx.Response(200, json=completion("success"))

    transport = RecordingTransport(handler)
    pool = OpenAIClientPool("https://model.test/v1", KEY,
                            transport_factory=lambda model: transport)
    first, second = pool.lease("shared"), pool.lease("shared")
    failed, succeeded = await asyncio.gather(
        first.chat([{"role": "user", "content": "fail"}]),
        second.chat([{"role": "user", "content": "ok"}]),
        return_exceptions=True)
    assert isinstance(failed, ForgePilotError) and failed.code == "llm_http"
    assert "private provider body" not in str(failed)
    assert succeeded.message.content == "success"
    assert not transport.closed
    await pool.aclose()


@pytest.mark.asyncio
async def test_http_client_pool_limits_preserve_security_options(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def aclose(self):
            return None

    monkeypatch.setattr("forgepilot.llm.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleClient("https://model.test/v1", KEY, "model")
    try:
        assert captured["follow_redirects"] is False
        assert captured["trust_env"] is False
        assert captured["limits"].max_connections == 20
        assert captured["limits"].max_keepalive_connections == 10
        assert captured["limits"].keepalive_expiry == 30.0
    finally:
        await client.aclose()
