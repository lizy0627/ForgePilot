"""Built-in Agent definitions and an immutable-by-value registry."""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from pydantic import ValidationError

from ..errors import ForgePilotError
from .definitions import AgentDefinition, AgentLimits


def _limits(model=8, tools=40, input_tokens=64_000, output_tokens=12_000, timeout=900):
    # Keep the historical token metadata in built-in definition hashes for existing missions.
    return AgentLimits(
        max_model_calls=model, max_tool_calls=tools, max_input_tokens=input_tokens,
        max_output_tokens=output_tokens, timeout_seconds=timeout,
    )


BUILTIN_AGENT_DEFINITIONS = (
    AgentDefinition(
        agent_id="orchestrator.v1", display_name="Orchestrator", role="orchestrator",
        role_description="Produces task specifications, merges evidence, and creates verifiable plans.",
        prompt_version="orchestrator-prompt.v1", model_route="orchestration",
        input_artifacts=("TaskSpec", "EvidenceBundle"), output_artifacts=("TaskSpec", "ExecutionPlan", "DeliveryBundle"),
        capabilities=("repository.read", "repository.search", "repository.rag"),
        limits=_limits(model=3, input_tokens=30_000),
    ),
    AgentDefinition(
        agent_id="root-cause-analyst.v1", display_name="Root Cause Analyst", role="root_cause_analyst",
        role_description="Finds root causes and records concise repository evidence.",
        prompt_version="root-cause-analyst-prompt.v1", model_route="analysis",
        input_artifacts=("TaskSpec",), output_artifacts=("EvidenceBundle",),
        capabilities=("repository.read", "repository.search", "repository.symbols", "repository.references", "repository.rag", "mcp.read"),
        limits=_limits(model=4, tools=50, input_tokens=40_000),
    ),
    AgentDefinition(
        agent_id="impact-test-analyst.v1", display_name="Impact and Test Analyst", role="impact_test_analyst",
        role_description="Assesses impact, compatibility, and regression-test requirements.",
        prompt_version="impact-test-analyst-prompt.v1", model_route="analysis",
        input_artifacts=("TaskSpec",), output_artifacts=("EvidenceBundle",),
        capabilities=("repository.read", "repository.search", "repository.symbols", "repository.references", "repository.rag", "mcp.read"),
        limits=_limits(model=4, tools=50, input_tokens=40_000),
    ),
    AgentDefinition(
        agent_id="implementer.v1", display_name="Implementer", role="implementer",
        role_description="Applies only approved plan changes and adds required regression tests.",
        prompt_version="implementer-prompt.v1", model_route="implementation",
        input_artifacts=("TaskSpec", "ExecutionPlan", "EvidenceBundle"),
        output_artifacts=("ChangeSet",),
        capabilities=("repository.read", "repository.search", "repository.rag", "repository.patch"), limits=_limits(),
    ),
    AgentDefinition(
        agent_id="reviewer.v1", display_name="Reviewer", role="reviewer",
        role_description="Independently reviews diffs, test integrity, scope, and acceptance coverage.",
        prompt_version="reviewer-prompt.v1", model_route="review",
        input_artifacts=("TaskSpec", "ChangeSet", "ValidationReport", "EvidenceBundle"),
        output_artifacts=("ReviewReport",),
        capabilities=("validation.read",),
        limits=_limits(model=6, tools=0),
    ),
    AgentDefinition(
        agent_id="repair.v1", display_name="Repair", role="repair",
        role_description="Repairs an approved change set from deterministic validation failures.",
        prompt_version="repair-prompt.v1", model_route="repair",
        input_artifacts=("TaskSpec", "ExecutionPlan", "ChangeSet", "ValidationReport"),
        output_artifacts=("ChangeSet",),
        capabilities=("repository.read", "repository.search", "repository.patch", "validation.read"),
        limits=_limits(model=6, tools=40),
    ),
)


class AgentRegistry:
    def __init__(self, definitions=BUILTIN_AGENT_DEFINITIONS, *, prompt_versions=None, router=None):
        # This is a manifest of known prompt versions, not executable prompts.
        # Custom declarations must supply a trusted version->role manifest.
        self._prompt_versions = dict(prompt_versions) if prompt_versions is not None else {
            definition.prompt_version: definition.role for definition in BUILTIN_AGENT_DEFINITIONS
        }
        self._router = router
        self._definitions: dict[str, AgentDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: AgentDefinition) -> None:
        definition = AgentDefinition.model_validate(definition)
        if self._prompt_versions.get(definition.prompt_version) != definition.role:
            raise ValueError("Unknown prompt version or prompt role mismatch")
        if self._router is not None:
            self._router.validate_agent(definition)
        if definition.agent_id in self._definitions:
            raise ValueError(f"Agent is already registered: {definition.agent_id}")
        self._definitions[definition.agent_id] = definition

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._definitions[agent_id]
        except KeyError:
            raise KeyError(f"Unknown Agent: {agent_id}") from None

    def all(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions.values())

    @classmethod
    def from_json(cls, raw: str | bytes, *, prompt_versions=None, router=None):
        """Load a versioned, data-only manifest; fail atomically on invalid data."""
        from ..models.profiles import MAX_CONFIGURATION_BYTES, _unique_json_pairs
        try:
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_CONFIGURATION_BYTES:
                raise ValueError("Agent registry exceeds size limit")
            payload = json.loads(raw, object_pairs_hook=_unique_json_pairs)
            if (not isinstance(payload, dict) or set(payload) != {"schema_version", "agents"}
                    or payload["schema_version"] != "1.0" or not isinstance(payload["agents"], list)
                    or not 1 <= len(payload["agents"]) <= 128):
                raise ValueError("Invalid Agent registry manifest")
            return cls(payload["agents"], prompt_versions=prompt_versions, router=router)
        except (ValueError, TypeError, RecursionError, UnicodeError, ValidationError):
            raise ForgePilotError("agent_config", "Invalid Agent registry configuration") from None

    @classmethod
    def load(cls, path: str | Path, *, prompt_versions=None, router=None):
        from ..models.profiles import MAX_CONFIGURATION_BYTES
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
            return cls.from_json(raw, prompt_versions=prompt_versions, router=router)
        except OSError:
            raise ForgePilotError("agent_config", "Cannot read Agent registry configuration") from None

    def validate_artifacts(self, agent_id: str, artifacts, *, output: bool = False) -> None:
        """Validate the types crossing a declared Agent boundary.

        Inputs such as ValidationReport are optional until a repair iteration, and
        an Orchestrator stage may produce only one of its declared output types.
        Individual Artifact envelopes are validated by the Artifact store.
        """
        definition = self.get(agent_id)
        allowed = definition.output_artifacts if output else definition.input_artifacts
        for artifact in artifacts:
            name = artifact if isinstance(artifact, str) else getattr(artifact, "artifact_type", None)
            if name not in allowed:
                raise ForgePilotError("agent_artifact", "Artifact type is outside this Agent contract")

    def validate_limits(self, agent_id: str, limits: AgentLimits | Mapping) -> AgentLimits:
        """Allow a caller to reduce, never increase, a registered Agent's budget."""
        requested = AgentLimits.model_validate(limits)
        allowed = self.get(agent_id).limits
        if any(getattr(requested, name) > getattr(allowed, name)
               for name in ("max_model_calls", "max_tool_calls", "timeout_seconds")):
            raise ForgePilotError("agent_budget", "Requested budget exceeds the Agent definition")
        return requested
