"""Evidence and policy gates against integrity-checked SQLite artifacts."""
from contextlib import closing
import json
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import DataPolicy
from forgepilot.errors import ForgePilotError
from forgepilot.mission_tools import MissionToolSelection
from forgepilot.security import Redactor
from forgepilot.skill_draft_evidence import (
    MAX_INPUT_BYTES, MAX_INPUT_CHARS, prepare_draft_input, suggestion_schema, validate_suggestion,
)
from forgepilot.skill_mission_drafts import create_mission_draft
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import PLAN_ID, TASK_ID, TASK_SPEC_ID, all_contents
from test_mission_events import make_mission
from test_skill_mission_drafts import put, set_state


@pytest.fixture
def store(tmp_path):
    with closing(SqliteEventStore(tmp_path / "draft.sqlite3", artifact_root=tmp_path / "artifacts")) as store:
        make_mission(store)
        set_state(store, "failed")
        freeze_policy(store, False)
        yield store


def freeze_policy(store, allow_cloud):
    selection = MissionToolSelection(False, DataPolicy(cloud_model_allowed=allow_cloud)).to_dict()
    with store.transaction() as db:
        db.execute("""UPDATE graph_events SET payload_json=json_set(payload_json,'$.tool_selection',json(?))
                      WHERE task_id=? AND event_type='task.created'""", (json.dumps(selection), TASK_ID))


def model_input(store, note="", **options):
    draft = create_mission_draft(store, TASK_ID, developer_note=note)
    return prepare_draft_input(store, draft, "attempt-1", DataPolicy(), **options)


def model_output(keys, **replacements):
    fields = dict(phenomenon="Stale list", priority_checks="Check the row", decision_branches="Observe the model",
                  stop_conditions="Record the signal", boundaries="One model", conclusion="Possible stale data",
                  evidence_keys=keys)
    return json.dumps({**fields, **replacements})


@pytest.mark.parametrize(("frozen", "spec", "current", "allowed"), [
    (True, True, True, True), (False, True, True, False), (True, False, True, False),
    (True, True, False, False),
])
def test_policy_intersects_admission_spec_and_current(store, frozen, spec, current, allowed):
    freeze_policy(store, frozen)
    task, evidence, *_ = all_contents()
    put(store, task.model_copy(update={"data_policy": DataPolicy(cloud_model_allowed=spec)}))
    put(store, evidence)
    draft = create_mission_draft(store, TASK_ID)
    prepared = prepare_draft_input(store, draft, "attempt", DataPolicy(cloud_model_allowed=current))
    assert prepared.policy.cloud_model_allowed is allowed


def test_bounded_public_projection_and_attempt_scoped_basis(store):
    spec, findings, plan, changes, validation, *_ = all_contents()
    for identifier, parents, content in ((TASK_SPEC_ID, (), spec), (PLAN_ID, (TASK_SPEC_ID,), plan),
                                         (str(uuid4()), (PLAN_ID,), changes)):
        store.artifacts.put(create_artifact(task_id=TASK_ID, producer="fixture.v1", producer_node="work",
                                           artifact_id=identifier, parent_artifact_ids=parents, content=content))
    item, = findings.items
    put(store, findings.model_copy(update={"items": (item, item.model_copy(
        update={"evidence_id": "EV-2", "path": ".hidden-tests/test.cpp", "claim": "Private sentinel."}))}))
    command, = validation.commands
    put(store, validation.model_copy(update={"commands": (command, command.model_copy(
        update={"kind": "protected_test", "output_summary": "Private result."}))}))
    draft = create_mission_draft(store, TASK_ID, developer_note="System prompt: private instruction")
    prepared = prepare_draft_input(store, draft, "first", DataPolicy())
    sent = prepared.messages[1]["content"]
    for expected in (changes.summary, spec.objective, item.claim, "test_exit_code"):
        assert expected in sent
    for forbidden in (changes.files[0].path, "Private sentinel", "Private result", "@@", "protected_test",
                      validation.summary, spec.problem.observed_behavior, "private instruction", TASK_SPEC_ID):
        assert forbidden not in sent
    key = next(key for key, ref in prepared.references.items() if ref.classification == "verified_result")
    basis = validate_suggestion(model_output([key, "unknown"]), prepared)["basis"]
    assert basis == [{"key": key, "classification": "verified_result",
                      "reference": prepared.references[key].model_dump(mode="json")},
                     {"key": "unknown", "classification": "pending_inference", "reference": None}]
    retry = prepare_draft_input(store, draft, "second", DataPolicy())
    assert validate_suggestion(model_output([key]), retry)["basis"][0]["reference"] is None
    assert validate_suggestion(model_output([]), retry)["basis"] == [
        {"key": None, "classification": "pending_inference", "reference": None}]


