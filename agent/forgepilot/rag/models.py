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
                  symbol: str | None, content: str, file_hash: str) -> str:
    """Hash an unambiguous canonical tuple; source content itself is never logged."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload = json.dumps(
        [repository_id, path, start_line, end_line, symbol, file_hash, content_hash],
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
            file_hash=self.file_hash,
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


class RetrievalScoreTrace(_RagModel):
    """One deterministic contribution to a result's final score."""

    source: str = Field(min_length=1, max_length=64)
    rank: int | None = Field(default=None, ge=1, le=10_000)
    weight: float = Field(ge=0.0, le=1.0)
    rrf: float | None = Field(default=None, ge=0.0)
    contribution: float = Field(ge=0.0)

    @field_validator("source")
    @classmethod
    def source_name(cls, value):
        return _checked_text(value, name="source", maximum=64)

    @field_validator("weight", "rrf", "contribution")
    @classmethod
    def finite_value(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("score trace values must be finite")
        return value


class RetrievalSourceTrace(_RagModel):
    source: Literal["literal", "vector", "clangd"]
    status: Literal["available", "unavailable", "failed", "not_applicable"]
    candidate_count: int = Field(default=0, ge=0, le=10_000)
    reason: str | None = Field(default=None, max_length=128)

    @field_validator("reason")
    @classmethod
    def bounded_reason(cls, value):
        if value is not None:
            return _checked_text(value, name="reason", maximum=128)
        return value


class RetrievalTrace(_RagModel):
    """Content-free query diagnostics safe for status events and logs."""

    mode: Literal["lexical", "vector", "hybrid"]
    status: Literal["ready", "degraded"]
    sources: tuple[RetrievalSourceTrace, ...] = Field(min_length=3, max_length=3)
    events: tuple[str, ...] = Field(default=(), max_length=32)
    stale_chunk_filtered: int = Field(default=0, ge=0, le=10_000)
    incremental_update_triggered: int = Field(default=0, ge=0, le=10_000)
    result_count: int = Field(default=0, ge=0, le=100)
    context_chars: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("events")
    @classmethod
    def bounded_events(cls, values):
        if any(not value or len(value) > 128 or value != value.strip() or "\0" in value
               for value in values):
            raise ValueError("trace events must be bounded nonblank text")
        if len(set(values)) != len(values):
            raise ValueError("trace events must be unique")
        return values


class RetrievalResult(_RagModel):
    """Ranked repository evidence; ``content`` is always untrusted source data."""

    repository_id: str | None = None
    path: str
    line: int = Field(ge=1, le=2**31 - 1)
    end_line: int = Field(ge=1, le=2**31 - 1)
    symbol: str | None = Field(default=None, max_length=512)
    score: float
    reasons: tuple[str, ...] = Field(min_length=1, max_length=32)
    file_hash: str | None = None
    sources: tuple[str, ...] = Field(default=(), max_length=8)
    score_trace: tuple[RetrievalScoreTrace, ...] = Field(default=(), max_length=16)
    content: str = Field(min_length=1, max_length=1_048_576, repr=False)

    @field_validator("repository_id")
    @classmethod
    def optional_repository_identity(cls, value):
        return checked_repository_id(value) if value is not None else value

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        return checked_relative_path(value)

    @field_validator("symbol")
    @classmethod
    def symbol_text(cls, value):
        if value is not None:
            return _checked_text(value, name="symbol", maximum=512)
        return value

    @field_validator("file_hash")
    @classmethod
    def optional_source_hash(cls, value):
        return checked_hash(value) if value is not None else value

    @field_validator("sources")
    @classmethod
    def bounded_sources(cls, values):
        if any(not value or len(value) > 64 or value != value.strip() or "\0" in value
               for value in values):
            raise ValueError("sources must be bounded nonblank text")
        if len(set(values)) != len(values):
            raise ValueError("sources must be unique")
        return values

    @field_validator("score")
    @classmethod
    def finite_score(cls, value):
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value

    @field_validator("reasons")
    @classmethod
    def bounded_reasons(cls, values):
        if any(not value or len(value) > 128 or value != value.strip() or "\0" in value
               for value in values):
            raise ValueError("reasons must be bounded nonblank text")
        if len(set(values)) != len(values):
            raise ValueError("reasons must be unique")
        return values

    @model_validator(mode="after")
    def lines_are_ordered(self):
        if self.end_line < self.line:
            raise ValueError("end_line must be greater than or equal to line")
        return self


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


IndexRunStatus = Literal[
    "disabled", "idle", "pending", "indexing", "ready", "empty", "degraded", "failed",
    "cancelled",
]

PersistedRunStatus = Literal[
    "indexing", "cancelling", "ready", "empty", "degraded", "failed", "cancelled",
]
IndexOperation = Literal["index", "clear"]


class IndexProgress(_RagModel):
    """A content-free progress snapshot safe to expose to callers and logs."""

    repository_id: str
    status: IndexRunStatus = "idle"
    total_files: int = Field(default=0, ge=0, le=2**31 - 1)
    processed: int = Field(default=0, ge=0, le=2**31 - 1)
    skipped: int = Field(default=0, ge=0, le=2**31 - 1)
    failed: int = Field(default=0, ge=0, le=2**31 - 1)
    chunks: int = Field(default=0, ge=0, le=2**31 - 1)

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @model_validator(mode="after")
    def counts_are_consistent(self):
        if self.processed > self.total_files:
            raise ValueError("processed cannot exceed total_files")
        if self.skipped + self.failed > self.processed:
            raise ValueError("skipped and failed must describe processed files")
        return self


class IndexRunRecord(_RagModel):
    """Latest durable snapshot for one repository's index operation."""

    repository_id: str
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: IndexOperation
    status: PersistedRunStatus
    revision: int = Field(ge=1, le=2**63 - 1)
    total_files: int = Field(default=0, ge=0, le=2**31 - 1)
    processed: int = Field(default=0, ge=0, le=2**31 - 1)
    skipped: int = Field(default=0, ge=0, le=2**31 - 1)
    failed: int = Field(default=0, ge=0, le=2**31 - 1)
    chunks: int = Field(default=0, ge=0, le=2**31 - 1)
    error_code: str | None = Field(default=None, max_length=128)
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @field_validator("repository_id")
    @classmethod
    def repository_identity(cls, value):
        return checked_repository_id(value)

    @field_validator("error_code")
    @classmethod
    def bounded_error_code(cls, value):
        if value is not None:
            return _checked_text(value, name="error_code", maximum=128)
        return value

    @field_validator("started_at", "updated_at", "finished_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def run_is_consistent(self):
        if self.processed > self.total_files:
            raise ValueError("processed cannot exceed total_files")
        if self.skipped + self.failed > self.processed:
            raise ValueError("skipped and failed must describe processed files")
        terminal = self.status in {"ready", "empty", "degraded", "failed", "cancelled"}
        if terminal != (self.finished_at is not None):
            raise ValueError("only terminal runs have finished_at")
        if self.updated_at < self.started_at:
            raise ValueError("updated_at cannot precede started_at")
        return self
