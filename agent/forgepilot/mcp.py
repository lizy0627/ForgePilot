"""MCP 2025-11-25 JSON-RPC over stdio, with an owned Windows process tree."""
import asyncio
import json
import os
import subprocess

from .build_process import ProcessJob
from .errors import ForgePilotError, MCPRequestError
from .frozen_runtime import owned_worker_command
from .security import Redactor
from .mcp_config import sensitive_values
from .process import wait_for_cleanup

PROTOCOL_VERSION = "2025-11-25"
MAX_MESSAGE = 262_144
MAX_TRAFFIC = 4_194_304


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Non-finite JSON")
    return json.loads(data, object_pairs_hook=unique, parse_constant=constant)


class MCPClient:
    def __init__(self, config):
        self.config = config.checked()
        self.redactor = Redactor(sensitive_values(config, {"env:" + name: value for name, value in config.env}))
        self.process = self.job = self.launcher = None
        self.reader = self.stderr_reader = self.closer = None
        self.pending = {}
        self.sequence = self.received = 0
        self.initialized = False
        self.failure = None

    async def start(self):
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self):
        if self.launcher is not None or self.closer is not None:
            raise ForgePilotError("mcp_state", "MCP session cannot be started twice")
        self.job = ProcessJob()
        # Reuse the existing trusted stdio Job launcher. No repository cwd, PATH,
        # Python startup hooks, API credentials or user-selected environment is inherited.
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(self.config.env)
        self.launcher = asyncio.create_task(asyncio.create_subprocess_exec(
            *owned_worker_command("stdio", self.job.name, self.config.command, *self.config.args),
            cwd=self.config.cwd, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE))
        try:
            self.process = await asyncio.shield(self.launcher)
        except OSError:
            raise ForgePilotError("mcp_unavailable", "Cannot start configured MCP server") from None
        self.reader = asyncio.create_task(self._read_loop())
        self.stderr_reader = asyncio.create_task(self._stderr())
        result = await self._request("initialize", {"protocolVersion": PROTOCOL_VERSION,
            "capabilities": {}, "clientInfo": {"name": "ForgePilot", "version": "1"}})
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise ForgePilotError("mcp_version", "MCP server did not negotiate supported version 2025-11-25")
        caps, info = result.get("capabilities"), result.get("serverInfo")
        if (not isinstance(caps, dict) or not isinstance(caps.get("tools"), dict)
                or not isinstance(info, dict) or not all(isinstance(info.get(k), str) for k in ("name", "version"))):
            raise ForgePilotError("mcp_capability", "MCP server lacks valid tools capability/server information")
        await self._send({"method": "notifications/initialized"})
        self.initialized = True
        self.server_info = self.redactor.data(info)

    def _fail(self, error):
        self.failure = self.failure or error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        if self.job is not None:
            self.job.terminate()

    async def _stderr(self):
        count = 0
        while chunk := await self.process.stderr.read(8192):
            count += len(chunk)
            # Drain and discard; never log raw server diagnostics or inherited secrets.
            if count >= 65_536:
                count -= 65_536
                await asyncio.sleep(0)

    async def _send(self, message):
        try:
            data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if len(data) > MAX_MESSAGE:
                raise ForgePilotError("mcp_limit", "MCP outgoing message byte limit reached")
            self.process.stdin.write(data)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ForgePilotError("mcp_disconnected", "MCP server stdio closed") from None

    async def _read_loop(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise ForgePilotError("mcp_disconnected", "MCP server disconnected before session ended")
                self.received += len(line)
                if len(line) > MAX_MESSAGE or self.received > MAX_TRAFFIC:
                    raise ForgePilotError("mcp_limit", "MCP incoming traffic limit reached")
                if not line.endswith(b"\n"):
                    raise ValueError()
                msg = decode(line.decode("utf-8"))
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    raise ValueError()
                if "method" in msg:
                    if not isinstance(msg["method"], str) or "result" in msg or "error" in msg:
                        raise ValueError()
                    if "id" in msg:
                        if type(msg["id"]) not in (str, int):
                            raise ValueError()
                        # No roots, sampling, elicitation, edits or commands are granted.
                        response = {"result": {}} if msg["method"] == "ping" else {
                            "error": {"code": -32601, "message": "Client method not supported"}}
                        await self._send({"id": msg["id"], **response})
                    elif msg["method"] == "notifications/tools/list_changed":
                        raise ForgePilotError("mcp_catalog_changed", "MCP tool catalog changed; start a new task to rediscover")
                    # Optional progress/logging notifications do not reset deadlines or grant authority.
                    continue
                request_id = msg.get("id")
                if type(request_id) is not int or request_id not in self.pending or ("result" in msg) == ("error" in msg):
                    raise ValueError()
                future = self.pending[request_id]
                if not future.done():
                    future.set_result(msg)
        except asyncio.CancelledError:
            raise
        except ForgePilotError as exc:
            self._fail(exc)
        except (ValueError, UnicodeError, RecursionError):
            self._fail(ForgePilotError("mcp_protocol", "Invalid MCP JSON-RPC stdio message"))
        except OSError:
            self._fail(ForgePilotError("mcp_disconnected", "MCP server pipe failed"))

    async def _request(self, method, params):
        if self.failure:
            raise self.failure
        if self.closer is not None:
            raise ForgePilotError("mcp_state", "MCP session is closed")
        if method != "initialize" and not self.initialized:
            raise ForgePilotError("mcp_state", "MCP session is not initialized")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        sent = False
        try:
            async with asyncio.timeout(self.config.request_timeout):
                await self._send({"id": request_id, "method": method, "params": params})
                sent = True
                message = await future
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                    raise ForgePilotError("mcp_protocol", "Invalid MCP error response")
                # Registry redacts before this reaches any model, trace or UI.
                detail = json.dumps(self.redactor.data(error), ensure_ascii=False)
                if len(detail) > 4096:
                    detail = detail[:4096] + " [TRUNCATED: MCP error limit]"
                raise MCPRequestError(detail)
            result = message["result"]
            if not isinstance(result, dict):
                raise ForgePilotError("mcp_protocol", "MCP response result must be an object")
            return result
        except (TimeoutError, asyncio.CancelledError) as exc:
            if sent and method != "initialize":  # MCP explicitly forbids cancelling initialize.
                try:
                    async with asyncio.timeout(0.2):
                        await self._send({"method": "notifications/cancelled", "params": {
                            "requestId": request_id, "reason": "ForgePilot request stopped"}})
                except (ForgePilotError, TimeoutError):
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ForgePilotError("mcp_timeout", "MCP request timed out") from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a simultaneous disconnect while send was failing.

    async def list_tools(self):
        try:
            return await collect_tools(self._request)
        except BaseException:
            await self.close()
            raise

    async def call_tool(self, name, arguments):
        try:
            return await self._request("tools/call", {"name": name, "arguments": arguments})
        except MCPRequestError:
            raise
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.closer is None:
            self.closer = asyncio.create_task(self._close_owned())
        await wait_for_cleanup(self.closer)

    async def _close_owned(self):
        self.failure = self.failure or ForgePilotError("mcp_state", "MCP session is closed")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        try:
            if self.launcher is not None and self.process is None:
                try:
                    self.process = await self.launcher
                except OSError:
                    pass
            if self.process is not None:
                self.process.stdin.close()
                try:
                    async with asyncio.timeout(0.5):
                        await self.process.wait()
                except TimeoutError:
                    pass
        finally:
            if self.job is not None:
                self.job.terminate()
                self.job.close()
            if self.process is not None:
                if self.process.returncode is None:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                if self.reader is not None:
                    self.reader.cancel()
                    await asyncio.gather(self.reader, return_exceptions=True)
                async def drain(stream):
                    while await stream.read(65_536):
                        pass
                await asyncio.gather(self.process.wait(), drain(self.process.stdout),
                    self.stderr_reader if self.stderr_reader is not None else drain(self.process.stderr),
                    return_exceptions=True)


async def collect_tools(request):
    """Transport-neutral bounded pagination; schema compatibility is per tool."""
    tools, names, cursors = [], set(), set()
    cursor = None
    for _ in range(8):
        result = await request("tools/list", {} if cursor is None else {"cursor": cursor})
        page = result.get("tools")
        if not isinstance(page, list):
            raise ForgePilotError("mcp_protocol", "Invalid MCP tools/list result")
        for tool in page:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not 1 <= len(name) <= 128 or name in names:
                raise ForgePilotError("mcp_catalog", "MCP catalog has invalid or duplicate tool names")
            names.add(name)
            tools.append(tool)
        if len(tools) > 128:
            raise ForgePilotError("mcp_limit", "MCP tool discovery limit reached")
        if len(json.dumps(tools, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_MESSAGE:
            raise ForgePilotError("mcp_limit", "MCP catalog exceeds 256 KiB")
        cursor = result.get("nextCursor")
        if cursor is None:
            return tools
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in cursors:
            raise ForgePilotError("mcp_catalog", "Invalid MCP pagination cursor")
        cursors.add(cursor)
    raise ForgePilotError("mcp_limit", "MCP discovery page limit reached")
