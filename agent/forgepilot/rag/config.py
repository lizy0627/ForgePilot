"""Disabled-by-default RAG settings and the DPAPI credential boundary."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..errors import ForgePilotError
from ..repository import EXCLUDED, MAX_FILE_BYTES
from ..windows_secrets import protect_secrets, unprotect_secrets


DEFAULT_INCLUDE = (
    "*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx",
    "*.py", "*.pyi", "*.js", "*.jsx", "*.ts", "*.tsx", "*.java", "*.cs",
    "*.go", "*.rs", "*.swift", "*.kt", "*.kts", "*.rb", "*.php", "*.lua",
    "*.sql", "*.sh", "*.ps1", "*.qml", "*.cmake", "CMakeLists.txt",
)
EMBEDDING_SECRET_NAME = "rag_embedding_api_key"


def _tuple_from_json(value):
    return tuple(value) if isinstance(value, list) else value


def _checked_api_key(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4_096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ForgePilotError("rag_secret_unavailable", "Invalid embedding credential")
    return value


class RagConfig(BaseModel):
    """Persistable values only. API keys are deliberately absent."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True,
        revalidate_instances="always",
    )

    enabled: bool = False
    include: tuple[str, ...] = Field(default=DEFAULT_INCLUDE, min_length=1, max_length=128)
    exclude: tuple[str, ...] = Field(default=EXCLUDED, max_length=128)
    max_file_bytes: int = Field(default=MAX_FILE_BYTES, ge=1, le=16_777_216)
    embedding_model: str | None = Field(default=None, max_length=256)
    embedding_base_url: str | None = Field(default=None, max_length=2_048)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    retrieval_top_k: int = Field(default=8, ge=1, le=100)
    context_max_chars: int = Field(default=24_000, ge=1, le=1_000_000)

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def json_arrays(cls, value):
        return _tuple_from_json(value)

    @field_validator("include", "exclude")
    @classmethod
    def bounded_patterns(cls, values):
        if any(not value or len(value) > 256 or "\0" in value or value != value.strip()
               for value in values):
            raise ValueError("RAG patterns must be bounded nonblank text")
        return values

    @field_validator("embedding_model")
    @classmethod
    def model_name(cls, value):
        if value is not None and (not value.strip() or value != value.strip()
                                  or any(ord(character) < 32 for character in value)):
            raise ValueError("Invalid embedding model")
        return value

    @field_validator("embedding_base_url")
    @classmethod
    def api_root(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Use an http(s) embedding API root without credentials/query/fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def enabled_connection(self):
        if self.enabled and (not self.embedding_model or not self.embedding_base_url):
            raise ValueError("Enabled RAG requires an embedding model and base URL")
        return self


class RagRuntimeSettings(BaseModel):
    """Ephemeral runtime-only values; never serialize this as configuration."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    config: RagConfig
    embedding_api_key: str = Field(repr=False, exclude=True)

    @field_validator("embedding_api_key")
    @classmethod
    def checked_secret(cls, value):
        return _checked_api_key(value)


@dataclass(frozen=True, slots=True)
class RagRuntimeAvailability:
    """Non-throwing startup view for this disabled-by-default subsystem."""

    status: Literal["available", "unavailable"]
    settings: RagRuntimeSettings | None = None
    reason: Literal["disabled", "not_configured", "invalid_configuration"] | None = None


def protect_embedding_api_key(value: str) -> bytes:
    try:
        return protect_secrets({EMBEDDING_SECRET_NAME: _checked_api_key(value)})
    except ForgePilotError:
        raise ForgePilotError("rag_secret_unavailable", "Cannot protect embedding credential") from None


def unprotect_embedding_api_key(ciphertext: bytes) -> str:
    try:
        values = unprotect_secrets(ciphertext)
        value = values.get(EMBEDDING_SECRET_NAME)
        if not isinstance(value, str) or not value:
            raise ValueError()
        return value
    except (ForgePilotError, ValueError):
        raise ForgePilotError("rag_secret_unavailable", "Cannot read saved embedding credential") from None


def rag_config_from_args(args) -> RagConfig:
    try:
        return RagConfig(
            enabled=args.rag_enabled,
            include=tuple(args.rag_include) if args.rag_include else DEFAULT_INCLUDE,
            exclude=tuple(args.rag_exclude) if args.rag_exclude else EXCLUDED,
            max_file_bytes=args.rag_max_file_bytes,
            embedding_model=args.embedding_model,
            embedding_base_url=args.embedding_base_url,
            embedding_batch_size=args.embedding_batch_size,
            retrieval_top_k=args.retrieval_top_k,
            context_max_chars=args.context_max_chars,
        )
    except ValidationError:
        raise ForgePilotError("rag_config", "Invalid RAG configuration or limits") from None


def rag_runtime_from_args(args, *, api_key: str | None = None) -> RagRuntimeSettings | None:
    # Disabled means inert: stale/malformed optional embedding environment
    # values cannot prevent the established Agent workflow from starting.
    if not args.rag_enabled:
        return None
    config = rag_config_from_args(args)
    # Desktop startup obtains FORGEPILOT_API_KEY from its existing current-user
    # DPAPI file. A distinct key may be injected at runtime, but is never a
    # config.json/CLI field.
    secret = os.environ.get("FORGEPILOT_EMBEDDING_API_KEY") or api_key or os.environ.get("FORGEPILOT_API_KEY")
    if not secret:
        raise ForgePilotError("rag_secret_unavailable", "Embedding API key is not configured")
    return RagRuntimeSettings(config=config, embedding_api_key=secret)


def rag_runtime_availability_from_args(args, *, api_key: str | None = None) -> RagRuntimeAvailability:
    """Return an inert unavailable state instead of breaking the established Agent startup."""
    if not args.rag_enabled:
        return RagRuntimeAvailability("unavailable", reason="disabled")
    if not args.embedding_model or not args.embedding_base_url:
        return RagRuntimeAvailability("unavailable", reason="not_configured")
    try:
        settings = rag_runtime_from_args(args, api_key=api_key)
    except ForgePilotError as exc:
        reason = "not_configured" if exc.code == "rag_secret_unavailable" else "invalid_configuration"
        return RagRuntimeAvailability("unavailable", reason=reason)
    return RagRuntimeAvailability("available", settings=settings)


def add_rag_arguments(parser):
    parser.add_argument("--enable-rag", dest="rag_enabled", action="store_true",
                        help="Enable the optional code RAG foundation (disabled by default)")
    parser.add_argument("--rag-include", action="append", help="Include glob; repeat for multiple patterns")
    parser.add_argument("--rag-exclude", action="append", help="Exclude glob; repeat for multiple patterns")
    parser.add_argument("--rag-max-file-bytes", type=int, default=MAX_FILE_BYTES)
    parser.add_argument("--embedding-model", default=os.environ.get("FORGEPILOT_EMBEDDING_MODEL"))
    parser.add_argument("--embedding-base-url", default=os.environ.get("FORGEPILOT_EMBEDDING_BASE_URL"))
    parser.add_argument("--embedding-batch-size", type=int, default=16)
    parser.add_argument("--retrieval-top-k", type=int, default=8)
    parser.add_argument("--context-max-chars", type=int, default=24_000)
