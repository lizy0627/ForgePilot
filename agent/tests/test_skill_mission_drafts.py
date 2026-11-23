"""Mission admission, cutoff and integrity checks for manual skill drafts."""
from contextlib import closing
import json
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import publish_mission_event
from forgepilot.skill_mission_drafts import create_mission_draft
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import PLAN_ID, TASK_ID, TASK_SPEC_ID, all_contents, task_spec
from test_mission_events import make_mission


@pytest.fixture
def store(tmp_path):
    with closing(SqliteEventStore(tmp_path / "notes.sqlite3", artifact_root=tmp_path / "artifacts")) as store:
        yield store


def set_state(store, status):
    with store.transaction() as db:
        db.execute("UPDATE task_graph_runs SET status=? WHERE task_id=?", (status, TASK_ID))


def put(store, content, task_id=TASK_ID):
    artifact = create_artifact(task_id=task_id, producer="fixture.v1", producer_node="work", content=content)
    store.artifacts.put(artifact)
    return artifact


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "completed", "skipped"])
def test_manual_draft_uses_task_status_and_freezes_revision(store, status):
    # A TaskSpec can exist before task.created and therefore lack its own event.
    artifact = put(store, task_spec())
    make_mission(store)
    set_state(store, status)
    cutoff = store.missions.get_graph_run(TASK_ID)["current_revision"]
    draft = create_mission_draft(store, TASK_ID, developer_note="Check the model row.", request_id="create")
    assert draft.data.evidence_revision == cutoff
    assert draft.data.generation_status == "manual"
    refs = draft.data.evidence_refs
    if status == "skipped":
        assert refs == ()
    else:
        ref, = refs
        assert (ref.record_id, ref.content_sha256, ref.locator, ref.classification) == (
            artifact.artifact_id, artifact.content_sha256, "/objective", "source_context")
    # A later event, new artifact and edit must not change the create receipt.
    put(store, task_spec())
    publish_mission_event(store, TASK_ID, "workspace.restored", {})
    edited = store.skills.edit_draft(draft.draft_id, expected_revision=1,
                                     data=draft.data.model_copy(update={"developer_note": "Add a trace."}))
    replay = create_mission_draft(store, TASK_ID, developer_note="Check the model row.", request_id="create")
    assert replay == edited and replay.data.evidence_revision == cutoff
    with pytest.raises(ForgePilotError, match="skill_request_conflict"):
        create_mission_draft(store, TASK_ID, developer_note="Different note.", request_id="create")


@pytest.mark.parametrize("status", ["interrupted", "retryable", "running", "awaiting_human", "pending"])
def test_finished_node_does_not_qualify_nonterminal_mission(store, status):
    make_mission(store)
    set_state(store, status)
    with store.transaction() as db:
        db.execute("UPDATE graph_node_states SET status='succeeded' WHERE task_id=?", (TASK_ID,))
    with pytest.raises(ForgePilotError, match="mission_not_terminal"):
        create_mission_draft(store, TASK_ID)
    assert not store.skills.list_drafts()


def test_terminal_graph_without_mission_admission_cannot_supply_evidence(store):
    make_mission(store)
    set_state(store, "succeeded")
    with store.transaction() as db:
        db.execute("DELETE FROM graph_events WHERE event_type='task.created'")
    with pytest.raises(ForgePilotError, match="unknown_mission"):
        create_mission_draft(store, TASK_ID)


def test_validation_and_observations_keep_recorded_meaning(store):
    make_mission(store)
    _, evidence, _, _, report, _, _ = all_contents()
    item, = evidence.items
    other_item = item.model_copy(update={"evidence_id": "EV-2", "source": "user"})
    bundle = put(store, evidence.model_copy(update={"items": (item, other_item)}))
    command, = report.commands
    report = report.model_copy(update={"status": "failed", "failure_classification": "test",
                                       "commands": (command.model_copy(update={"kind": "protected_test"}),
                                                    command.model_copy(update={"exit_code": 7}))})
    validation = put(store, report)
    foreign = put(store, task_spec(), task_id=str(uuid4()))
    set_state(store, "cancelled")
    draft = create_mission_draft(store, TASK_ID)
    refs = draft.data.evidence_refs
    assert {ref.record_id for ref in refs} == {bundle.artifact_id, validation.artifact_id}
    assert foreign.artifact_id not in draft.model_dump_json()
    assert {(ref.locator, ref.classification, ref.recorded_value) for ref in refs} == {
        ("/status", "verified_result", "failed"), ("/commands/1/exit_code", "verified_result", "7"),
        ("/items/0/claim", "pending_inference", None), ("/items/1/claim", "developer_observation", None)}
    assert "protected_test" not in draft.model_dump_json() and not draft.data.content.body


def test_bounded_snapshot_covers_changes_without_copying_patch(store):
    make_mission(store)
    spec, evidence, plan, changes, report, _, _ = all_contents()
    for artifact_id, content, parents in ((TASK_SPEC_ID, spec, ()), (PLAN_ID, plan, (TASK_SPEC_ID,)),
                                          (str(uuid4()), changes, (PLAN_ID,))):
        store.artifacts.put(create_artifact(task_id=TASK_ID, artifact_id=artifact_id, producer="fixture.v1",
                                            producer_node="work", content=content, parent_artifact_ids=parents))
    item, = evidence.items
    put(store, evidence.model_copy(update={"items": tuple(item.model_copy(update={"evidence_id": f"EV-{i}"})
                                                           for i in range(1, 12))}))
    put(store, report)
    set_state(store, "succeeded")
    draft = create_mission_draft(store, TASK_ID)
    assert len(draft.data.evidence_refs) == 8
    locators = {ref.locator for ref in draft.data.evidence_refs}
    assert {"/objective", "/status", "/items/0/claim", "/files/0/change_type"} <= locators
    assert changes.files[0].unified_diff not in draft.model_dump_json()
    assert changes.files[0].path not in draft.model_dump_json()


@pytest.mark.parametrize("corruption", ["owner", "digest", "payload", "locator", "value"])
def test_read_checks_artifact_owner_payload_and_recorded_result(store, corruption):
    make_mission(store)
    other_id = str(uuid4())
    make_mission(store, other_id)
    report = all_contents()[4]
    artifact = put(store, report)
    foreign = put(store, report, task_id=other_id)
    set_state(store, "failed")
    draft = create_mission_draft(store, TASK_ID)
    with store.transaction() as db:
        if corruption == "payload":
            db.execute("UPDATE artifacts SET inline_json=? WHERE artifact_id=?", (b"{}", artifact.artifact_id))
        else:
            data = json.loads(draft.data.model_dump_json())
            field, value = {"owner": ("record_id", foreign.artifact_id), "digest": ("content_sha256", "0" * 64),
                            "locator": ("locator", "/commands/999/exit_code"), "value": ("recorded_value", "0")
                            }[corruption]
            data["evidence_refs"][0][field] = value
            db.execute("UPDATE skill_drafts SET data_json=? WHERE draft_id=?", (json.dumps(data), draft.draft_id))
    with pytest.raises(ForgePilotError, match="invalid_skill_evidence"):
        store.skills.get_draft(draft.draft_id)
