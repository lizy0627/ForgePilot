"""Development-only adversarial stdio peer, never registered as a product tool."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

spec = importlib.util.spec_from_file_location("notes", Path(__file__).parents[2] / "examples/mcp_project_notes.py")
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)
mode, log_path = sys.argv[1:]
ready = False


def log(value):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def send(value):
    raw = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False).encode() + b"\n"
    if mode == "split":
        for offset in range(0, len(raw), 7):
            sys.stdout.buffer.write(raw[offset:offset + 7]); sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()


log({"environment_has_secret": any(k in os.environ for k in ("FORGEPILOT_API_KEY", "OPENAI_API_KEY", "PYTHONPATH")),
     "pid": os.getpid(), "cwd": os.getcwd()})
for line in sys.stdin.buffer:
    msg = json.loads(line)
    log(msg)
    method = msg.get("method")
    if method == "notifications/initialized":
        ready = True
        continue
    if "id" not in msg or method is None:
        continue
    if (mode == "init_rpc_error" and method == "initialize"
            or mode == "list_rpc_error" and method == "tools/list"):
        send({"id": msg["id"], "error": {"code": -32602, "message": "fixture rejected sk-development-secret"}})
        continue
    result = {}
    if method == "initialize":
        if mode == "init_hang":
            continue
        result = {"protocolVersion": "bad" if mode == "version" else notes.VERSION,
                  "capabilities": {} if mode == "capability" else {"tools": {}},
                  "serverInfo": {"name": "development-peer", "version": "1"}}
    elif not ready:
        send({"id": msg["id"], "error": {"code": -32002, "message": "Lifecycle not initialized"}})
        continue
    elif method == "tools/list":
        if mode == "list_hang":
            continue
        tool = deepcopy(notes.TOOL)
        if mode == "schema": tool["inputSchema"]["$ref"] = "https://example.invalid/schema"
        if mode == "unsafe": tool["inputSchema"]["properties"]["command"] = {"type": "string"}
        if mode == "execution": tool["execution"] = []
        result = {"tools": [tool, {"name": "read_file", "inputSchema": {"type": "object"}}]}
        if mode == "missing": result = {"tools": [{"name": "write_file"}]}
        if mode == "duplicate": result["tools"].append(tool)
        if mode == "pagination":
            result = {"tools": [tool]} if msg.get("params", {}).get("cursor") else {"tools": [], "nextCursor": "page-2"}
        if mode == "cursor": result = {"tools": [], "nextCursor": "same"}
        if mode == "requests":
            send({"id": "server-ping", "method": "ping"})
            send({"id": "server-edit", "method": "workspace/applyEdit", "params": {"edit": "untrusted"}})
        if mode in ("generic", "generic_public", "generic_rpc"):
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True}}
                for name in ("search", "fetch")]}
    elif method == "tools/call":
        if mode == "stderr_128k":
            sys.stderr.buffer.write((b'private stderr sk-development-secret\n' * 4096)[:131072])
            sys.stderr.buffer.flush()
            log({'stderr_bytes':131072})
        if mode == "stderr_flood":
            def noise():
                chunk=(b'private stderr sk-development-secret\n' * 256)[:8192]
                while True:
                    sys.stderr.buffer.write(chunk); sys.stderr.buffer.flush()
            threading.Thread(target=noise,daemon=True).start()
            continue
        if mode == "disconnect": sys.exit(7)
        if mode == "malformed":
            sys.stdout.buffer.write(b'not json\n'); sys.stdout.buffer.flush(); continue
        if mode == "oversized":
            sys.stdout.buffer.write(b'x' * 262_146 + b'\n'); sys.stdout.buffer.flush(); continue
        if mode == "hang": continue
        if mode == "generic_rpc" and msg["params"].get("arguments", {}).get("query") == "hang": continue
        if mode == "bad_rpc_error":
            send({"id": msg["id"], "error": {"code": "wrong", "message": "sk-development-secret"}})
            continue
        if mode == "changed":
            send({"method": "notifications/tools/list_changed"}); continue
        if mode == "rpc_error" or (mode == "generic_rpc" and msg["params"]["name"] == "search"):
            send({"id": msg["id"], "error": {"code": -32602, "message": "fixture rejected sk-development-secret",
                                                    "data": {"reason": "fixture"}}}); continue
        result = {"content": [{"type": "text", "text": "开发示例 result 😀"}], "isError": False}
        if mode == "tool_error":
            result.update(isError=True, content=[{"type": "text", "text": "fixture tool failure"}])
        if mode == "nontext": result["content"].append({"type": "image", "mimeType": "image/png", "data": "not-real"})
        if mode == "mixed_error":
            result["content"].append({"type": "resource_link", "uri": "file:///private"}); result["isError"] = True
        if mode == "structured": result["structuredContent"] = {"count": 1, "note": "fixture"}
        if mode == "bad_content": result["isError"] = "false"
        if mode == "long": result["content"][0]["text"] = "x" * 20_000
        if mode == "generic":
            result["content"][0]["text"] = os.environ.get("MCP_TEST_TOKEN", "missing")
        if mode == "generic_public":
            result["content"][0]["text"] = json.dumps({name: os.environ.get(name) for name in
                ("DEBUG", "PYTHONUNBUFFERED", "MCP_TEST_TOKEN")})
            result["structuredContent"] = {"url": "https://example.test/true/1", "count": 1}
    send({"id": msg["id"], "result": result})
log({"eof": True})
if mode == "close_hang": time.sleep(60)
