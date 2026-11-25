"""Generation receipts, crash recovery, idempotency and separation from editor revisions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.skill_models import SkillDraftData
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID
from test_mission_events import finish_mission, make_mission


TEMPLATE = "skill-draft.v1"
SUGGESTION = {
    "phenomenon": "A stale row", "priority_checks": "Read the model row", "decision_branches": "Check the role",
    "stop_conditions": "Stop once the branch is known", "boundaries": "Only after a successful save",
    "conclusion": "Inspect the model before binding",
    "basis": [{"key": None, "classification": "pending_inference", "reference": None}],
}


def create(store, *, request_id="create"):
    return store.skills.create_generation(TASK_ID, developer_note="Keep my handwritten note",
                                         request_id=request_id, template_version=TEMPLATE)


def test_v12_upgrade_preserves_original_manual_draft(tmp_path):
    path = tmp_path / "v12.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        draft = store.skills.create_draft(request_id="legacy", data=SkillDraftData(developer_note="Private note"))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP VIEW skill_promotion_sources")
        db.execute("DROP INDEX skill_versions_by_hash")
        db.execute("ALTER TABLE skills DROP COLUMN builtin_withdrawn")
        db.execute("DROP TABLE skill_generation_attempts")
        db.execute("PRAGMA user_version=12")
    with closing(SqliteEventStore(path)) as store:
        assert store.skills.get_draft(draft.draft_id) == draft
        with closing(sqlite3.connect(store.migration_backup_path)) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (12,)
            assert db.execute("SELECT draft_id FROM skill_drafts").fetchone() == (draft.draft_id,)


def test_create_rolls_back_note_when_receipt_cannot_be_saved():
    with closing(SqliteEventStore()) as store:
        make_mission(store)
        finish_mission(store)
        with store.transaction() as db:
            db.execute("""CREATE TRIGGER fail_generation BEFORE INSERT ON skill_generation_attempts
                          BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            create(store)
        assert store.skills.list_drafts() == []


def test_double_click_and_competing_retry_share_only_the_original_receipt():
    with closing(SqliteEventStore()) as store:
        make_mission(store)
        finish_mission(store)
        barrier = Barrier(2)

        def click():
            barrier.wait(timeout=5)
            return create(store)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(click) for _ in range(2)]
            a, b = [f.result(timeout=10) for f in futures]
        assert a[:2] == b[:2]
        assert {a[2], b[2]} == {True, False}
        draft, attempt_id = a[:2]
        with pytest.raises(ForgePilotError, match="skill_revision_conflict"):
            store.skills.regenerate(draft.draft_id, expected_revision=1, request_id="retry",
                                    template_version=TEMPLATE)
        assert store.skills.finish_generation(attempt_id, start_revision=1, status="cancelled")
        new, retry_id, started = store.skills.regenerate(draft.draft_id, expected_revision=1, request_id="retry",
                                                        template_version=TEMPLATE)
        assert started and retry_id != attempt_id and new.latest_attempt.attempt_id == retry_id
        replay, replay_id, started = create(store)
        assert not started and replay_id == attempt_id and replay == new


