"""Bounded mission diagnostics around the shared model and tool services."""
import json
import re
from uuid import uuid4

from .artifacts.canonical import content_sha256
from .errors import ForgePilotError
from .tools.change_tools import WRITE_TOOLS


_FILE_READ_TOOLS = frozenset({"read_file", "repository_read_range", "repository_read_cmake_target"})
_SAFE_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_AUDIT_DENIAL_CODES = frozenset({"policy_denied", "path_escape", "file_denied",
    "scope_approval_required", "test_approval_required", "unknown_tool", "invalid_arguments"})


def _enabled(events, task_id):
    return events.missions.is_admitted(task_id)


def _emit(events, context, kind, data):
    events.missions.record_observation(context.task_id, kind, data,
        node_id=context.node.node_id, actor=context.node.agent_id)


async def model_call(events, context, definition, client, messages, tools, **kwargs):
    if not _enabled(events, context.task_id):
        return await client.chat(messages, tools, is_cancelled=context.is_cancelled, **kwargs)
    def record_attempt(record):
        events.missions.record_model_call(
            call_id=str(uuid4()), task_id=context.task_id, node_id=context.node.node_id,
            role=definition.role, model=record["model"], prompt_version=definition.prompt_version,
            input_hash=record["input_hash"], tool_schema={"names": [tool.name for tool in tools or ()]},
            duration_ms=record["duration_ms"], input_tokens=record["input_tokens"],
            output_tokens=record["output_tokens"], total_tokens=record["input_tokens"] + record["output_tokens"],
            retry_count=record["retry_count"], fallback_model=record["model"] if record["fallback_count"] else None,
            response_hash=record["response_hash"], error_code=None if record["status"] == "success" else record["status"],
        )
    # Context-local accounting isolates concurrent Analysts and records every
    # actual provider attempt, including retries/fallbacks and failed responses.
    with client.gateway.accounting.scoped_sink(record_attempt):
        return await client.chat(messages, tools, is_cancelled=context.is_cancelled, **kwargs)


def validate_tool(events, context, registry, name, arguments, **kwargs):
    try:
        return registry.validate_call(name, arguments, **kwargs)
    except ForgePilotError as exc:
        if _enabled(events, context.task_id):
            code = exc.code if _SAFE_ERROR_CODE.fullmatch(exc.code) else "tool_denied"
            safe_tool = name if registry.get(name) is not None else "unknown_tool"
            events.missions.record_security_event(
                event_id=str(uuid4()), task_id=context.task_id,
                event_type="tool_denied", severity="warning",
                attributes={"code": code if code in _AUDIT_DENIAL_CODES else "other_denial",
                            "boundary": "tool_validation"},
            )
            _emit(events, context, "security.denied", {"tool": safe_tool, "error_code": code})
        raise


async def tool_call(events, context, registry, name, arguments):
    result = await registry.execute(name, arguments)
    if _enabled(events, context.task_id):
        # Never publish raw arguments, file contents, tool output or environments.
        error_code = result.error.split(":", 1)[0] if result.error else None
        if error_code is not None and not _SAFE_ERROR_CODE.fullmatch(error_code):
            error_code = "tool_error"
        data = {"tool": name, "arguments_hash": content_sha256(arguments),
                "success": result.success, "duration_ms": result.duration_ms,
                "error_code": error_code,
                "operation": "write" if name in WRITE_TOOLS else "read" if name in _FILE_READ_TOOLS else "other"}
        observed_arguments = arguments
        if isinstance(arguments, str) and len(arguments) <= 65_536:
            try:
                observed_arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                observed_arguments = None
        if isinstance(observed_arguments, dict) and isinstance(observed_arguments.get("path"), str):
            data["path"] = observed_arguments["path"][:4096]
        _emit(events, context, "tool.completed", data)
        if name.startswith("mcp_"):
            _emit(events, context, "task.tools_updated", {"component": "mcp", "status": {
                "used": result.success, "fallback_reasons": [] if result.success else [error_code or "mcp_unavailable"]}})
        if not result.success and data["error_code"] in {
                "policy_denied", "path_escape", "file_denied", "scope_approval_required", "test_approval_required"}:
            events.missions.record_security_event(
                event_id=str(uuid4()), task_id=context.task_id,
                event_type="tool_denied", severity="warning",
                attributes={"code": data["error_code"], "boundary": "tool_execution"},
            )
            _emit(events, context, "security.denied", {"tool": name, "error_code": data["error_code"]})
    return result
