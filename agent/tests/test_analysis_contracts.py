"""Offline contracts for the three versioned read-only analysis roles."""
from copy import deepcopy
import json

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError as JsonValidationError
from pydantic import ValidationError

from forgepilot.agents import AgentRegistry
from forgepilot.agents.contracts import (
    contract_schemas, input_schema, output_schema, validate_evidence_bundle,
    validate_execution_plan, validate_task_spec,
)
from forgepilot.agents.prompts import AGENT_PROMPT_VERSIONS, get_prompt
from forgepilot.artifacts import (
    ArtifactRelationshipError, TaskSpec, content_sha256, create_artifact, validate_artifact,
)
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute


TASK_ID = "a0000000-0000-4000-a000-000000000001"
SPEC_ID = "a0000000-0000-4000-a000-000000000002"


def task_payload():
    return {
        "task_type": "bug_fix", "title": "Cancel worker on close",
        "objective": "Stop the background worker before window destruction.",
        "repository": {"base_ref": "HEAD", "toolchain_profile": "windows-msvc-qt6"},
        "problem": {"observed_behavior": "Worker outlives the window.",
                    "expected_behavior": "Worker terminates within one second of close.",
                    "reproduction_steps": ["Start worker", "Close window"]},
        "acceptance_criteria": [
            {"id": "AC-1", "description": "Close-window regression stops worker within one second.",
             "verification": "protected_test"},
            {"id": "AC-2", "description": "The application and tests build successfully.",
             "verification": "build"},
            {"id": "AC-3", "description": "All existing CTest tests pass without modification.",
             "verification": "ctest"},
        ],
        "allowed_scope": {"path_patterns": ["src/**", "tests/**"], "max_changed_files": 4},
        "forbidden_scope": [".git/**", "build/**", ".forgepilot/oracle/**"],
        "test_policy": {"existing_tests_read_only": True, "allow_new_tests": True,
                        "modifiable_existing_test_patterns": []},
        "data_policy": {"cloud_model_allowed": False, "external_mcp_allowed": False},
        "assumptions": [], "risks": ["Cancellation must preserve thread ownership."],
        "risk_level": "medium",
    }


def evidence_payload():
    return {"summary": "The destructor omits worker cancellation.", "items": [{
        "evidence_id": "EV-1", "claim": "The worker can outlive the window.",
        "path": "src/window.cpp", "start_line": 10, "end_line": 12,
        "file_hash": "a" * 64, "symbol": "Window::~Window", "source": "local_repository",
        "confidence": 0.9, "reason": "The destructor deletes the UI without cancelling the worker.",
    }]}


def plan_payload():
    return {"task_spec_artifact_id": SPEC_ID, "summary": "Cancel and verify worker shutdown.", "steps": [{
        "step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [],
        "expected_files": ["src/window.cpp", "tests/window_shutdown_test.cpp"],
        "action": "Cancel worker before destruction and add the regression test.",
        "verification": ["protected_test", "build", "ctest"],
        "risk": "Thread shutdown may deadlock if the UI blocks a queued callback.",
        "adds_tests": True, "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3"],
    }]}


def fake_gateway(handler):
    profile = ModelProfile(profile_id="fake", base_url="https://offline.test/v1",
                           api_key_ref="env:FORGEPILOT_API_KEY", model="offline-model",
                           family="offline", max_output_tokens=2048)
    config = ModelConfiguration(profiles=(profile,), routes=(
        ModelRoute(route_id="analysis", primary="fake"),
        ModelRoute(route_id="orchestration", primary="fake"),
    ))
    return ModelGateway(config, secret_resolver=lambda _: "offline-key",
                        transport_factory=lambda _: httpx.MockTransport(handler))


def completion(payload):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,agent_id,payload", [
    ("draft_task_spec", "orchestrator.v1", task_payload),
    ("root_cause_analysis", "root-cause-analyst.v1", evidence_payload),
    ("impact_test_analysis", "impact-test-analyst.v1", evidence_payload),
    ("generate_plan", "orchestrator.v1", plan_payload),
])
async def test_fake_model_each_role_repairs_once_to_the_versioned_contract(stage, agent_id, payload):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion({"chain_of_thought": "not allowed"}
                                                   if len(seen) == 1 else payload()))

    definition = AgentRegistry().get(agent_id)
    schema = output_schema(stage, task_spec=validate_task_spec(task_payload()))
    async with fake_gateway(handler) as gateway:
        client = gateway.for_agent(definition)
        result = await client.chat([
            {"role": "system", "content": get_prompt(agent_id, stage)},
            {"role": "user", "content": "Only the supplied task context is available."},
        ], output_schema=schema)
        Draft202012Validator(schema).validate(json.loads(result.message.content))
        assert [record.attempt for record in gateway.accounting.snapshot()] == ["primary", "schema_repair"]
        assert {record.role for record in gateway.accounting.snapshot()} == {definition.role}
        assert {record.prompt_version for record in gateway.accounting.snapshot()} == {definition.prompt_version}
        assert client.budget.model_calls == 2
        assert "chain_of_thought" not in json.dumps([record.__dict__ if hasattr(record, "__dict__") else
            {"input_hash": record.input_hash, "response_hash": record.response_hash}
            for record in gateway.accounting.snapshot()])


