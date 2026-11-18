"""Official MCP 1.x Streamable HTTP in a single context-owning coroutine.

The SDK owns JSON/SSE routing and session IDs. Our transport bounds bytes before
SDK buffering, fixes connection metadata, and rejects redirects/compression.
Raw Result is intentional: SDK tools/list/call_tool would validate the whole
catalog/output before our per-tool, local-reference-only validation could run.
"""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import re

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from .errors import ForgePilotError, MCPTransportError, MCPRequestError
from .mcp import MAX_MESSAGE, MAX_TRAFFIC, PROTOCOL_VERSION, collect_tools, decode
from .security import Redactor
from .process import wait_for_cleanup

_private_session = ContextVar("forgepilot_mcp_http_private", default=False)
_sse_newlines = re.compile(rb"[\r\n]")


class _PrivateLogs(logging.Filter):
    def filter(self, record):
        # SDK debug/exception logs include complete untrusted messages and session
        # headers. Suppress only logs emitted within our owner and its children.
        return not _private_session.get()


for _name in ("mcp.client.streamable_http", "mcp.shared.session", "client",
              "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
    logging.getLogger(_name).addFilter(_PrivateLogs())


def _error(exc):
    if isinstance(exc, ForgePilotError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        return _error(exc.exceptions[0])
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return ForgePilotError("mcp_timeout", "MCP HTTP request timed out")
    if isinstance(exc, httpx.TransportError):
        return ForgePilotError("mcp_disconnected", "MCP HTTP connection failed")
    if isinstance(exc, McpError):
        # Remote exception text/data never escapes the transport boundary.
        return ForgePilotError("mcp_request", "MCP server rejected the request")
    return ForgePilotError("mcp_protocol", "Invalid MCP HTTP response or session")


def _retry_after(value):
    if value is None or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is not None:
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    return None


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream, owner, sse):
        self.stream, self.owner, self.sse = stream, owner, sse

    async def __aiter__(self):
        size = 0
        line_size = 0
        previous_cr = False
        buffer = bytearray()
        try:
            async for chunk in self.stream:
                self.owner.received += len(chunk)
                if self.owner.received > MAX_TRAFFIC:
                    raise ForgePilotError("mcp_limit", "MCP session exceeds 4 MiB")
                if not self.sse:
                    size += len(chunk)
                    if size > MAX_MESSAGE:
                        raise ForgePilotError("mcp_limit", "MCP message exceeds 256 KiB")
                    buffer.extend(chunk)
                else:
                    # Bound an entire SSE event, including data/comment/id lines.
                    # Handle LF, CRLF and CR, including separators split in chunks.
                    position = 0
                    for newline in _sse_newlines.finditer(chunk):
                        end = newline.end()
                        length = end - position
                        size += length
                        if size > MAX_MESSAGE:
                            raise ForgePilotError("mcp_limit", "MCP SSE event exceeds 256 KiB")
                        buffer.extend(chunk[position:end])
                        position = end
                        if length > 1:
                            line_size += length - 1
                            previous_cr = False
                        byte = chunk[newline.start()]
                        if byte == 10 and previous_cr:
                            previous_cr = False
                            continue
                        if line_size == 0:
                            self._event(bytes(buffer))
                            buffer.clear()
                            size = 0
                        line_size = 0
                        previous_cr = byte == 13
                    remaining = len(chunk) - position
                    if remaining:
                        size += remaining
                        if size > MAX_MESSAGE:
                            raise ForgePilotError("mcp_limit", "MCP SSE event exceeds 256 KiB")
                        buffer.extend(chunk[position:])
                        line_size += remaining
                        previous_cr = False
                yield chunk
            if not self.sse and buffer:
                self._message(bytes(buffer))
        except Exception as exc:
            self.owner._abort(_error(exc))
            raise

    def _event(self, raw):
        lines = raw.decode("utf-8-sig").splitlines()
        data, event = [], "message"
        for line in lines:
            key, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if key == "data":
                data.append(value)
            elif key == "event":
                event = value
        if data and "\n".join(data):
            if event != "message":
                raise ForgePilotError("mcp_transport_unsupported", "Only Streamable HTTP message events are supported")
            self._message("\n".join(data))

    def _message(self, raw):
        message = decode(raw)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ForgePilotError("mcp_protocol", "Invalid MCP JSON-RPC message")
        if "method" in message:
            if (not isinstance(message["method"], str) or "result" in message or "error" in message
                    or ("params" in message and not isinstance(message["params"], dict))
                    or ("id" in message and type(message["id"]) not in (int, str))):
                raise ForgePilotError("mcp_protocol", "Invalid MCP request or notification")
        else:
            if (type(message.get("id")) not in (int, str)
                    or ("result" in message) == ("error" in message)
                    or ("result" in message and not isinstance(message["result"], dict))):
                raise ForgePilotError("mcp_protocol", "Invalid MCP response")
            if "error" in message:
                error = message["error"]
                if (not isinstance(error, dict) or type(error.get("code")) is not int
                        or not isinstance(error.get("message"), str)):
                    raise ForgePilotError("mcp_protocol", "Invalid MCP error")
                active = self.owner._active_request
                if active is not None and type(message["id"]) is type(active[0]) and message["id"] == active[0]:
                    # McpError also represents SDK-local timeouts/closed streams.
                    # Keep only structural proof of a validated remote response.
                    self.owner._validated_rpc_error = (active, error["code"])

    async def aclose(self):
        await self.stream.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, owner):
        self.owner = owner
        self.inner = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        self.closed = False

    async def handle_async_request(self, request):
        response = None
        try:
            if request.url != httpx.URL(self.owner.config.endpoint):
                raise ForgePilotError("mcp_redirect", "Configure the final MCP endpoint; redirects are disabled")
            if request.method == "DELETE":
                request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"), 1.0)
            elif request.method == "POST":
                body = request.content
                if len(body) > MAX_MESSAGE:
                    raise ForgePilotError("mcp_limit", "MCP outgoing message exceeds 256 KiB")
                message = decode(body)
                if "id" in message and "method" in message:
                    self.owner._active_request = (message["id"], message["method"])
                    self.owner._validated_rpc_error = None
            response = await self.inner.handle_async_request(request)
            status = response.status_code
            if 300 <= status < 400:
                raise ForgePilotError("mcp_redirect", "Configure the final MCP endpoint; redirects are disabled")
            if status in (401, 403):
                raise ForgePilotError("mcp_auth", "MCP server refused authentication or access")
            if status == 429:
                raise MCPTransportError("mcp_capacity", "MCP server rate limit reached", status_code=status,
                    retry_after_seconds=_retry_after(response.headers.get("retry-after")))
            if 500 <= status < 600:
                raise MCPTransportError("mcp_transient", "MCP server is temporarily unavailable", status_code=status)
            if status == 404 and request.headers.get("mcp-session-id"):
                raise MCPTransportError("mcp_session_expired", "MCP server session expired", status_code=status)
            if status >= 400 and not (status == 405 and request.method in ("GET", "DELETE")):
                code = "mcp_not_mcp" if status in (400, 404, 405) else "mcp_request"
                raise ForgePilotError(code, "MCP HTTP endpoint rejected the request")
            encoding = response.headers.get("content-encoding", "identity").lower()
            if encoding != "identity":
                raise ForgePilotError("mcp_transport_unsupported", "MCP endpoint must honor Accept-Encoding: identity")
            content_type = response.headers.get("content-type", "").split(";")[0].lower().strip()
            if status == 200 and request.method in ("POST", "GET") and content_type not in {
                    "application/json", "text/event-stream"}:
                raise ForgePilotError("mcp_not_mcp", "Expected a Streamable HTTP JSON or SSE endpoint")
            session_id = response.headers.get("mcp-session-id")
            if session_id is not None and (not session_id or len(session_id) > 1024
                    or any(not 0x21 <= ord(c) <= 0x7e for c in session_id)):
                raise ForgePilotError("mcp_protocol", "Invalid MCP session ID")
            # Empty 202/204/405 transport responses have no MCP body to parse.
            if status == 200 and request.method in ("POST", "GET"):
                response.stream = _LimitedStream(response.stream, self.owner, content_type == "text/event-stream")
            else:
                # Error/notification/termination bodies are never useful payloads.
                # Close without downloading them (including unsolicited huge bodies).
                await response.aclose()
                response = httpx.Response(status, headers=response.headers, content=b"")
            return response
        except Exception as exc:
            if response is not None:
                await response.aclose()
            if request.method != "DELETE":
                self.owner._abort(_error(exc))
            raise _error(exc) from None

    async def aclose(self):
        await self.inner.aclose()
        self.closed = True


