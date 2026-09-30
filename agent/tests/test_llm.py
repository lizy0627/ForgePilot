import asyncio
import json

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.llm import OpenAICompatibleClient, ToolSpec
from conftest import call

KEY = "fake-test-private-key-DO-NOT-LOG"


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
