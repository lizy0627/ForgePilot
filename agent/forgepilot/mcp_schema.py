"""Bounded Draft 2020-12 validation with local references only.

Regex assertions/dynamic scope are explicitly unsupported in this phase: Python's
regex engine has no time budget. Other standard assertions use jsonschema. Unknown
keywords fail closed, rather than silently discarding an unfamiliar constraint.
"""
from contextvars import ContextVar
from copy import deepcopy
import json
from time import monotonic
from urllib.parse import unquote

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema._utils import equal
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from .errors import ForgePilotError

_budget = ContextVar("mcp_schema_budget", default=None)
ANNOTATIONS = {"$schema", "$defs", "$ref", "$comment", "title", "description", "default", "examples",
               "deprecated", "readOnly", "writeOnly", "contentEncoding", "contentMediaType",
               "then", "else", "minContains", "maxContains"}
UNSUPPORTED = {"pattern", "patternProperties", "$id", "$anchor", "$dynamicRef", "$dynamicAnchor",
               "format", "contentSchema"}
MAPS = {"properties", "$defs", "dependentSchemas"}
SINGLE = {"additionalProperties", "unevaluatedProperties", "items", "contains", "unevaluatedItems",
          "propertyNames", "not", "if", "then", "else"}
ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _tick():
    budget = _budget.get()
    if budget is not None:
        budget[0] -= 1
        if budget[0] < 0 or monotonic() > budget[1]:
            raise ValueError("Schema validation budget exceeded")


def _bounded(handler):
    def run(validator, constraint, instance, schema):
        _tick()
        yield from handler(validator, constraint, instance, schema)
    return run


def _unique_items(validator, unique, instance, schema):
    if unique and validator.is_type(instance, "array"):
        for index, item in enumerate(instance):
            for other in instance[:index]:
                _tick()
                if equal(item, other):
                    yield ValidationError("Array elements must be unique")
                    return


Validator = validators.extend(Draft202012Validator, {
    name: _bounded(_unique_items if name == "uniqueItems" else handler)
    for name, handler in Draft202012Validator.VALIDATORS.items()
})


class _Unsupported(ValueError):
    """Only our own fixed messages may become a discovery reason."""


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


LOCAL_ONLY = Registry(retrieve=_no_remote)


def _target(root, reference):
    if not isinstance(reference, str) or not reference.startswith("#"):
        raise _Unsupported("External references are forbidden")
    pointer = unquote(reference[1:])
    if pointer and not pointer.startswith("/"):
        raise _Unsupported("Only local JSON pointers are supported")
    node = root
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    if not isinstance(node, (dict, bool)):
        raise _Unsupported("Reference must identify a schema")
    return node


def checked_schema(schema):
    def check(node, depth=0, ancestors=()):
        _tick()
        if depth > 8 or id(node) in ancestors:
            raise _Unsupported("Schema depth or recursive reference exceeds supported limits")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise _Unsupported("Schema must be an object or boolean")
        unknown = set(node) - set(Draft202012Validator.VALIDATORS) - ANNOTATIONS
        if unknown or set(node) & UNSUPPORTED:
            # Do not include server-controlled keyword text in errors.
            raise _Unsupported("Unsupported Schema keyword (including regex, format or dynamic scope)")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise _Unsupported("Only Draft 2020-12 is supported")
        if depth and "$schema" in node:
            raise _Unsupported("Nested schema dialect declarations are unsupported")
        children = []
        for key in MAPS & node.keys():
            children.extend(node[key].values())
        children.extend(node[key] for key in SINGLE & node.keys())
        for key in ARRAYS & node.keys():
            children.extend(node[key])
        if "$ref" in node:
            children.append(_target(schema, node["$ref"]))
        for child in children:
            check(child, depth + 1, (*ancestors, id(node)))

    token = _budget.set([10_000, monotonic() + 0.1])
    try:
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16_384):
            raise _Unsupported("Schema must be an object schema within 16 KiB")
        check(schema)
        Draft202012Validator.check_schema(schema)
        _tick()
        return deepcopy(schema)
    except (SchemaError, KeyError, IndexError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ForgePilotError("mcp_schema", "Invalid MCP JSON Schema") from None
    except ValueError as exc:
        reason = str(exc) if isinstance(exc, _Unsupported) else "Invalid or over-budget MCP JSON Schema"
        raise ForgePilotError("mcp_schema", reason) from None
    finally:
        _budget.reset(token)


def validate(schema, value):
    token = _budget.set([10_000, monotonic() + 0.1])
    try:
        def check_json(node, depth=0):
            _tick()
            if depth > 32:
                raise ValueError("JSON value depth limit exceeded")
            if type(node) is dict:
                if any(type(key) is not str for key in node):
                    raise ValueError("JSON keys must be strings")
                for child in node.values():
                    check_json(child, depth + 1)
            elif type(node) is list:
                for child in node:
                    check_json(child, depth + 1)
            elif type(node) not in (str, int, float, bool, type(None)):
                raise ValueError("Expected a JSON value")
        check_json(value)
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 262_144:
            raise ValueError("JSON value size limit exceeded")
        Validator(schema, registry=LOCAL_ONLY).validate(value)
    except (ValidationError, Unresolvable, RecursionError, OverflowError, TypeError):
        raise ValueError("Value does not match the bounded JSON Schema") from None
    finally:
        _budget.reset(token)
