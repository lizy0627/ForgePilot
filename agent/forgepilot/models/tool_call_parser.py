"""Bounded Tool Calling validation and ordered, authority-preserving execution.

Model output is data. A tool schema never grants capabilities or path access;
the application-owned registry checks both again immediately before execution.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from ..errors import ForgePilotError
from ..mcp_schema import validate as validate_schema
from ..policy import LEGACY_ALIASES

if TYPE_CHECKING:
    from .contracts import ToolSpec


@dataclass(frozen=True, slots=True)
class ValidatedToolCall:
    id: str
    name: str
    arguments: dict
    wire_call: dict


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("Nonfinite JSON number")


def parse_arguments(arguments: str | dict) -> dict:
    """Parse strict bounded JSON, without including model data in errors."""
    try:
        if isinstance(arguments, str):
            if len(arguments) > 16_384:
                raise ValueError("Arguments too large")
            arguments = json.loads(arguments, object_pairs_hook=_object,
                                   parse_constant=_nonfinite)
        if type(arguments) is not dict:
            raise ValueError("Arguments must be an object")
        if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False)) > 16_384:
            raise ValueError("Arguments too large")
        return arguments
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ForgePilotError("llm_tool_schema", "Tool arguments must be a bounded JSON object") from None


def validate_tool_calls(calls, specs: list[ToolSpec] | None) -> list[ValidatedToolCall]:
    """Validate the entire batch against exactly this request's exposed tools."""
    if calls is None:
        return []
    if not isinstance(calls, (list, tuple)) or len(calls) > 8:
        raise ForgePilotError("llm_tool_protocol", "Invalid tool call batch")
    schemas = {spec.name: spec.input_schema for spec in specs or ()}
    validated = []
    seen = set()
    for value in calls:
        call = value.model_dump() if hasattr(value, "model_dump") else value
        try:
            function = call["function"]
            call_id, name = call["id"], function["name"]
            if (call.get("type") != "function" or type(call_id) is not str
                    or not 1 <= len(call_id) <= 128 or call_id in seen
                    or type(name) is not str or not 1 <= len(name) <= 64
                    or any(ord(char) < 32 or ord(char) == 127 for char in call_id + name)
                    or type(function["arguments"]) is not str):
                raise ValueError("Invalid tool call envelope")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ForgePilotError("llm_tool_protocol", "Invalid or duplicate tool call envelope") from None
        seen.add(call_id)
        canonical = LEGACY_ALIASES.get(name, name)
        schema = schemas.get(name, schemas.get(canonical))
        if schema is None:
            raise ForgePilotError("llm_tool_unknown", "Tool was not exposed for this model request")
        arguments = parse_arguments(function["arguments"])
        try:
            validate_schema(schema, arguments)
        except Exception:
            raise ForgePilotError("llm_tool_schema", "Tool arguments do not match the exposed schema") from None
        validated.append(ValidatedToolCall(call_id, name, arguments, {
            "id": call_id, "type": "function", "function": {
                "name": name, "arguments": function["arguments"],
            },
        }))
    return validated


def preflight_tool_calls(calls, specs, registry, *, capabilities=None, path_validator=None,
                         max_tool_calls=None) -> list[ValidatedToolCall]:
    """Validate all grants/paths before the first tool in a new Agent batch runs."""
    validated = validate_tool_calls(calls, specs)
    if max_tool_calls is not None and len(validated) > max_tool_calls:
        raise ForgePilotError("model_budget", "Tool call budget exhausted")
    for call in validated:
        registry.validate_call(call.name, call.arguments, capabilities=capabilities)
        if path_validator is not None:
            path_validator(call.name, call.arguments)
    return validated


async def execute_tool_calls(calls, specs, registry, *, principal=None, capabilities=None,
                             path_validator=None, max_tool_calls=None) -> list[dict]:
    """Return one sanitized observation per call in stable model order.

The principal and declared capabilities originate in AgentDefinition/application
state, never in the provider, route, fallback or tool arguments.
"""
    scope = registry.as_principal(principal) if principal is not None else nullcontext()
    with scope, registry.as_capabilities(capabilities):
        validated = preflight_tool_calls(calls, specs, registry, capabilities=capabilities,
            path_validator=path_validator, max_tool_calls=max_tool_calls)
        results = []
        for call in validated:
            # Recheck custom boundaries too: previous tools can change paths.
            if path_validator is not None:
                path_validator(call.name, call.arguments)
            result = await registry.execute(call.name, call.arguments)
            results.append({"role": "tool", "tool_call_id": call.id,
                            "content": json.dumps(result.model_dump(), ensure_ascii=False)})
        return results
