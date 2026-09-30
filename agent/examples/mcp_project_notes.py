"""Local read-only MCP example. Run with the existing Python: python -I -u <this-file>.

Only preset strings are returned; no file, shell, network or repository operations.
stdout contains newline-delimited MCP JSON-RPC only. EOF ends the process.
"""
import json
import sys

VERSION = "2025-11-25"
NOTES = {
    "overview": "ForgePilot is a local research and development agent for CMake C++ and Qt6 projects.",
    "architecture": "The Qt Quick client uses HTTP and SSE. The Python Agent Runtime owns reasoning, tools and approvals.",
    "workflow": "Analyze, propose a plan, obtain user approval, modify approved files, configure, build and test; repair at most three times.",
}
TOOL = {"name": "project_info", "description": "Query preset ForgePilot project notes (read only).",
        "inputSchema": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": list(NOTES)}},
            "required": ["topic"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}


def serve():
    initialized = requested = False
    for line in sys.stdin.buffer:
        if len(line) > 262_144:
            return 1
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                return 1
            method, request_id = msg.get("method"), msg.get("id")
            if method == "notifications/initialized" and requested:
                initialized = True
                continue
            if request_id is None:
                continue
            error, result = None, None
            if method == "initialize" and not requested:
                requested = True
                result = {"protocolVersion": VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "forgepilot-project-notes", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif not initialized:
                error = {"code": -32002, "message": "Initialize and notifications/initialized are required"}
            elif method == "tools/list":
                result = {"tools": [TOOL]}
            elif method == "tools/call":
                params = msg.get("params", {})
                args = params.get("arguments", {})
                if params.get("name") != "project_info":
                    error = {"code": -32602, "message": "Unknown tool"}
                elif (not isinstance(args, dict) or set(args) != {"topic"}
                        or not isinstance(args["topic"], str) or args["topic"] not in NOTES):
                    result = {"isError": True, "content": [{"type": "text", "text": "Invalid project_info topic"}]}
                else:
                    result = {"isError": False, "content": [{"type": "text", "text": NOTES[args["topic"]]}]}
            else:
                error = {"code": -32601, "message": "Method not supported"}
            response = {"jsonrpc": "2.0", "id": request_id}
            response.update({"error": error} if error else {"result": result})
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
        except (ValueError, TypeError, KeyError, AttributeError):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
