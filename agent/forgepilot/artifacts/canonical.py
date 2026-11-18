"""Deterministic JSON serialization used by versioned artifacts."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON data identically across dict insertion order and locales."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()
