"""The implementer exposes only the versioned, bounded implementation contract."""
import pytest

from forgepilot.agents import AgentRegistry
from forgepilot.agents.prompts import get_prompt
from forgepilot.errors import ForgePilotError


def test_implementer_advertises_only_required_artifacts_and_capabilities():
    registry = AgentRegistry()
    definition = registry.get("implementer.v1")
    assert definition.input_artifacts == ("TaskSpec", "ExecutionPlan", "EvidenceBundle")
    assert definition.output_artifacts == ("ChangeSet",)
    assert set(definition.capabilities) == {
        "repository.read", "repository.search", "repository.rag", "repository.patch",
    }
    assert definition.model_route == "implementation"
    with pytest.raises(ForgePilotError):
        registry.validate_artifacts("implementer.v1", ["ValidationReport"])
    with pytest.raises(ForgePilotError):
        registry.validate_artifacts("implementer.v1", ["ReviewReport"], output=True)


def test_implementer_has_its_own_write_capable_prompt_and_cannot_select_analysis_stage():
    prompt = get_prompt("implementer.v1", "implementer")
    assert "implementer-prompt.v1" in prompt
    assert "plan_step_id" in prompt and "acceptance_criteria_ids" in prompt
    assert "awaiting_human" in prompt
    assert "WorkspaceManager" in prompt
    assert "untrusted data" in prompt
    assert "Never write files, apply patches" not in prompt
    with pytest.raises(ValueError):
        get_prompt("implementer.v1", "generate_plan")
    with pytest.raises(ValueError):
        get_prompt("orchestrator.v1", "implementer")


def test_implementer_budget_can_only_be_reduced():
    registry = AgentRegistry()
    limits = registry.get("implementer.v1").limits
    for field in ("max_model_calls", "max_tool_calls", "timeout_seconds"):
        payload = limits.model_dump()
        payload[field] += 1
        with pytest.raises(ForgePilotError, match="budget"):
            registry.validate_limits("implementer.v1", payload)


def test_definition_accepts_missing_or_historical_token_fields():
    registry = AgentRegistry()
    for token_fields in ({}, {"max_input_tokens": 500_000, "max_output_tokens": 100_000}):
        payload = {"max_model_calls": 2, "max_tool_calls": 0, "timeout_seconds": 30, **token_fields}
        result = registry.validate_limits("implementer.v1", payload)
        assert result.max_input_tokens == token_fields.get("max_input_tokens")
        assert result.max_output_tokens == token_fields.get("max_output_tokens")
