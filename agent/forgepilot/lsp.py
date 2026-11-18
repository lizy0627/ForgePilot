"""Small LSP 3.17 stdio client; no editor actions, shell commands or server-side authority."""
import asyncio
import json
import os
import subprocess

from .build_process import ProcessJob, build_environment
from .errors import ForgePilotError
from .frozen_runtime import owned_worker_command
from .process import wait_for_cleanup

MAX_MESSAGE = 4_194_304
MAX_TRAFFIC = 16_777_216


def frame(message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ForgePilotError("clangd_limit", "LSP message exceeds the byte limit")
    return f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data


class LspClient:
    def __init__(self, command, root, *, request_timeout=8.0):
        self.command, self.root, self.request_timeout = command, root, request_timeout
        self.process = self.job = self.launcher = self.stderr_reader = None
        self.sequence = self.received = 0
        self.initialized = False
        self.documents = {}
        self.diagnostics = {}
        self.stderr_limited = False
        self.closer = None

    async def start(self):
        self.job = ProcessJob()
        self.launcher = asyncio.create_task(asyncio.create_subprocess_exec(
            *owned_worker_command("stdio", self.job.name, *self.command),
            cwd=self.root, env=build_environment(), creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_MESSAGE + 8192))
        try:
            self.process = await asyncio.shield(self.launcher)
        except OSError:
            raise ForgePilotError("clangd_unavailable", "Cannot start clangd") from None
        self.stderr_reader = asyncio.create_task(self._stderr())
        # Process creation through the isolated worker can exceed an intentionally
        # tiny per-request timeout on loaded Windows hosts.  Keep protocol
        # requests strict while giving startup a small, bounded floor.
        response = await self.request("initialize", {
            "processId": os.getpid(), "rootUri": self.root.as_uri(),
            "clientInfo": {"name": "ForgePilot", "version": "1"},
            "capabilities": {"general": {"positionEncodings": ["utf-16"]},
                "textDocument": {"definition": {"linkSupport": True},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}}},
            "trace": "off"}, timeout=max(self.request_timeout, 2.0))
        capabilities = response.get("capabilities", {}) if isinstance(response, dict) else {}
        if not isinstance(capabilities, dict):
            raise ForgePilotError("clangd_protocol", "Invalid initialize capabilities")
        if capabilities.get("positionEncoding", "utf-16") != "utf-16":
            raise ForgePilotError("clangd_encoding", "Server did not negotiate UTF-16 positions")
        if not all(capabilities.get(name) is not None and capabilities[name] is not False
                   for name in ("definitionProvider", "referencesProvider", "documentSymbolProvider")):
            raise ForgePilotError("clangd_capability", "Server lacks the required LSP capabilities")
        await self.notify("initialized", {})
        self.initialized = True

    async def _stderr(self):
        # Drain without retaining untrusted diagnostics, file contents or authentication data.
        count = 0
        while chunk := await self.process.stderr.read(8192):
            count += len(chunk)
            if count > 65_536:
                self.stderr_limited = True
                self.job.terminate()
                return

    async def send(self, message):
        try:
            self.process.stdin.write(frame({"jsonrpc": "2.0", **message}))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ForgePilotError("clangd_exited", "clangd stdio closed") from None

    async def notify(self, method, params=None):
        await self.send({"method": method, **({"params": params} if params is not None else {})})

    async def receive(self):
        try:
            headers = {}
            size = 0
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise ForgePilotError("clangd_exited", "clangd exited before replying")
                size += len(line)
                if size > 8192 or not line.endswith(b"\r\n"):
                    raise ValueError()
                if line == b"\r\n":
                    break
                key, value = line[:-2].decode("ascii").split(":", 1)
                key = key.casefold()
                if key in headers:
                    raise ValueError()
                headers[key] = value.strip()
            length = headers.get("content-length", "")
            if not length.isascii() or not length.isdigit() or len(length) > 9:
                raise ValueError()
            length = int(length)
            self.received += size + length
            if not 0 < length <= MAX_MESSAGE or self.received > MAX_TRAFFIC or self.stderr_limited:
                raise ForgePilotError("clangd_limit", "LSP traffic limit reached")
            content_type = headers.get("content-type", "application/vscode-jsonrpc; charset=utf-8").lower().replace(" ", "")
            if "charset=" in content_type and content_type.split("charset=", 1)[1] not in {"utf-8", "utf8"}:
                raise ValueError()
            value = json.loads((await self.process.stdout.readexactly(length)).decode("utf-8"))
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ValueError()
            return value
        except asyncio.IncompleteReadError:
            raise ForgePilotError("clangd_exited", "clangd returned an incomplete frame") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ForgePilotError("clangd_protocol", "Invalid LSP frame") from None

    async def request(self, method, params=None, *, timeout=None):
        self.sequence += 1
        request_id = self.sequence
        try:
            async with asyncio.timeout(self.request_timeout if timeout is None else timeout):
                await self.send({"id": request_id, "method": method, **({"params": params} if params is not None else {})})
                while True:
                    message = await self.receive()
                    if "method" in message:
                        if "id" in message:
                            # Never honor workspace/applyEdit, executeCommand or configuration authority.
                            await self.send({"id": message["id"], "error": {"code": -32601, "message": "Client method not supported"}})
                        elif message["method"] == "textDocument/publishDiagnostics":
                            data = message.get("params", {})
                            if isinstance(data, dict) and isinstance(data.get("diagnostics"), list):
                                uri = data.get("uri")
                                if not isinstance(uri, str):
                                    raise ForgePilotError("clangd_protocol", "Invalid diagnostic URI")
                                self.diagnostics[uri] = any(
                                    isinstance(d, dict) and d.get("severity") == 1 for d in data["diagnostics"])
                        continue
                    if message.get("id") != request_id:
                        continue  # E.g. a late response to a cancelled request during shutdown.
                    if "error" in message:
                        raise ForgePilotError("clangd_request", "clangd rejected the LSP request")
                    if "result" not in message:
                        raise ForgePilotError("clangd_protocol", "LSP response has no result")
                    return message["result"]
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                async with asyncio.timeout(0.2):
                    await self.notify("$/cancelRequest", {"id": request_id})
            except (ForgePilotError, TimeoutError):
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ForgePilotError("clangd_timeout", "clangd request timed out") from None

    async def sync(self, path, text):
        uri = path.as_uri()
        previous = self.documents.get(uri)
        if previous is None:
            version = 1
            await self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "cpp", "version": version, "text": text}})
        elif previous[1] != text:
            version = previous[0] + 1
            await self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}]})
        else:
            return
        self.documents[uri] = (version, text)
        # A document-symbol request waits for this document's AST; no guessed sleep/index readiness.
        await self.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})

    async def close(self):
        # Shutdown itself is owned work: a second cancel must not abandon pipes or the Job.
        if self.closer is None:
            self.closer = asyncio.create_task(self._close_owned())
        await wait_for_cleanup(self.closer)

    async def _close_owned(self):
        try:
            if self.launcher is not None and self.process is None:
                try:
                    self.process = await self.launcher
                except OSError:
                    pass
            if self.process is not None and self.process.returncode is None and self.initialized:
                try:
                    async with asyncio.timeout(1.0):
                        for uri in self.documents:
                            await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                        await self.request("shutdown", timeout=0.5)
                        await self.notify("exit")
                        self.process.stdin.close()
                        await self.process.wait()
                except (ForgePilotError, TimeoutError, OSError):
                    pass
        finally:
            if self.job is not None:
                self.job.terminate()
                # Close before draining: a worker cancelled before joining must not join
                # an otherwise still-live empty Job and launch a new server after terminate.
                self.job.close()
            if self.process is not None:
                if self.process.returncode is None:
                    try:
                        self.process.kill()  # Covers the root launcher before it joins the Job.
                    except ProcessLookupError:
                        pass
                async def drain(stream):
                    while await stream.read(65_536):
                        pass
                await asyncio.gather(self.process.wait(), drain(self.process.stdout),
                    self.stderr_reader if self.stderr_reader is not None else drain(self.process.stderr),
                    return_exceptions=True)
