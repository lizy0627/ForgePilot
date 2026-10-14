"""Strict, transport-neutral records for the optional code RAG foundation."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import PureWindowsPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_HASH = re.compile(r"^[0-9a-f]{64}$")


def _checked_text(value: str, *, name: str, maximum: int) -> str:
    if not value or len(value) > maximum or "\0" in value or value != value.strip():
        raise ValueError(f"{name} must be bounded, nonblank text without surrounding whitespace")
    return value


def checked_repository_id(value: str) -> str:
    return _checked_text(value, name="repository_id", maximum=256)


def checked_relative_path(value: str) -> str:
    value = _checked_text(value, name="path", maximum=1_024)
    if (value.startswith(("/", "\\")) or ":" in value or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/"))):
        raise ValueError("path must be a normalized relative repository path")
    return value.replace("\\", "/")


def checked_hash(value: str) -> str:
    if not _HASH.fullmatch(value):
        raise ValueError("hash must be a lowercase SHA-256 hexadecimal digest")
    return value


def make_chunk_id(*, repository_id: str, path: str, start_line: int, end_line: int,
                  symbol: str | None, content: str) -> str:
    """Hash an unambiguous canonical tuple; source content itself is never logged."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload = json.dumps(
        [repository_id, path, start_line, end_line, symbol, content_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _RagModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True,
        revalidate_instances="always",
    )


class CodeChunk(_RagModel):
    repository_id: str
    path: str
    language: str = Field(min_length=1, max_length=64)
    symbol: str | None = Field(default=None, max_length=512)
    symbol_type: str | None = Field(default=None, max_length=64)
    start_line: int = Field(ge=1, le=2**31 - 1)
    end_line: int = Field(ge=1, le=2**31 - 1)
    content: str = Field(min_length=1, max_length=1_048_576, repr=False)
    file_hash: str
    chunk_id: str = ""

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        return checked_relative_path(value)

    @field_validator("language", "symbol", "symbol_type")
    @classmethod
    def metadata_text(cls, value, info):
        if value is not None:
            return _checked_text(value, name=info.field_name, maximum=512 if info.field_name == "symbol" else 64)
        return value

    @field_validator("file_hash")
    @classmethod
    def source_hash(cls, value):
        return checked_hash(value)

    @model_validator(mode="after")
    def lines_and_identity(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        expected = make_chunk_id(
            repository_id=self.repository_id,
            path=self.path,
            start_line=self.start_line,
            end_line=self.end_line,
            symbol=self.symbol,
            content=self.content,
        )
        if self.chunk_id and self.chunk_id != expected:
            raise ValueError("chunk_id does not match the chunk identity")
        object.__setattr__(self, "chunk_id", expected)
        return self


IndexFileStatus = Literal["pending", "indexed", "failed"]


class IndexFileState(_RagModel):
    repository_id: str
    path: str
    content_hash: str
    indexed_at: datetime
    chunk_count: int = Field(ge=0, le=2**31 - 1)
    status: IndexFileStatus

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        return checked_relative_path(value)

    @field_validator("content_hash")
    @classmethod
    def source_hash(cls, value):
        return checked_hash(value)

    @field_validator("indexed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("indexed_at must include a timezone")
        return value


class RetrievalQuery(_RagModel):
    repository_id: str
    query: str = Field(min_length=1, max_length=16_384, repr=False)
    top_k: int = Field(default=8, ge=1, le=100)
    context_max_chars: int = Field(default=24_000, ge=1, le=1_000_000)

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @field_validator("query")
    @classmethod
    def query_text(cls, value):
        return _checked_text(value, name="query", maximum=16_384)


class RetrievedChunk(_RagModel):
    chunk: CodeChunk
    score: float
    rank: int | None = Field(default=None, ge=1, le=100)

    @field_validator("score")
    @classmethod
    def finite_score(cls, value):
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value


class RagIndexStatus(_RagModel):
    repository_id: str
    status: Literal["disabled", "empty", "indexing", "ready", "degraded", "failed"] = "disabled"
    file_count: int = Field(default=0, ge=0, le=2**31 - 1)
    chunk_count: int = Field(default=0, ge=0, le=2**31 - 1)
    failed_file_count: int = Field(default=0, ge=0, le=2**31 - 1)
    updated_at: datetime | None = None

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @field_validator("updated_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("updated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def counts_are_consistent(self):
        if self.failed_file_count > self.file_count:
            raise ValueError("failed_file_count cannot exceed file_count")
        return self
