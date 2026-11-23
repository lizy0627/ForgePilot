"""Version isolation and reproducible artifacts are protocol compatibility gates."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from forgepilot.api import create_app
from forgepilot.api_models import ConversationEvent, EventPayload
from forgepilot.api_v2_models import (
    CreateMission, MissionApproval, MissionControl, MissionDelivery, MissionErrorResponse,
    MissionResume, MissionResponse, MissionRetry, MissionScopeExpansion,
    SkillRecommendationRequest, SkillRecommendations,
)
from forgepilot.export_protocol import export, generated_protocol
from forgepilot.mission_events import MissionEvent
from forgepilot.protocol_examples import examples


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
        ("skill_recommendations_request", SkillRecommendationRequest),
        ("skill_recommendations_excluded_request", SkillRecommendationRequest),
        ("skill_recommendations_unmatched_request", SkillRecommendationRequest),
        ("skill_recommendations_response", SkillRecommendations),
        ("skill_recommendations_empty_response", SkillRecommendations),
        ("mission_create_request", CreateMission),
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
