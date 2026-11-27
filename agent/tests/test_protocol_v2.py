"""Version isolation and reproducible artifacts are protocol compatibility gates."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from forgepilot.api import create_app
from forgepilot.api_models import ConversationEvent, EventPayload
from forgepilot.api_v2_models import (
    CreateMission, MissionApproval, MissionControl, MissionDelivery, MissionErrorResponse,
    MissionResume, MissionResponse, MissionRetry, MissionScopeExpansion,
    MissionNode, MissionSkills, SkillRecommendationRequest, SkillRecommendations,
    CreateSkillDraft, CreateSkillRevisionDraft, DisableSkill, PublishLocalSkill,
    SaveSkillDraft, RegenerateSkillDraft, SkillDraftPage, SkillPage,
)
from forgepilot.export_protocol import export, generated_protocol
from forgepilot.mission_events import MissionEvent
from forgepilot.protocol_examples import examples
from forgepilot.skill_models import SkillDraft, SkillGenerationAttempt, SkillIdentity, SkillVersion


ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("fixtures") / "protocol_v1.snapshot.json"


def _v1_openapi(document):
    paths = {path: operation for path, operation in document["paths"].items()
             if path.startswith("/api/v1/")}
    schemas = {}

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in schemas:
                    schemas[name] = document["components"]["schemas"][name]
                    visit(schemas[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(paths)
    return paths, {"schemas": schemas}


def test_api_v1_paths_and_reachable_schemas_match_frozen_snapshot():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    app = create_app(lambda trace: None)
    try:
        paths, components = _v1_openapi(app.openapi())
        assert paths == baseline["paths"]
        assert components == baseline["components"]
    finally:
        app.state.task_host.store.close()


def test_api_v1_events_and_examples_remain_unchanged():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert EventPayload.model_json_schema() == baseline["events"]
    assert TypeAdapter(ConversationEvent).json_schema() == baseline["conversation_events"]
    current = examples()
    assert {name: current[name] for name in baseline["examples"]} == baseline["examples"]


def test_all_generated_protocol_artifacts_have_no_drift(tmp_path):
    export(tmp_path)
    expected = generated_protocol()
    assert {file.name for file in tmp_path.iterdir()} == set(expected)
    for name, value in expected.items():
        assert json.loads((ROOT / "protocol" / name).read_text(encoding="utf-8")) == value, name
        # Check deterministic formatting as well as schema object equality.
        assert (ROOT / "protocol" / name).read_bytes() == (tmp_path / name).read_bytes(), name


def test_v2_request_examples_match_strict_transport_schemas():
    samples = examples()
    for name, model in (
        ("skill_generate_request", CreateSkillDraft),
        ("skill_generate_accepted_response", SkillDraft),
        ("skill_generation_ready_response", SkillGenerationAttempt),
        ("skill_generation_insufficient_response", SkillGenerationAttempt),
        ("skill_generation_interrupted_response", SkillGenerationAttempt),
        ("skill_regenerate_request", RegenerateSkillDraft),
        ("skill_save_edited_suggestion_request", SaveSkillDraft),
        ("skill_draft_create_request", CreateSkillDraft),
        ("skill_draft_create_response", SkillDraft),
        ("skill_draft_with_validation_response", SkillDraft),
        ("skill_draft_save_request", SaveSkillDraft),
        ("skill_draft_save_response", SkillDraft),
        ("skill_draft_page_response", SkillDraftPage),
        ("skill_draft_detail_response", SkillDraft),
        ("skill_publish_local_request", PublishLocalSkill),
        ("skill_publish_local_response", SkillVersion),
        ("skill_page_response", SkillPage),
        ("skill_version_detail_response", SkillVersion),
        ("skill_revision_draft_request", CreateSkillRevisionDraft),
        ("skill_revision_draft_response", SkillDraft),
        ("skill_disable_request", DisableSkill),
        ("skill_disable_response", SkillIdentity),
        ("skill_save_conflict_error", MissionErrorResponse),
        ("skill_evidence_mismatch_error", MissionErrorResponse),
        ("skill_recommendations_request", SkillRecommendationRequest),
        ("skill_recommendations_excluded_request", SkillRecommendationRequest),
        ("skill_recommendations_unmatched_request", SkillRecommendationRequest),
        ("skill_recommendations_response", SkillRecommendations),
        ("skill_recommendations_empty_response", SkillRecommendations),
        ("mission_create_request", CreateMission),
        ("mission_create_with_skills_request", CreateMission),
        ("mission_skill_selection_changed_error", MissionErrorResponse),
        ("mission_skills_response", MissionSkills),
        ("mission_skills_empty_response", MissionSkills),
        ("mission_missing_current_evidence_node", MissionNode),
        ("mission_execution_pending_node", MissionNode),
        ("mission_scope_pending_node", MissionNode),
        ("mission_retryable_node", MissionNode),
        ("mission_interrupted_node", MissionNode),
        ("mission_approve_request", MissionApproval),
        ("mission_reject_request", MissionApproval),
        ("mission_scope_approval_request", MissionApproval),
        ("mission_scope_expansion_preview", MissionScopeExpansion),
        ("mission_cancel_request", MissionControl),
        ("mission_retry_request", MissionRetry),
        ("mission_resume_request", MissionResume),
        ("mission_delivery_request", MissionDelivery),
        ("mission_revision_conflict_error", MissionErrorResponse),
        ("mission_approval_conflict_error", MissionErrorResponse),
        ("mission_query_response", MissionResponse),
    ):
        model.model_validate(samples[name], strict=True)
        Draft202012Validator(model.model_json_schema()).validate(samples[name])


def test_node_retry_actions_distinguish_legacy_omission_from_no_action():
    current = examples()["mission_missing_current_evidence_node"]
    assert MissionNode.model_validate(current).model_dump()["retry_actions"] == []
    legacy = dict(current)
    del legacy["retry_actions"]
    assert MissionNode.model_validate(legacy).model_dump() == legacy


@pytest.mark.parametrize("actions", [None, "retry", ["recover"], ["retry", "retry"]])
def test_node_retry_actions_require_distinct_supported_actions(actions):
    node = examples()["mission_retryable_node"] | {"retry_actions": actions}
    with pytest.raises(ValidationError):
        MissionNode.model_validate(node)
    assert not Draft202012Validator(MissionNode.model_json_schema()).is_valid(node)


def test_v2_event_examples_cover_every_public_event_category():
    samples = [value for name, value in examples().items() if name.startswith("mission_event_")]
    validator = Draft202012Validator(MissionEvent.model_json_schema())
    categories = set()
    for sample in samples:
        MissionEvent.model_validate(sample, strict=True)
        validator.validate(sample)
        categories.add(sample["event_type"].split(".")[0])
    assert categories == {"task", "node", "artifact", "approval", "tool", "model", "validation", "workspace", "security"}
    assert len({sample["event_id"] for sample in samples}) == len(samples)
