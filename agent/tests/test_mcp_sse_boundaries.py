"""SSE framing stays bounded across arbitrary network chunk boundaries."""
import json
from types import SimpleNamespace

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.mcp_http import MAX_MESSAGE, _LimitedStream


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.emitted = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.emitted += 1
            yield chunk


def framed_stream(chunks):
    failures = []
    owner = SimpleNamespace(received=0, _abort=failures.append)
    source = Chunks(chunks)
    return _LimitedStream(source, owner, True), source, failures


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
@pytest.mark.parametrize("invalid", [False, True])
async def test_sse_validates_messages_across_delimiters_and_utf8_chunks(newline, chunk_size, invalid):
    message = {"jsonrpc": "1.0" if invalid else "2.0", "method": "notifications/message",
               "params": {"text": "多字节内容"}}
    # Empty events and comments grant no RPC authority; the following data must
    # still be validated even when CRLF or UTF-8 bytes cross a network chunk.
    raw = newline * 2 + b": comment" + newline * 2
    raw += b"data: " + json.dumps(message, ensure_ascii=False).encode("utf-8") + newline * 2
    stream, _, failures = framed_stream([raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)])
    if invalid:
        with pytest.raises(ForgePilotError, match="mcp_protocol"):
            async for _ in stream:
                pass
        assert failures and failures[0].code == "mcp_protocol"
    else:
        assert b"".join([chunk async for chunk in stream]) == raw
        assert not failures


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
async def test_near_limit_comment_event_does_not_hide_following_message(newline):
    comment = b":" + b"x" * (MAX_MESSAGE - 9) + newline * 2
    invalid = b'data: {"jsonrpc":"1.0","method":"notice"}' + newline * 2
    raw = comment + invalid
    stream, _, failures = framed_stream([raw[i:i + 8191] for i in range(0, len(raw), 8191)])
    with pytest.raises(ForgePilotError, match="mcp_protocol"):
        async for _ in stream:
            pass
    assert failures and failures[0].code == "mcp_protocol"


@pytest.mark.asyncio
async def test_oversized_event_stops_before_reading_later_chunks():
    stream, source, failures = framed_stream([b":" + b"x" * MAX_MESSAGE, b"never consumed"])
    with pytest.raises(ForgePilotError, match="mcp_limit"):
        async for _ in stream:
            pass
    assert source.emitted == 1
    assert failures and failures[0].code == "mcp_limit"
