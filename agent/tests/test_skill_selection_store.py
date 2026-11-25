"""Selection resolution, atomic admission and the skill receipt log."""
from contextlib import closing
from uuid import uuid4

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.skill_selection import skill_manifest_sha256
from forgepilot.sqlite_store import SqliteEventStore


def publish(store, draft_data):
    draft = store.skills.create_draft(request_id=str(uuid4()), data=draft_data)
    version = store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id=str(uuid4()))
    return version.model_dump(include={"skill_id", "version", "content_sha256"})


def admit(store, skills):
    store.missions.create_graph_run(task_id="m", graph_id="g", graph_version=1)
    store.missions.admit_mission("m", {"task_id": "m", "workspace_id": "w", "repository_path": "repo",
                                      "worktree_path": "work", "base_ref": "HEAD", "head_commit": "abc",
                                      "status": "created"}, {}, selected_skills=skills)


def test_resolution_validates_identity_content_and_per_node_utf8_budget(tmp_path, qt_draft):
    with closing(SqliteEventStore(tmp_path / "db")) as store:
        ref = publish(store, qt_draft)
        for selection in (None, {}, "", [ref, ref], [ref, ref, ref], [dict(ref, version=True)], [dict(ref, name="forged")],
                          [dict(ref, skill_id="missing")], [dict(ref, content_sha256="a" * 64)]):
            with pytest.raises(ForgePilotError, match="invalid_skill_selection"):
                store.skills.resolve_selection(selection)
        boundaries = ("界" * 256,) * 14
        data = qt_draft.model_copy(update={"content": qt_draft.content.model_copy(update={"boundaries": boundaries})})
        first, second = publish(store, data), publish(store, data)
        assert len(store.skills.resolve_selection([first])) == 1
        with pytest.raises(ForgePilotError, match="skill_budget_exceeded"):
            store.skills.resolve_selection([first, second])
        impact_data = data.model_copy(update={"content": data.content.model_copy(
            update={"stages": ("impact_test_analysis",)})})
        impact = publish(store, impact_data)
        assert len(store.skills.resolve_selection([first, impact])) == 2


@pytest.mark.parametrize("change", ["upgrade", "disable"])
def test_change_between_resolution_and_admission_rolls_back_partial_copy(tmp_path, qt_draft, change):
    path = tmp_path / "db"
    with closing(SqliteEventStore(path)) as store:
        refs = sorted([publish(store, qt_draft), publish(store, qt_draft)], key=lambda ref: ref["skill_id"])
        selected = store.skills.resolve_selection(refs)
        last = selected[-1]
        if change == "disable":
            store.skills.disable(last.skill_id, expected_revision=1)
        else:
            draft = store.skills.draft_from_version(last.skill_id, 1, request_id="revise")
            store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="publish")
        with pytest.raises(ForgePilotError, match="skill_selection_changed"):
            admit(store, selected)
        with pytest.raises(ForgePilotError, match="skill_selection_changed"):
            store.skills.resolve_selection(refs)
        with store.locked_connection() as db:
            for table in ("workspace_records", "mission_skill_snapshots", "graph_events"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            assert db.execute("SELECT sum(use_count) FROM skills").fetchone()[0] == 0
        assert store.missions.get_graph_run("m")["workspace_id"] is None


def test_ordered_receipts_and_corruption_survive_restart(tmp_path, qt_draft, monkeypatch):
    path = tmp_path / "db"
    with closing(SqliteEventStore(path)) as store:
        refs = sorted([publish(store, qt_draft), publish(store, qt_draft)], key=lambda ref: ref["skill_id"])
        selected = store.skills.resolve_selection(refs[::-1])
        admit(store, selected)
        digest = skill_manifest_sha256(selected)
        assert store.skills.mission_usage("m") == {"selected_skills": refs, "injections": []}
        store.skills.disable(selected[0].skill_id, expected_revision=1)

    # A later release may lower the admission limit while old Missions still use both snapshots.
    monkeypatch.setattr("forgepilot.skill_selection.MISSION_SKILL_SELECTION_LIMIT", 1)
    with closing(SqliteEventStore(path)) as store:
        with pytest.raises(ForgePilotError, match="invalid_skill_selection"):
            store.skills.resolve_selection(refs)
        assert store.skills.load_frozen("m", digest) == selected
        for attempt in (1, 1, 2):
            store.skills.record_injection("m", "root_cause_analysis", attempt, selected)
        usage = {"selected_skills": refs, "injections": [dict(ref, node_id="root_cause_analysis", attempt=a)
                                                        for a in (1, 2) for ref in refs]}
        assert store.skills.mission_usage("m") == usage
        with store.transaction() as db:
            events = db.execute("SELECT payload_json FROM graph_events WHERE event_type='node.skills_injected'").fetchall()
            assert len(events) == 2 and all(qt_draft.content.body not in row[0] for row in events)
            db.execute("DROP TRIGGER mission_skill_snapshot_no_update")
            db.execute("UPDATE mission_skill_snapshots SET content_json=json_set(content_json,'$.body','modified')")
        with pytest.raises(ForgePilotError, match="skill_snapshot_invalid"):
            store.skills.load_frozen("m", digest)
        assert store.skills.mission_usage("m") == usage
        with store.transaction() as db:
            db.execute("DELETE FROM mission_skill_snapshots")
        with pytest.raises(ForgePilotError, match="skill_snapshot_invalid"):
            store.skills.load_frozen("m", digest)
        assert store.skills.mission_usage("m") == usage