def test_byte_budget_and_section_validation(store):
    findings = all_contents()[1]
    item, = findings.items
    put(store, findings.model_copy(update={"items": tuple(
        item.model_copy(update={"evidence_id": f"EV-{i+1}", "claim": "现" * 2000}) for i in range(12))}))
    prepared = model_input(store, note="工作" * 250)
    messages = prepared.messages + [{"role": "system", "content":
        "Return the final response as JSON matching this output schema: " +
        json.dumps(suggestion_schema(), ensure_ascii=False)}]
    raw = json.dumps(messages, ensure_ascii=False)
    assert len(raw.encode()) <= MAX_INPUT_BYTES and len(raw) <= MAX_INPUT_CHARS
    assert 0 < len(prepared.references) < 8
    for raw in (model_output([], conclusion="x" * 513), model_output(["x"] * 9), model_output([], extra="x"),
                model_output([], conclusion="password: leak")):
        with pytest.raises(ForgePilotError, match="invalid_skill_suggestion"):
            validate_suggestion(raw, prepared)


@pytest.mark.parametrize("prohibited", ["D:/private/config", "RAW_PROMPT: message", "PATH=/private/bin",
                                         "def hidden_fixture(): return 17", "@@\n+private line", "gateway-provider-key"])
def test_free_text_never_bypasses_projection(store, prohibited):
    findings = all_contents()[1]
    item, = findings.items
    put(store, findings.model_copy(update={"items": (item.model_copy(update={"claim": prohibited}),)}))
    put(store, all_contents()[4])
    prepared = model_input(store, note=prohibited, redactor=Redactor(("gateway-provider-key",)))
    assert prohibited not in json.dumps(prepared.messages, ensure_ascii=False)
    assert "investigation" not in prepared.messages[1]["content"]
    assert "developer_observation" not in prepared.messages[1]["content"]


@pytest.mark.parametrize("field", ["record_id", "content_sha256", "locator"])
def test_forged_reference_rejected_at_dispatch(store, field):
    findings = all_contents()[1]
    put(store, findings)
    other = put(store, findings, task_id=str(uuid4()))
    draft = create_mission_draft(store, TASK_ID)
    ref, = draft.data.evidence_refs
    corrupt = {"record_id": other.artifact_id, "content_sha256": "0" * 64, "locator": "/items/5/claim"}
    data = draft.data.model_copy(update={"evidence_refs": (ref.model_copy(update={field: corrupt[field]}),)})
    with pytest.raises(ForgePilotError, match="invalid_skill_evidence"):
        prepare_draft_input(store, draft.model_copy(update={"data": data}), "attempt", DataPolicy())


@pytest.mark.parametrize("damage", ["oversize", "invalid"])
def test_unreadable_artifact_does_not_prevent_local_note_or_attempt(store, damage):
    artifact = put(store, all_contents()[1])
    with store.transaction() as db:
        if damage == "oversize":
            db.execute("UPDATE artifacts SET size_bytes=50000000 WHERE artifact_id=?", (artifact.artifact_id,))
        else:
            db.execute("UPDATE artifacts SET payload_sha256=? WHERE artifact_id=?", ("0" * 64, artifact.artifact_id))
    draft, attempt, created = store.skills.create_generation(TASK_ID, developer_note="Remember the observation",
                                                            template_version="skill-draft.v1")
    assert created and not draft.data.evidence_refs
    with pytest.raises(ForgePilotError, match="skill_draft_evidence_insufficient"):
        prepare_draft_input(store, draft, attempt, DataPolicy())
    assert store.skills.get_draft(draft.draft_id).data.developer_note == "Remember the observation"