class MCPHttpClient:
    def __init__(self, config):
        self.config = config.checked()
        self.redactor = Redactor((config.secret,) if config.secret else ())
        self.owner = self.closer = self.ready = None
        self.queue = asyncio.Queue(maxsize=8)
        self.failure = None
        self.received = 0
        self.server_info = None
        self._active_request = None
        self._validated_rpc_error = None
        self._stopping = False
        self.transport = _BoundedTransport(self)
        self.http = None

    def _abort(self, error):
        if self.failure is None:
            self.failure = error
            if self.owner is not None and not self.owner.done() and not self._stopping:
                self._stopping = True
                self.owner.cancel()

    async def start(self):
        if self.owner is not None or self.closer is not None:
            raise ForgePilotError("mcp_state", "MCP session cannot be started twice")
        self.ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._run(), name="mcp-http-owner")
        await self._wait(self.ready)

    async def _wait(self, future):
        try:
            async with asyncio.timeout(self.config.request_timeout):
                return await asyncio.shield(future)
        except (TimeoutError, asyncio.CancelledError) as exc:
            future.cancel()
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ForgePilotError("mcp_timeout", "MCP HTTP request timed out") from None
        except MCPRequestError:
            raise
        except BaseException:
            await self.close()
            raise

    async def _request(self, method, params):
        if self.failure:
            await self.close()
            raise self.failure
        if self.owner is None or self.owner.done() or self.closer is not None or not self.ready.done():
            raise ForgePilotError("mcp_state", "MCP session is not active")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((method, deepcopy(params), future))
        except asyncio.QueueFull:
            raise ForgePilotError("mcp_capacity", "MCP connection request queue is full") from None
        return await self._wait(future)

    async def list_tools(self):
        try:
            async with asyncio.timeout(min(20.0, self.config.request_timeout)):
                return await collect_tools(self._request)
        except TimeoutError:
            await self.close()
            raise ForgePilotError("mcp_timeout", "MCP discovery timed out") from None
        except BaseException:
            await self.close()
            raise

    async def call_tool(self, name, arguments):
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def _message(self, message):
        if isinstance(message, Exception):
            self._abort(_error(message))
        elif isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
            self._abort(ForgePilotError("mcp_catalog_changed", "MCP catalog changed; rediscover tools"))

    async def _run(self):
        token = _private_session.set(True)
        active = None
        try:
            self.http = httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                headers={**self.config.headers(), "Accept-Encoding": "identity"},
                timeout=httpx.Timeout(self.config.request_timeout))
            async with self.http:
                async with streamable_http_client(self.config.endpoint, http_client=self.http) as (read, write, _):
                    async with ClientSession(read, write, message_handler=self._message) as session:
                        try:
                            # Explicit supported version/capabilities; no sampling, roots,
                            # elicitation or task capabilities are advertised.
                            result = await session.send_request(types.ClientRequest(types.InitializeRequest(
                                params=types.InitializeRequestParams(protocolVersion=PROTOCOL_VERSION,
                                    capabilities=types.ClientCapabilities(),
                                    clientInfo=types.Implementation(name="ForgePilot", version="1")))), types.Result)
                            data = result.model_dump(by_alias=True, exclude_none=True)
                            if data.get("protocolVersion") != PROTOCOL_VERSION:
                                raise ForgePilotError("mcp_version", "MCP requires protocol 2025-11-25")
                            caps, info = data.get("capabilities"), data.get("serverInfo")
                            if (not isinstance(caps, dict) or not isinstance(caps.get("tools"), dict)
                                    or not isinstance(info, dict)
                                    or not all(isinstance(info.get(k), str) for k in ("name", "version"))):
                                raise ForgePilotError("mcp_capability", "MCP server must support tools")
                            self.server_info = self.redactor.data(info)
                            await session.send_notification(types.ClientNotification(types.InitializedNotification()))
                            if not self.ready.done():
                                self.ready.set_result(None)
                            self._active_request = None
                            while True:
                                method, params, active = await self.queue.get()
                                if active.cancelled():
                                    continue
                                if method == "tools/list":
                                    request = types.ListToolsRequest(params=types.PaginatedRequestParams(**params))
                                else:
                                    request = types.CallToolRequest(params=types.CallToolRequestParams(**params))
                                try:
                                    result = await session.send_request(types.ClientRequest(request), types.Result)
                                except McpError as exc:
                                    if (method != "tools/call" or self.failure is not None
                                            or self._validated_rpc_error != (self._active_request, exc.error.code)):
                                        raise
                                    if not active.done():
                                        active.set_exception(MCPRequestError())
                                    self._active_request = None
                                    self._validated_rpc_error = None
                                    active = None
                                    continue
                                if not active.done():
                                    data = result.model_dump(by_alias=True)
                                    if data.get("_meta") is None:
                                        data.pop("_meta", None)
                                    active.set_result(data)
                                self._active_request = None
                                self._validated_rpc_error = None
                                active = None
                        except asyncio.CancelledError:
                            if self._active_request and self._active_request[1] != "initialize":
                                try:
                                    async with asyncio.timeout(0.2):
                                        await session.send_notification(types.ClientNotification(types.CancelledNotification(
                                            params=types.CancelledNotificationParams(requestId=self._active_request[0],
                                                reason="ForgePilot request stopped"))))
                                except (Exception, asyncio.CancelledError):
                                    pass
        except BaseException as exc:
            self.failure = self.failure or _error(exc)
        finally:
            error = self.failure or ForgePilotError("mcp_state", "MCP session closed")
            for future in (self.ready, active):
                if future is not None and not future.done():
                    future.set_exception(error)
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(error)
            _private_session.reset(token)

    async def close(self):
        if self.closer is None:
            self.closer = asyncio.create_task(self._close_owned())
        await wait_for_cleanup(self.closer)

    async def _close_owned(self):
        if self.owner is None:
            await self.transport.aclose()
            return
        if not self.owner.done() and not self._stopping:
            self._stopping = True
            self.owner.cancel()
        # Enforce an absolute cleanup budget as well as DELETE's idle timeout.
        # The timer interrupts the owner; it never exits its contexts from here.
        timer = asyncio.get_running_loop().call_later(2.5, self.owner.cancel)
        try:
            await asyncio.gather(self.owner, return_exceptions=True)
        finally:
            timer.cancel()
            if not self.transport.closed:
                await self.transport.aclose()
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(ForgePilotError("mcp_state", "MCP session closed before initialization"))