def test_all_input_schemas_validate_selected_context_only():
    spec = create_artifact(task_id=TASK_ID, artifact_id=SPEC_ID, producer="orchestrator.v1",
                           producer_node="draft-task-spec", content=validate_task_spec(task_payload()))
    evidence = create_artifact(task_id=TASK_ID, producer="root-cause-analyst.v1",
                              producer_node="root-cause-analysis", content=validate_evidence_bundle(evidence_payload()))
    inputs = {
        "draft_task_spec": {"request": "Fix the shutdown regression", "repository": task_payload()["repository"],
                            "data_policy": task_payload()["data_policy"], "language": "zh-CN"},
        "root_cause_analysis": {"task_spec": spec.model_dump(mode="json")},
        "impact_test_analysis": {"task_spec": spec.model_dump(mode="json")},
        "generate_plan": {"task_spec": spec.model_dump(mode="json"), "evidence": evidence.model_dump(mode="json")},
    }
    for stage, payload in inputs.items():
        validator = Draft202012Validator(input_schema(stage))
        validator.validate(payload)
        with pytest.raises(JsonValidationError):
            validator.validate({**payload, "chat_history": [{"content": "unselected private data"}]})
    assert len(contract_schemas()) == 8


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(task_type="maintenance"),
    lambda value: value.update(risks=[]),
    lambda value: value.update(forbidden_scope=[]),
    lambda value: value["acceptance_criteria"].pop(1),
    lambda value: value["acceptance_criteria"][0].update(verification="inspection"),
    lambda value: value["acceptance_criteria"][0].update(id="REQ-1"),
    lambda value: value["acceptance_criteria"][0].update(description="works as expected"),
    lambda value: value["problem"].update(reproduction_steps=[]),
])
def test_analysis_tasks_require_verifiable_scope_risks_build_test_and_regression(mutation):
    payload = task_payload()
    mutation(payload)
    with pytest.raises(ValidationError):
        validate_task_spec(payload)


def test_feature_requires_new_behavior_and_does_not_require_bug_reproduction():
    payload = task_payload()
    payload["task_type"] = "feature"
    payload["problem"]["reproduction_steps"] = []
    payload["acceptance_criteria"][0].update(description="New stop button cancels the active worker.", verification="test")
    assert validate_task_spec(payload).task_type == "feature"
    payload["problem"]["expected_behavior"] = payload["problem"]["observed_behavior"]
    with pytest.raises(ValidationError, match="new expected behavior"):
        validate_task_spec(payload)


@pytest.mark.parametrize("field", ["path", "start_line", "end_line", "file_hash", "symbol"])
def test_new_evidence_requires_complete_location_and_hash(field):
    payload = evidence_payload()
    del payload["items"][0][field]
    with pytest.raises(ValidationError):
        validate_evidence_bundle(payload)
    with pytest.raises(JsonValidationError):
        Draft202012Validator(output_schema("root_cause_analysis")).validate(payload)


def test_external_evidence_is_explicit_and_reasons_are_short_without_private_traces():
    payload = evidence_payload()
    payload["items"][0]["source"] = "external"
    assert validate_evidence_bundle(payload).items[0].source == "external"
    for key, value in (("reason", "x" * 513), ("source", "approved_mcp"), ("chain_of_thought", "private")):
        invalid = deepcopy(payload)
        invalid["items"][0][key] = value
        with pytest.raises(ValidationError):
            validate_evidence_bundle(invalid)


def test_plan_coverage_requires_matching_verification_not_only_ids():
    spec = validate_task_spec(task_payload())
    assert validate_execution_plan(spec, plan_payload()).steps[0].adds_tests
    for field, value in (("acceptance_criteria_ids", ["AC-1", "AC-2"]),
                         ("verification", ["build", "ctest"])):
        invalid = plan_payload()
        invalid["steps"][0][field] = value
        with pytest.raises(ArtifactRelationshipError):
            validate_execution_plan(spec, invalid)
        with pytest.raises(JsonValidationError):
            Draft202012Validator(output_schema("generate_plan", task_spec=spec)).validate(invalid)


def test_analysis_definitions_prompts_budgets_and_capabilities_are_bound():
    registry = AgentRegistry()
    for agent_id, prompt_version in AGENT_PROMPT_VERSIONS.items():
        definition = registry.get(agent_id)
        assert definition.prompt_version == prompt_version
        assert prompt_version in get_prompt(agent_id)
        assert "repository.patch" not in definition.capabilities
        assert definition.limits.max_model_calls == (3 if agent_id == "orchestrator.v1" else 6)
        assert ("mcp.read" in definition.capabilities) == (agent_id != "orchestrator.v1")
    with pytest.raises(ValueError):
        get_prompt("root-cause-analyst.v1", "generate_plan")


def test_legacy_v1_artifact_without_new_risks_field_keeps_its_hash():
    legacy = task_payload()
    del legacy["risks"]
    spec = TaskSpec.model_validate(legacy)
    assert "risks" not in spec.model_dump(mode="json")
    artifact = create_artifact(task_id=TASK_ID, producer="orchestrator.v1", producer_node="draft", content=spec)
    assert artifact.content_sha256 == content_sha256(legacy)
    assert validate_artifact(artifact.model_dump_json()).content_sha256 == artifact.content_sha256
