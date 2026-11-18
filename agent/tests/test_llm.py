import asyncio
import json

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.llm import (
    ContentDelta,
    OpenAICompatibleClient,
    StreamFinished,
    ToolCallDelta,
    ToolSpec,
)
from conftest import call

KEY = "fake-test-private-key-DO-NOT-LOG"


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, error=None, started=None, closed=None):
        self.chunks = chunks
        self.error = error
        self.started = started
        self.closed = closed

    async def __aiter__(self):
        if self.started:
            self.started.set()
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        if self.closed:
            self.closed.set()


def stream_event(delta=None, *, finish=None, index=0, usage=None):
    value = {"choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n".encode()


def stream_response(body=None, *, chunks=None, status=200, content_type="text/event-stream"):
    headers = {"Content-Type": content_type} if content_type is not None else {}
    if chunks is not None:
        return httpx.Response(status, headers=headers, stream=ChunkStream(chunks))
    return httpx.Response(status, headers=headers, content=body or b"")


async def collect_stream(client, messages=None, tools=None):
    return [event async for event in client.stream_chat(messages or [], tools)]


def completion(content="回答", calls=None, usage=None, finish=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": calls},
                         "finish_reason": finish or ("tool_calls" if calls else "stop")}], "usage": usage}


@pytest.mark.asyncio
async def test_chat_wire_format_tool_ids_and_actual_usage():
    requests = []
    usage = {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11,
             "completion_tokens_details": {"reasoning_tokens": 2}}

    def handler(request):
        assert str(request.url) == "https://model.test/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion(None, [call()], usage))

    async with OpenAICompatibleClient("https://model.test/v1/", KEY, "configured-model",
                                      transport=httpx.MockTransport(handler)) as client:
        messages = [{"role": "tool", "tool_call_id": "previous_call", "content": "a tool result"}]
        tools = [ToolSpec(name="read_file",description="Read a file",input_schema={"type":"object"})]
        result = await client.chat(messages, tools)
        assert result.message.tool_calls[0].id == "call_1"
        assert result.message.tool_calls[0].function.arguments == '{"path":"Counter.cpp"}'
        assert result.usage == usage
        assert requests[0] == {"model": "configured-model", "messages": messages, "tools": [
            {"type":"function","function":{"name":"read_file","description":"Read a file","parameters":{"type":"object"}}}], "stream": False}
        assert KEY not in repr(client)


@pytest.mark.asyncio
async def test_plain_response_without_tools_or_usage():
    def handler(request):
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json=completion())
    async with OpenAICompatibleClient("http://localhost:1234/v1", KEY, "local",
                                      transport=httpx.MockTransport(handler)) as client:
        result = await client.chat([])
        assert result.message.content == "回答"
        assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500])
async def test_http_failure_is_diagnostic_without_secret_body_or_redirect(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY, headers={"Location": "https://other.test/" + KEY})
    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
                                      transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForgePilotError, match=f"HTTP {status}") as exc:
            await client.chat([])
        assert KEY not in str(exc.value)
        assert len(calls) == 1
        if status == 401:
            assert '模型鉴权失败' in str(exc.value)
            assert 'Configure-ForgePilot.cmd' in str(exc.value)
            assert 'Stop-ForgePilot.cmd' in str(exc.value)
        if status == 403:
            assert '模型访问权限' in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {}, {"choices": []}, {"choices": [None]},
    completion("", []), completion(None, [call(), call()]),
    completion(None, [call(arguments="x" * 16_385)]),
    completion(usage={"total_tokens": -1}), completion(usage={"total_tokens": True}),
    completion(usage=[]), completion(finish="length"),
    completion(finish="content_filter"), completion(None, [], finish="tool_calls"),
])
async def test_malformed_or_incomplete_responses_fail(data):
    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
                                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
        with pytest.raises(ForgePilotError, match="llm_(protocol|incomplete)"):
            await client.chat([])