def test_interrupted_job_and_late_callback_preserve_current_attempt_and_editor(tmp_path):
    path = tmp_path / "restart.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        make_mission(store)
        finish_mission(store)
        draft, original, _ = create(store)
        assert store.skills.record_generation_route(original, 1, "summary/primary")
        edited = store.skills.patch_draft(draft.draft_id, expected_revision=1,
                                          changes={"developer_note": "Keep the revised note", "name": "Edited name"})
        assert edited.revision == 2 and edited.data.generation_status == "generating"

    with closing(SqliteEventStore(path)) as store:
        recovered = store.skills.get_draft(draft.draft_id)
        assert (recovered.data.generation_status, recovered.latest_attempt.route) == ("interrupted", "summary/primary")
        active, retry_id, started = store.skills.regenerate(draft.draft_id, expected_revision=2, request_id="retry",
                                                           template_version=TEMPLATE)
        assert started and active.latest_attempt.start_revision == 2
        assert not store.skills.finish_generation(original, start_revision=2, status="ready", suggestion=SUGGESTION)
        assert store.skills.get_attempt(draft.draft_id, original).suggestion is None
        assert not store.skills.finish_generation(original, start_revision=1, status="ready", suggestion=SUGGESTION,
                                                  usage={"completion_tokens": 10, "private note": 1})
        stale = store.skills.get_attempt(draft.draft_id, original)
        assert stale.status == "interrupted" and stale.suggestion.conclusion == SUGGESTION["conclusion"]
        assert stale.usage == {"completion_tokens": 10}
        assert store.skills.get_draft(draft.draft_id) == active
        assert store.skills.finish_generation(retry_id, start_revision=2, status="failed", error_code="llm_http")
        assert not store.skills.finish_generation(retry_id, start_revision=2, status="ready", suggestion=SUGGESTION)
        current = store.skills.get_draft(draft.draft_id)
        assert current.latest_attempt.status == "failed" and current.latest_attempt.suggestion is None
        assert current.revision == 2 and current.data.developer_note == "Keep the revised note"
        assert current.data.name == "Edited name"
        assert store.skills.regenerate(draft.draft_id, expected_revision=2, request_id="retry",
                                       template_version=TEMPLATE) == (current, retry_id, False)


def test_generation_admission_preserves_but_never_opens_oversized_manual_reference(tmp_path, monkeypatch):
    from forgepilot.artifacts import create_artifact
    from forgepilot.artifacts.models import DataPolicy
    from forgepilot.mission_tools import MissionToolSelection
    from forgepilot.skill_draft_evidence import prepare_draft_input
    from forgepilot.skill_mission_drafts import create_mission_draft
    from test_artifact_protocol import all_contents

    with closing(SqliteEventStore(artifact_root=tmp_path)) as store:
        make_mission(store)
        finish_mission(store)
        _, evidence, _, _, report, _, _ = all_contents()
        item = evidence.items[0]
        large_items = tuple(item.model_copy(update={"evidence_id": f"EV-{i}", "reason": "x" * 8192})
                            for i in range(1, 130))
        large = create_artifact(task_id=TASK_ID, producer="fixture.v1", producer_node="work",
                                content=evidence.model_copy(update={"items": large_items}))
        store.artifacts.put(large)
        store.artifacts.put(create_artifact(task_id=TASK_ID, producer="fixture.v1", producer_node="work",
                                            content=report))
        draft = create_mission_draft(store, TASK_ID, developer_note="Retain the original note")
        assert any(ref.record_id == large.artifact_id for ref in draft.data.evidence_refs)
        original_read = store.missions.owned_artifact

        def checked_read(task_id, artifact_id):
            assert artifact_id != large.artifact_id
            return original_read(task_id, artifact_id)

        monkeypatch.setattr(store.missions, "owned_artifact", checked_read)
        policy = DataPolicy(cloud_model_allowed=False)
        monkeypatch.setattr(store.missions, "metadata", lambda _: {
            "tool_selection": MissionToolSelection(False, policy).to_dict()})
        active, attempt_id, started = store.skills.regenerate(draft.draft_id, expected_revision=1,
                                                             request_id="retry", template_version=TEMPLATE)
        assert started and active.data.evidence_refs == draft.data.evidence_refs
        prepared = prepare_draft_input(store, active, attempt_id, policy)
        assert prepared.references and all(ref.record_id != large.artifact_id for ref in prepared.references.values())
        assert store.skills.regenerate(draft.draft_id, expected_revision=1, request_id="retry",
                                       template_version=TEMPLATE) == (active, attempt_id, False)
        generated, _, _ = create(store)
        assert all(ref.record_id != large.artifact_id for ref in generated.data.evidence_refs)
