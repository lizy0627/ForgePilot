"""Use provider output-limit fields only when the SDK caller requests a limit."""
import json

import httpx
import pytest

from forgepilot.models.openai import OpenAICompatibleClient
from test_model_gateway import completion, sse


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("base_url,model,expected", [
    ("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-1-lite-260915", "max_completion_tokens"),
    ("https://ark.cn-beijing.volces.com/api/v3", "deepseek-v4-1-flash-261017", "max_tokens"),
    ("https://example.invalid/v1", "doubao-seed-2-1-lite-260915", "max_tokens"),
])
async def test_total_output_limit_uses_verified_provider_model_semantics(base_url, model, expected, stream):
    seen = []
    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload[expected] == 128
        assert ("max_tokens" in payload) != ("max_completion_tokens" in payload)
        if stream:
            return httpx.Response(200, content=sse({"content": "OK"}) + sse(finish="stop") + b"data: [DONE]\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=completion("OK"))
    async with OpenAICompatibleClient(base_url, "offline-key", model, max_output_tokens=256,
                                      transport=httpx.MockTransport(respond)) as client:
        if stream:
            events = [event async for event in client.stream_chat([], max_output_tokens=128)]
            assert events
        else:
            reply = await client.chat([], max_output_tokens=128)
            assert reply.message.content == "OK"
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("limit", [None, 100_000])
async def test_sdk_omits_unspecified_limits_and_supports_explicit_large_limits(stream, limit):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, content=sse({"content": "OK"}) + sse(finish="stop") + b"data: [DONE]\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=completion("OK"))

    async with OpenAICompatibleClient("https://ark.cn-beijing.volces.com/api/v3", "offline-key",
            "doubao-seed-2-1-fixture", transport=httpx.MockTransport(respond)) as client:
        if stream:
            assert [event async for event in client.stream_chat([], max_output_tokens=limit)]
        else:
            assert (await client.chat([], max_output_tokens=limit)).message.content == "OK"
    assert len(seen) == 1 and "max_tokens" not in seen[0]
    if limit is None:
        assert "max_completion_tokens" not in seen[0]
    else:
        assert seen[0]["max_completion_tokens"] == limit