@pytest.mark.asyncio
async def test_timeout_and_network_error_are_safe():
    async def slow(request):
        await asyncio.sleep(10)
    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test", timeout=0.01,
                                      transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(ForgePilotError, match="llm_timeout"):
            await client.chat([])
    def broken(request):
        raise httpx.ConnectError(KEY, request=request)
    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
                                      transport=httpx.MockTransport(broken)) as client:
        with pytest.raises(ForgePilotError, match="llm_network") as exc:
            await client.chat([])
        assert KEY not in str(exc.value)


@pytest.mark.asyncio
async def test_cancellation_closes_active_response_stream():
    started = asyncio.Event()
    closed = asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(10)
            yield b"{}"
        async def aclose(self):
            closed.set()
    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))) as client:
        task = asyncio.create_task(client.chat([]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


@pytest.mark.asyncio
async def test_response_size_limit_and_invalid_json():
    for body, code in [(b"x" * 1_048_577, "llm_response_limit"), (KEY.encode(), "llm_protocol")]:
        async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
            with pytest.raises(ForgePilotError, match=code):
                await client.chat([])


@pytest.mark.parametrize("url", ["file:///x", "https://user:password@model.test", "https://model.test/?key=x", "https://model.test/#x", "not-a-url"])
def test_invalid_endpoint_is_not_echoed(url):
    with pytest.raises(ForgePilotError, match="llm_config") as exc:
        OpenAICompatibleClient(url, KEY, "test")
    assert url not in str(exc.value)


@pytest.mark.parametrize("timeout", [0, -1, 601, float("inf"), float("nan"), True, "1"])
def test_timeout_configuration_is_bounded(timeout):
    with pytest.raises(ForgePilotError, match="llm_config"):
        OpenAICompatibleClient("https://model.test/v1", KEY, "test", timeout=timeout)


@pytest.mark.asyncio
async def test_stream_content_utf8_arbitrary_boundaries_crlf_multiline_and_usage_tail():
    requests = []
    first = stream_event({"role": "assistant", "content": "你"})
    second_json = json.dumps({
        "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": None}],
    }, ensure_ascii=False)
    midpoint = second_json.index('"delta"')
    second = (f": heartbeat\r\n\r\ndata: {second_json[:midpoint]}\r\n"
              f"data: {second_json[midpoint:]}\r\n\r\n").encode()
    finish = stream_event({}, finish="stop")
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
    body = b"\n" + first + second + finish + usage + b": done heartbeat\n\ndata: [DONE]\n\n"
    chunks = [body[index:index + 1] for index in range(len(body))]

    def handler(request):
        requests.append(json.loads(request.content))
        return stream_response(chunks=chunks)

    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test", stream_include_usage=True,
        transport=httpx.MockTransport(handler),
    ) as client:
        events = await collect_stream(client, [{"role": "user", "content": "hi"}])

    assert events == [
        ContentDelta("你"), ContentDelta("好"),
        StreamFinished("stop", {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}),
    ]
    assert requests == [{
        "model": "test", "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "stream_options": {"include_usage": True},
    }]


@pytest.mark.asyncio
async def test_stream_accepts_matching_usage_on_finish_and_tail():
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    body = (stream_event({"content": "ok"})
            + stream_event({}, finish="stop", usage=usage)
            + f'data: {json.dumps({"choices": [], "usage": usage})}\n\n'.encode()
            + b'data: [DONE]\n\n')
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test", stream_include_usage=True,
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        assert await collect_stream(client) == [ContentDelta("ok"), StreamFinished("stop", usage)]


@pytest.mark.asyncio
async def test_stream_rejects_conflicting_finish_and_tail_usage():
    finish_usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    tail_usage = {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}
    body = (stream_event({"content": "ok"})
            + stream_event({}, finish="stop", usage=finish_usage)
            + f'data: {json.dumps({"choices": [], "usage": tail_usage})}\n\n'.encode()
            + b'data: [DONE]\n\n')
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test", stream_include_usage=True,
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            await collect_stream(client)


