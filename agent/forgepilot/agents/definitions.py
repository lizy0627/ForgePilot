"""Declarative, versioned Agent definitions. Executable plugins are intentionally absent."""
from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifacts.canonical import content_sha256
from ..artifacts.models import ArtifactTypeName, IDENTIFIER_PATTERN

AGENT_SCHEMA_VERSION = "1.0"
AgentRole: TypeAlias = Literal[
    "orchestrator", "root_cause_analyst", "impact_test_analyst",
    "implementer", "reviewer", "repair",
]
ModelRoute: TypeAlias = Literal["orchestration", "analysis", "implementation", "review", "repair"]
Capability: TypeAlias = Literal[
    "repository.read", "repository.search", "repository.symbols", "repository.references",
    "repository.rag", "repository.patch", "validation.read", "mcp.read",
]

ROLE_ROUTES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"orchestration"}),
    "root_cause_analyst": frozenset({"analysis"}),
    "impact_test_analyst": frozenset({"analysis"}),
    "implementer": frozenset({"implementation"}),
    "reviewer": frozenset({"review"}),
    "repair": frozenset({"repair", "implementation"}),
}
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"repository.read", "repository.search", "repository.rag"}),
    "root_cause_analyst": frozenset({
        "repository.read", "repository.search", "repository.symbols", "repository.references",
        "repository.rag", "mcp.read",
    }),
    "impact_test_analyst": frozenset({
        "repository.read", "repository.search", "repository.symbols", "repository.references",
        "repository.rag", "mcp.read",
    }),
    "implementer": frozenset({"repository.read", "repository.search", "repository.patch", "validation.read"}),
    "reviewer": frozenset({
        "repository.read", "repository.search", "repository.symbols", "repository.references",
        "validation.read",
    }),
    "repair": frozenset({"repository.read", "repository.search", "repository.patch", "validation.read"}),
}


def _tuple(value):
    return tuple(value) if isinstance(value, list) else value


class AgentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")

    max_model_calls: int = Field(ge=1, le=64)
    max_tool_calls: int = Field(ge=0, le=256)
    max_input_tokens: int = Field(ge=1, le=262_144)
    max_output_tokens: int = Field(ge=1, le=65_536)
    timeout_seconds: int = Field(ge=1, le=3_600)


class AgentDefinition(BaseModel):
    """Data-only Agent contract; it cannot name Python modules, callables, or entry points."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, str_strip_whitespace=True,
        revalidate_instances="always", hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = AGENT_SCHEMA_VERSION
    agent_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    role: AgentRole
    role_description: Annotated[str, Field(min_length=1, max_length=2_048)]
    prompt_version: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    model_route: ModelRoute
    input_artifacts: tuple[ArtifactTypeName, ...] = Field(default=(), max_length=16)
    output_artifacts: tuple[ArtifactTypeName, ...] = Field(min_length=1, max_length=16)
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=32)
    limits: AgentLimits

    _arrays = field_validator("input_artifacts", "output_artifacts", "capabilities", mode="before")(_tuple)

    @property
    def definition_hash(self) -> str:
        """Canonical identity used by GraphRunner idempotency keys."""
        return content_sha256(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def enforce_policy(self):
        for label, values in (
            ("input_artifacts", self.input_artifacts),
            ("output_artifacts", self.output_artifacts),
            ("capabilities", self.capabilities),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.model_route not in ROLE_ROUTES[self.role]:
            raise ValueError(f"model_route {self.model_route!r} is not allowed for role {self.role!r}")
        excessive = set(self.capabilities) - ROLE_CAPABILITIES[self.role]
        if excessive:
            raise ValueError(f"Capabilities are not allowed for role {self.role!r}: {sorted(excessive)}")
        return self
