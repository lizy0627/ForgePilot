"""Built-in Agent definitions and an immutable-by-value registry."""
from __future__ import annotations

from .definitions import AgentDefinition, AgentLimits


def _limits(model=8, tools=40, input_tokens=64_000, output_tokens=12_000, timeout=900):
    return AgentLimits(
        max_model_calls=model, max_tool_calls=tools, max_input_tokens=input_tokens,
        max_output_tokens=output_tokens, timeout_seconds=timeout,
    )


BUILTIN_AGENT_DEFINITIONS = (
    AgentDefinition(
        agent_id="orchestrator.v1", display_name="Orchestrator", role="orchestrator",
        role_description="Produces task specifications, merges evidence, and creates verifiable plans.",
        prompt_version="orchestrator-prompt.v1", model_route="orchestration",
        input_artifacts=(), output_artifacts=("TaskSpec", "ExecutionPlan", "DeliveryBundle"),
        capabilities=("repository.read", "repository.search", "repository.rag"), limits=_limits(),
    ),
    AgentDefinition(
        agent_id="root-cause-analyst.v1", display_name="Root Cause Analyst", role="root_cause_analyst",
        role_description="Finds root causes and records concise repository evidence.",
        prompt_version="root-cause-analyst-prompt.v1", model_route="analysis",
        input_artifacts=("TaskSpec",), output_artifacts=("EvidenceBundle",),
        capabilities=("repository.read", "repository.search", "repository.symbols", "repository.references", "repository.rag"),
        limits=_limits(model=6, tools=50),
    ),
    AgentDefinition(
        agent_id="impact-test-analyst.v1", display_name="Impact and Test Analyst", role="impact_test_analyst",
        role_description="Assesses impact, compatibility, and regression-test requirements.",
        prompt_version="impact-test-analyst-prompt.v1", model_route="analysis",
        input_artifacts=("TaskSpec",), output_artifacts=("EvidenceBundle",),
        capabilities=("repository.read", "repository.search", "repository.symbols", "repository.references", "repository.rag"),
        limits=_limits(model=6, tools=50),
    ),
    AgentDefinition(
        agent_id="implementer.v1", display_name="Implementer", role="implementer",
        role_description="Applies only approved plan changes and adds required regression tests.",
        prompt_version="implementer-prompt.v1", model_route="implementation",
        input_artifacts=("TaskSpec", "ExecutionPlan", "EvidenceBundle", "ValidationReport"),
        output_artifacts=("ChangeSet",),
        capabilities=("repository.read", "repository.search", "repository.patch", "validation.read"), limits=_limits(),
    ),
    AgentDefinition(
        agent_id="reviewer.v1", display_name="Reviewer", role="reviewer",
        role_description="Independently reviews diffs, test integrity, scope, and acceptance coverage.",
        prompt_version="reviewer-prompt.v1", model_route="review",
        input_artifacts=("TaskSpec", "ExecutionPlan", "ChangeSet", "ValidationReport"),
        output_artifacts=("ReviewReport",),
        capabilities=("repository.read", "repository.search", "repository.symbols", "repository.references", "validation.read"),
        limits=_limits(model=6, tools=40),
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
    def __init__(self, definitions=BUILTIN_AGENT_DEFINITIONS):
        self._definitions: dict[str, AgentDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: AgentDefinition) -> None:
        definition = AgentDefinition.model_validate(definition)
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
