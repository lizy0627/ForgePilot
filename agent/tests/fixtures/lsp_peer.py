"""Explicit test-only LSP peer; never used by production configuration."""
import json
import os
from pathlib import Path
import sys

mode, log = sys.argv[1], Path(sys.argv[2])


def send(message):
    data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode()
    value = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
    if mode == "fragment":
        for byte in value:
            sys.stdout.buffer.write(bytes([byte])); sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(value); sys.stdout.buffer.flush()


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1); headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps({**message, "peer_pid": os.getpid(), "credential_present": "FORGEPILOT_API_KEY" in os.environ}, ensure_ascii=False) + "\n")
    method = message.get("method")
    if method == "exit":
        sys.exit(0)
    if method is None or "id" not in message:
        continue
    id = message["id"]
    if method == "initialize":
        if mode == "bad_initialize":
            send({"id": id, "result": {"capabilities": []}}); continue
        send({"id": id, "result": {"serverInfo": {"name": "开发测试😀"}, "capabilities": {
            "positionEncoding": "utf-16", "definitionProvider": True, "referencesProvider": True, "documentSymbolProvider": True}}})
    elif method == "shutdown":
        if mode != "shutdown_hang": send({"id": id, "result": None})
    elif method == "textDocument/documentSymbol":
        send({"id": id, "result": []})
    elif method in {"textDocument/definition", "textDocument/references"}:
        if mode == "exit": sys.exit(9)
        if mode == "hang": continue
        if mode == "oversized":
            sys.stdout.buffer.write(b"Content-Length: 999999999\r\n\r\n"); sys.stdout.buffer.flush(); continue
        if mode == "request_error": send({"id": id, "error": {"code": -32603, "message": "development fixture"}}); continue
        if mode == "diagnostic":
            send({"method": "textDocument/publishDiagnostics", "params": {
                "uri": message["params"]["textDocument"]["uri"], "diagnostics": [{"severity": 1, "message": "test parse error"}]}})
        # A server must not be able to edit the repository through a client request.
        send({"id": "server-edit", "method": "workspace/applyEdit", "params": {"edit": {}}})
        send({"id": id, "result": []})
    else:
        send({"id": id, "result": None})