@pytest.mark.asyncio
async def test_stream_default_omits_stream_options_and_ignores_heartbeat_empty_events():
    requests = []
    body = (b": ping\n\n\n\ndata:\n\n" + stream_event({"content": "ok"})
            + stream_event({}, finish="stop") + b"data: [DONE]\n\n")

    def handler(request):
        requests.append(json.loads(request.content))
        return stream_response(body)

    async with OpenAICompatibleClient("https://model.test/v1", KEY, "test",
                                      transport=httpx.MockTransport(handler)) as client:
        assert await collect_stream(client) == [ContentDelta("ok"), StreamFinished("stop", None)]
    assert "stream_options" not in requests[0]
    assert requests[0]["stream"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'data: {"choices":[{"index":0,"delta":{"content":"x"}},',
    b'data: {not json}\n\n',
    stream_event({"content": "x"}) + b"data: [DONE]\n\n",
    stream_event({"content": "x"}) + stream_event({}, finish="stop"),
    stream_event({}, finish="stop") + b"data: [DONE]\n\n",
])
async def test_stream_truncated_json_invalid_json_or_missing_finish_is_protocol_error(body):
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            await collect_stream(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "content_filter", "future_reason"])
async def test_stream_rejects_incomplete_and_unknown_finish_reasons(reason):
    body = stream_event({"content": "partial"}) + stream_event({}, finish=reason) + b"data: [DONE]\n\n"
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_incomplete"):
            await collect_stream(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_event", [
    {"choices": [
        {"index": 0, "delta": {"content": "a"}, "finish_reason": None},
        {"index": 1, "delta": {"content": "b"}, "finish_reason": None},
    ]},
    {"choices": [{"index": 1, "delta": {"content": "a"}, "finish_reason": None}]},
    {"choices": [{"index": False, "delta": {"content": "a"}, "finish_reason": None}]},
])
async def test_stream_requires_one_choice_at_index_zero(bad_event):
    body = f"data: {json.dumps(bad_event)}\n\n".encode()
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            await collect_stream(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,content_type,code", [
    (302, "text/event-stream", "llm_http"),
    (500, "text/event-stream", "llm_http"),
    (200, "application/json", "llm_protocol"),
    (200, None, "llm_protocol"),
])
async def test_stream_validates_http_status_redirect_and_content_type(status, content_type, code):
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(
            lambda _: stream_response(KEY.encode(), status=status, content_type=content_type)),
    ) as client:
        with pytest.raises(ForgePilotError, match=code) as exc:
            await collect_stream(client)
        assert KEY not in str(exc.value)


@pytest.mark.asyncio
async def test_stream_single_tool_call_appends_arguments_and_validates_at_finish():
    body = b"".join([
        stream_event({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                      "function": {"name": "read_file", "arguments": '{"path":"'}}]}),
        stream_event({"tool_calls": [{"index": 0, "function": {"arguments": "x.cpp\"}"}}]}),
        stream_event({}, finish="tool_calls"),
        b"data: [DONE]\n\n",
    ])
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        events = await collect_stream(client)
    assert events == [
        ToolCallDelta(0, "call_1", "read_file", '{"path":"'),
        ToolCallDelta(0, None, None, 'x.cpp"}'),
        StreamFinished("tool_calls", None),
    ]


@pytest.mark.asyncio
async def test_stream_interleaves_multiple_tool_calls_by_contiguous_index():
    body = b"".join([
        stream_event({"tool_calls": [{"index": 0, "id": "a", "type": "function",
                                      "function": {"name": "one", "arguments": "{"}}]}),
        stream_event({"tool_calls": [{"index": 1, "id": "b", "type": "function",
                                      "function": {"name": "two", "arguments": "["}}]}),
        stream_event({"tool_calls": [
            {"index": 1, "function": {"arguments": "]"}},
            {"index": 0, "function": {"arguments": "}"}},
        ]}),
        stream_event({}, finish="tool_calls"), b"data: [DONE]\n\n",
    ])
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        events = await collect_stream(client)
    assert [event.index for event in events if isinstance(event, ToolCallDelta)] == [0, 1, 1, 0]
    assert events[-1] == StreamFinished("tool_calls", None)


