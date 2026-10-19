import pytest
from pydantic import ValidationError

from forgepilot.agents import AgentDefinition, AgentLimits, AgentRegistry, BUILTIN_AGENT_DEFINITIONS


def definition_payload():
    return {
        "schema_version": "1.0",
        "agent_id": "implementer.v1",
        "display_name": "Implementer",
        "role": "implementer",
        "role_description": "Applies an approved plan.",
        "prompt_version": "implementer-prompt.v1",
        "model_route": "implementation",
        "input_artifacts": ["TaskSpec", "ExecutionPlan", "EvidenceBundle", "ValidationReport"],
        "output_artifacts": ["ChangeSet"],
        "capabilities": ["repository.read", "repository.search", "repository.patch"],
        "limits": {
            "max_model_calls": 8, "max_tool_calls": 40, "max_input_tokens": 64000,
            "max_output_tokens": 12000, "timeout_seconds": 900,
        },
    }


def test_builtin_agent_contracts_are_unique_and_registered():
    registry = AgentRegistry()
    assert len(registry.all()) == 6
    assert len({item.agent_id for item in BUILTIN_AGENT_DEFINITIONS}) == 6
    assert registry.get("reviewer.v1").model_route == "review"


def test_definition_accepts_declarative_contract_from_json():
    definition = AgentDefinition.model_validate_json(__import__("json").dumps(definition_payload()))
    assert definition.output_artifacts == ("ChangeSet",)


@pytest.mark.parametrize("field,value", [
    ("max_model_calls", 65), ("max_tool_calls", 257), ("max_input_tokens", 262145),
    ("max_output_tokens", 65537), ("timeout_seconds", 3601),
])
def test_hard_limits_reject_excess(field, value):
    limits = {
        "max_model_calls": 8, "max_tool_calls": 40, "max_input_tokens": 64000,
        "max_output_tokens": 12000, "timeout_seconds": 900,
    }
    limits[field] = value
    with pytest.raises(ValidationError):
        AgentLimits(**limits)


def test_capability_whitelist_and_role_policy_reject_escalation():
    payload = definition_payload()
    payload["capabilities"].append("shell.execute")
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate_json(__import__("json").dumps(payload))

    payload = definition_payload()
    payload["role"] = "reviewer"
    payload["model_route"] = "review"
    with pytest.raises(ValidationError, match="repository.patch"):
        AgentDefinition.model_validate_json(__import__("json").dumps(payload))


@pytest.mark.parametrize("plugin_field", ["python_module", "entry_point", "plugin", "callable"])
def test_arbitrary_python_plugin_declarations_are_rejected(plugin_field):
    payload = definition_payload()
    payload[plugin_field] = "evil.module:run"
    with pytest.raises(ValidationError, match="Extra inputs"):
        AgentDefinition.model_validate_json(__import__("json").dumps(payload))


def test_unknown_agent_schema_version_is_rejected():
    payload = definition_payload()
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate_json(__import__("json").dumps(payload))
