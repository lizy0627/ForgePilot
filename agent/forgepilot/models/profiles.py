"""Trusted, serializable model settings containing credential references only."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ForgePilotError
from ..windows_secrets import unprotect_secrets

_ID = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_REF = r"^(?:env:[A-Z][A-Z0-9_]{0,127}|dpapi:[a-z][a-z0-9._-]{0,127})$"
Identifier = Annotated[str, Field(pattern=_ID, max_length=128)]
MAX_CONFIGURATION_BYTES = 1_048_576


class ModelProfile(BaseModel):
    """A provider configuration, never an Agent permission grant."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              revalidate_instances="always", hide_input_in_errors=True)
    profile_id: Identifier
    provider: Literal["openai-compatible"] = "openai-compatible"
    base_url: str = Field(min_length=1, max_length=2_048, repr=False)
    api_key_ref: str = Field(pattern=_REF, max_length=134, repr=False)
    model: str = Field(min_length=1, max_length=256)
    family: Identifier
    supports_streaming: bool = True
    supports_tool_calling: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0, le=600, allow_inf_nan=False)
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float = Field(default=0.1, ge=0, le=2, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value):
        try:
            url = httpx.URL(value)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("Invalid model API root") from None
        if (url.scheme not in ("http", "https") or not url.host or url.userinfo
                or url.query or url.fragment or any(ord(c) < 32 for c in value)):
            raise ValueError("Model API root must be HTTP(S) without credentials, query or fragment")
        return str(url).rstrip("/")

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid model identifier")
        return value.strip()


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              revalidate_instances="always", hide_input_in_errors=True)
    route_id: Identifier
    primary: Identifier
    fallback: Identifier | None = None
    role: Literal["orchestrator", "root_cause_analyst", "impact_test_analyst",
                  "implementer", "reviewer", "repair"] | None = None

    @model_validator(mode="after")
    def distinct_profiles(self):
        if self.primary == self.fallback:
            raise ValueError("Fallback must name a different model profile")
        return self


def _unique_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              revalidate_instances="always", hide_input_in_errors=True)
    schema_version: Literal["1.0"] = "1.0"
    profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=64)
    routes: tuple[ModelRoute, ...] = Field(min_length=1, max_length=128)
    enforce_review_family_separation: bool = True

    @field_validator("profiles", "routes", mode="before")
    @classmethod
    def arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def valid_routes(self):
        profiles = {profile.profile_id: profile for profile in self.profiles}
        if len(profiles) != len(self.profiles):
            raise ValueError("Model profile identifiers must be unique")
        if len({(route.route_id, route.role) for route in self.routes}) != len(self.routes):
            raise ValueError("Model routes must be unique by use case and role")
        for route in self.routes:
            if route.primary not in profiles or route.fallback and route.fallback not in profiles:
                raise ValueError("Model route references an unknown profile")
        if self.enforce_review_family_separation:
            review = set()
            implementation = set()
            for route in self.routes:
                families = {profiles[name].family for name in (route.primary, route.fallback) if name}
                if route.role == "reviewer" or route.route_id == "review" and route.role is None:
                    review.update(families)
                if route.role == "implementer" or route.route_id == "implementation" and route.role is None:
                    implementation.update(families)
            if review & implementation:
                raise ValueError("Reviewer and Implementer primary/fallback model families must differ")
        return self

    def profile(self, profile_id: str) -> ModelProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise ForgePilotError("llm_config", "Unknown model profile")

    def router(self):
        from .routing import ModelRouter
        return ModelRouter(self)

    @classmethod
    def from_json(cls, raw: str | bytes, *, preserve_output_limits: bool = False):
        try:
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_CONFIGURATION_BYTES:
                raise ValueError("Configuration too large")
            config = cls.model_validate(json.loads(raw, object_pairs_hook=_unique_json_pairs))
            profiles = []
            for profile in config.profiles:
                if preserve_output_limits:
                    # Older model files could omit the field and use the v1 default.
                    limit = profile.max_output_tokens if "max_output_tokens" in profile.model_fields_set else 12_000
                else:
                    limit = None
                profiles.append(profile.model_copy(update={"max_output_tokens": limit}))
            return config.model_copy(update={"profiles": tuple(profiles)})
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise ForgePilotError("llm_config", "Invalid model configuration") from None

    @classmethod
    def load(cls, path: str | Path, *, preserve_output_limits: bool = False):
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
            return cls.from_json(raw, preserve_output_limits=preserve_output_limits)
        except OSError:
            raise ForgePilotError("llm_config", "Cannot read model configuration") from None


class SecretResolver:
    """Resolve existing runtime environment keys or a trusted current-user DPAPI store.

    The DPAPI lookup receives only the configured name and returns an encrypted
    blob created by ``windows_secrets.protect_secrets({'api_key': ...})``. It never
    accepts paths from model output. Desktop launches can continue to unwrap their
    existing PowerShell DPAPI file into ``env:FORGEPILOT_API_KEY``.
    """

    def __init__(self, *, environment: Mapping[str, str] | None = None,
                 dpapi_lookup: Callable[[str], bytes] | None = None):
        self._environment = os.environ if environment is None else environment
        self._dpapi_lookup = dpapi_lookup

    def __call__(self, reference: str) -> str:
        import re
        try:
            if not isinstance(reference, str) or re.fullmatch(_REF, reference) is None:
                raise ValueError("Invalid credential reference")
            kind, name = reference.split(":", 1)
            if kind == "env":
                secret = self._environment.get(name)
            elif self._dpapi_lookup is not None:
                secret = unprotect_secrets(self._dpapi_lookup(name)).get("api_key")
            else:
                secret = None
            if (not isinstance(secret, str) or not secret.strip() or len(secret) > 4_096
                    or any(ord(c) < 32 or ord(c) == 127 for c in secret)):
                raise ValueError("Missing model credential")
            return secret
        except Exception:
            raise ForgePilotError("llm_config", "Cannot resolve model credential reference") from None