def tool_stream_body(*deltas, finish=True):
    body = b"".join(stream_event({"tool_calls": [delta]}) for delta in deltas)
    if finish:
        body += stream_event({}, finish="tool_calls") + b"data: [DONE]\n\n"
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    (tool_stream_body({"index": 1, "id": "a", "function": {"name": "x"}}), "llm_protocol"),
    (tool_stream_body({"index": 8, "id": "a", "function": {"name": "x"}}), "llm_protocol"),
    (tool_stream_body(
        {"index": 0, "id": "a", "function": {"name": "x"}},
        {"index": 0, "id": "changed", "function": {}},
    ), "llm_protocol"),
    (tool_stream_body(
        {"index": 0, "id": "a", "function": {"name": "x"}},
        {"index": 0, "function": {"name": "changed"}},
    ), "llm_protocol"),
    (tool_stream_body(
        {"index": 0, "id": "duplicate", "function": {"name": "x"}},
        {"index": 1, "id": "duplicate", "function": {"name": "y"}},
    ), "llm_protocol"),
    (tool_stream_body(
        {"index": 0, "id": "a", "function": {"name": "x", "arguments": "x" * 16_385}},
    ), "llm_response_limit"),
    (tool_stream_body(
        {"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}},
        finish=False,
    ), "llm_protocol"),
])
async def test_stream_rejects_malicious_or_incomplete_tool_assembly(body, code):
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match=code):
            await collect_stream(client)


@pytest.mark.asyncio
async def test_stream_does_not_emit_finished_for_incomplete_tool_call():
    body = tool_stream_body({"index": 0, "function": {"arguments": "{}"}})
    emitted = []
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(body)),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_protocol"):
            async for event in client.stream_chat([]):
                emitted.append(event)
    assert all(not isinstance(event, StreamFinished) for event in emitted)
    assert all(not hasattr(event, "function") for event in emitted)


@pytest.mark.asyncio
async def test_stream_content_event_and_total_response_limits():
    oversized_content = stream_event({"content": "x" * 32_769})
    heartbeat = b": " + b"x" * 60_000 + b"\n\n"
    oversized_response = heartbeat * 18
    for body in (oversized_content, b"data: " + b"x" * 65_537 + b"\n\n", oversized_response):
        async with OpenAICompatibleClient(
            "https://model.test/v1", KEY, "test",
            transport=httpx.MockTransport(lambda _, value=body: stream_response(value)),
        ) as client:
            with pytest.raises(ForgePilotError, match="llm_response_limit"):
                await collect_stream(client)


@pytest.mark.asyncio
async def test_stream_timeout_network_failure_and_cancellation_are_safe_and_close_response():
    async def slow(_):
        await asyncio.sleep(10)

    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test", timeout=0.01,
        transport=httpx.MockTransport(slow),
    ) as client:
        with pytest.raises(ForgePilotError, match="llm_timeout"):
            await collect_stream(client)

    request = httpx.Request("POST", "https://model.test/v1/chat/completions")
    broken = ChunkStream([b'data: {"cho'], error=httpx.ReadError(KEY, request=request))
    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: stream_response(chunks=broken.chunks)),
    ) as client:
        client._client._transport = httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=broken))
        with pytest.raises(ForgePilotError, match="llm_network") as exc:
            await collect_stream(client)
        assert KEY not in str(exc.value)

    started = asyncio.Event()
    closed = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(10)
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            closed.set()

    async with OpenAICompatibleClient(
        "https://model.test/v1", KEY, "test",
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=SlowStream())),
    ) as client:
        task = asyncio.create_task(collect_stream(client))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
