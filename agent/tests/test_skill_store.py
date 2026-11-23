"""Local skill authoring using a real database, including failed and racing writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from forgepilot.artifacts.canonical import content_sha256
from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.skill_models import SkillDraftData, SkillEvidenceRef
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID, task_spec
from test_mission_events import finish_mission, make_mission


@pytest.fixture
def store(tmp_path):
    with closing(SqliteEventStore(tmp_path / "skills.sqlite3")) as db:
        yield db


def test_note_only_draft_can_be_reopened_edited_and_found_by_mission(tmp_path, qt_draft):
    path = tmp_path / "reopen.sqlite3"
    note = SkillDraftData(developer_note="下次先观察模型中的值。")
    with closing(SqliteEventStore(path)) as store:
        make_mission(store)
        finish_mission(store)
        draft = store.skills.create_draft(request_id="take-note", source_mission_id=TASK_ID, data=note)
        store.skills.create_draft(request_id="other", data=note)
        with pytest.raises(ForgePilotError, match="skill_draft_incomplete"):
            store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="review")
        assert store.skills.list_available() == []

    with closing(SqliteEventStore(path)) as store:
        assert store.skills.get_draft(draft.draft_id) == draft
        assert store.skills.list_drafts(source_mission_id=TASK_ID) == [draft]
        assert store.skills.create_draft(request_id="take-note", source_mission_id=TASK_ID, data=note) == draft
        with pytest.raises(ForgePilotError, match="skill_request_conflict"):
            store.skills.create_draft(request_id="take-note", source_mission_id=TASK_ID, data=qt_draft)
        edited = store.skills.edit_draft(draft.draft_id, expected_revision=draft.revision, data=qt_draft)

    with closing(SqliteEventStore(path)) as store:
        assert store.skills.get_draft(draft.draft_id) == edited
        assert edited.revision == draft.revision + 1 and edited.data == qt_draft
        assert store.skills.list_available() == []


def test_only_one_simultaneous_editor_can_save_a_revision(store, qt_draft):
    draft = store.skills.create_draft(request_id="create", data=qt_draft)
    ready = Barrier(2)

    def save(note):
        data = qt_draft.model_copy(update={"developer_note": note})
        ready.wait(timeout=10)
        return store.skills.edit_draft(draft.draft_id, expected_revision=draft.revision, data=data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, note) for note in ("观察一", "观察二")]
        winners = []
        conflicts = []
        for future in futures:
            try:
                winners.append(future.result(timeout=20))
            except ForgePilotError as exc:
                conflicts.append(exc.code)
    assert conflicts == ["skill_revision_conflict"]
    assert winners == [store.skills.get_draft(draft.draft_id)]
    assert winners[0].revision == draft.revision + 1
    assert winners[0].data.developer_note in ("观察一", "观察二")


def test_private_evidence_requires_matching_mission_digest_and_revision(store, qt_draft):
    other_id = str(uuid4())
    artifacts = []
    for task_id in (TASK_ID, other_id):
        make_mission(store, task_id)
        artifact = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="work",
                                   content=task_spec())
        store.artifacts.put(artifact)
        finish_mission(store, task_id)
        artifacts.append(SkillEvidenceRef(kind="artifact", record_id=artifact.artifact_id,
                                         content_sha256=artifact.content_sha256))

    with store.locked_connection() as db:
        final_event = db.execute("SELECT sequence,revision FROM graph_events WHERE task_id=? AND event_type='task.completed'",
                                 (TASK_ID,)).fetchone()
        other_sequence = db.execute("SELECT sequence FROM graph_events WHERE task_id=? AND event_type='task.created'",
                                    (other_id,)).fetchone()[0]
    cutoff = final_event["revision"]
    event = SkillEvidenceRef(kind="event", record_id=f"{TASK_ID}:{final_event['sequence']}")
    node = SkillEvidenceRef(kind="node", record_id="work")
    data = qt_draft.model_copy(update={"evidence_refs": (artifacts[0], event, node), "evidence_revision": cutoff})
    draft = store.skills.create_draft(request_id="with-evidence", source_mission_id=TASK_ID, data=data)
    assert draft.data.evidence_refs == (artifacts[0], event, node)

    invalid = (
        (artifacts[1], cutoff),
        (artifacts[0], 1),
        (artifacts[0].model_copy(update={"content_sha256": "0" * 64}), cutoff),
        (SkillEvidenceRef(kind="node", record_id="unknown-node"), cutoff),
        (SkillEvidenceRef(kind="event", record_id=f"{other_id}:{other_sequence}"), cutoff),
        (event, cutoff - 1),
        (event, None),
    )
    for ref, evidence_revision in invalid:
        update = data.model_copy(update={"evidence_refs": (ref,), "evidence_revision": evidence_revision})
        with pytest.raises(ForgePilotError, match="invalid_skill_evidence"):
            store.skills.edit_draft(draft.draft_id, expected_revision=draft.revision, data=update)
        assert store.skills.get_draft(draft.draft_id) == draft


def test_read_and_review_recheck_private_evidence_after_draft_was_created(store, qt_draft):
    for mission_id in (TASK_ID, str(uuid4())):
        make_mission(store, mission_id)
        artifact = create_artifact(task_id=mission_id, producer="orchestrator.v1", producer_node="work",
                                   content=task_spec())
        store.artifacts.put(artifact)
        finish_mission(store, mission_id)
    # A persisted reference from a different Mission must be rejected even if
    # the artifact ID and digest agree with one another.
    draft = store.skills.create_draft(request_id="working", data=qt_draft, source_mission_id=TASK_ID)
    with store.transaction() as db:
        cutoff = db.execute("SELECT current_revision FROM task_graph_runs WHERE task_id=?", (TASK_ID,)).fetchone()[0]
        reference = SkillEvidenceRef(kind="artifact", record_id=artifact.artifact_id,
                                     content_sha256=artifact.content_sha256)
        forged = qt_draft.model_copy(update={"evidence_revision": cutoff, "evidence_refs": (reference,)})
        db.execute("UPDATE skill_drafts SET data_json=? WHERE draft_id=?", (forged.model_dump_json(), draft.draft_id))

    operations = (
        lambda: store.skills.get_draft(draft.draft_id),
        lambda: store.skills.list_draft_page(limit=1, offset=0),
        lambda: store.skills.create_draft(request_id="working", data=qt_draft, source_mission_id=TASK_ID),
        lambda: store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="review"),
    )
    for read in operations:
        with pytest.raises(ForgePilotError, match="invalid_skill_evidence"):
            read()
    assert store.skills.list_available() == []


def test_patch_replay_after_restart_and_competing_edit_preserves_saved_fields(tmp_path, qt_draft):
    path = tmp_path / "edits.sqlite3"
    changes = {"name": "已核对的排查经验", "developer_note": "保留这条现场观察"}
    with closing(SqliteEventStore(path)) as store:
        draft = store.skills.create_draft(request_id="working", data=qt_draft)
        saved = store.skills.patch_draft(draft.draft_id, expected_revision=draft.revision, changes=changes)
        assert saved.data.content == draft.data.content and saved.revision == draft.revision + 1
    with closing(SqliteEventStore(path)) as store:
        assert store.skills.patch_draft(draft.draft_id, expected_revision=draft.revision, changes=changes) == saved
        with pytest.raises(ForgePilotError, match="skill_revision_conflict"):
            store.skills.patch_draft(draft.draft_id, expected_revision=draft.revision, changes={"name": "另一窗口"})
        assert store.skills.get_draft(draft.draft_id) == saved


def test_double_publish_and_persistent_request_replay(tmp_path, qt_draft):
    path = tmp_path / "review.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        draft = store.skills.create_draft(request_id="create", data=qt_draft)
        ready = Barrier(2)

        def publish():
            ready.wait(timeout=10)
            return store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="publish")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish) for _ in range(2)]
            first, retry = [future.result(timeout=20) for future in futures]
        assert first == retry
        result = store.skills.get_draft(draft.draft_id)
        assert (result.result_skill_id, result.result_version) == (first.skill_id, 1)
        assert store.skills.list_available() == [first]
        with store.locked_connection() as db:
            assert db.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 1

    with closing(SqliteEventStore(path)) as store:
        assert store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="publish") == first
        second = store.skills.create_draft(request_id="another-draft", data=qt_draft)
        with pytest.raises(ForgePilotError, match="skill_request_conflict"):
            store.skills.publish_local(second.draft_id, expected_revision=second.revision, request_id="publish")
        assert store.skills.get_draft(second.draft_id).result_version is None
        assert store.skills.list_available() == [first]


def test_review_failure_rolls_back_version_pointer_and_draft_result(store, qt_draft):
    draft = store.skills.create_draft(request_id="create", data=qt_draft)
    original = store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="first")
    identity = store.skills.get_skill(original.skill_id)
    revision = store.skills.draft_from_version(original.skill_id, original.version, request_id="revise")
    changed_content = qt_draft.content.model_copy(update={"body": "先记录对应行和角色，再检查视图通知。"})
    edited = store.skills.edit_draft(revision.draft_id, expected_revision=revision.revision,
                                     data=qt_draft.model_copy(update={"content": changed_content}))
    with store.transaction() as db:
        db.execute("""CREATE TRIGGER simulate_review_failure BEFORE UPDATE ON skill_drafts
                      BEGIN SELECT RAISE(ABORT, 'injected review failure'); END""")

    with pytest.raises((sqlite3.IntegrityError, ForgePilotError)):
        store.skills.publish_local(edited.draft_id, expected_revision=edited.revision, request_id="second")
    assert store.skills.get_draft(edited.draft_id) == edited
    assert store.skills.get_skill(original.skill_id) == identity
    assert store.skills.list_available() == [original]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 1
        db.execute("DROP TRIGGER simulate_review_failure")

    reviewed = store.skills.publish_local(edited.draft_id, expected_revision=edited.revision, request_id="second")
    assert reviewed.version == original.version + 1
    assert reviewed.content_sha256 != original.content_sha256
    assert store.skills.get_version(original.skill_id, original.version) == original
    assert store.skills.list_available() == [reviewed]


def test_historic_content_and_review_result_survive_new_version_and_disable(store, qt_draft):
    skills = store.skills
    make_mission(store)
    finish_mission(store)
    with store.locked_connection() as db:
        event = db.execute("SELECT sequence,revision FROM graph_events WHERE task_id=? AND event_type='task.created'",
                           (TASK_ID,)).fetchone()
    evidence = SkillEvidenceRef(kind="event", record_id=f"{TASK_ID}:{event['sequence']}")
    data = qt_draft.model_copy(update={"evidence_refs": (evidence,), "evidence_revision": event["revision"]})
    draft = skills.create_draft(request_id="create", source_mission_id=TASK_ID, data=data)
    first = skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="first")
    assert first.content_sha256 == content_sha256(first.content)
    assert qt_draft.developer_note not in first.model_dump_json()
    assert evidence.record_id not in first.model_dump_json()
    assert skills.get_draft(draft.draft_id).data.evidence_refs == (evidence,)
    revision = skills.draft_from_version(first.skill_id, first.version, request_id="revise")
    assert (revision.base_skill_id, revision.base_version) == (first.skill_id, first.version)
    assert revision.data.content.body == first.content.body
    assert not revision.data.evidence_refs and not revision.data.developer_note
    new_body = first.content.body + "复现时核对过滤代理中的目标行。"
    changed = revision.data.content.model_copy(update={"body": new_body})
    revision = skills.edit_draft(revision.draft_id, expected_revision=revision.revision,
                                 data=revision.data.model_copy(update={"content": changed}))
    second = skills.publish_local(revision.draft_id, expected_revision=revision.revision, request_id="second")
    assert (second.skill_id, second.version, second.content.body) == (first.skill_id, 2, new_body)
    assert second.content_sha256 != first.content_sha256
    current = skills.get_skill(first.skill_id)
    assert current.enabled and current.current_version == 2
    historical = skills.draft_from_version(first.skill_id, first.version, request_id="from-history")
    assert historical.data.content.body == first.content.body
    with pytest.raises(ForgePilotError, match="skill_revision_conflict"):
        skills.publish_local(historical.draft_id, expected_revision=historical.revision, request_id="stale-base")
    assert skills.get_draft(historical.draft_id) == historical
    with pytest.raises(ForgePilotError, match="skill_revision_conflict"):
        skills.disable(first.skill_id, expected_revision=current.revision - 1)
    disabled = skills.disable(first.skill_id, expected_revision=current.revision)
    assert not disabled.enabled
    assert skills.list_available() == []
    assert skills.get_version(first.skill_id, 1) == first
    assert skills.get_version(first.skill_id, 2) == second
    assert skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="first") == first
    assert skills.list_available() == []


def test_two_database_instances_have_independent_libraries(tmp_path, store, qt_draft):
    draft = store.skills.create_draft(request_id="create", data=qt_draft)
    first = store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="publish")
    with closing(SqliteEventStore(tmp_path / "independent.sqlite3")) as other:
        assert other.skills.list_available() == [] and other.skills.list_drafts() == []
        with pytest.raises(ForgePilotError, match="skill_draft_not_found"):
            other.skills.get_draft(draft.draft_id)
        with pytest.raises(ForgePilotError, match="skill_not_found"):
            other.skills.get_version(first.skill_id, first.version)
        private_data = qt_draft.model_copy(update={"developer_note": "另一资料库的手记。", "evidence_refs": ()})
        other_draft = other.skills.create_draft(request_id="create", data=private_data)
        other_version = other.skills.publish_local(other_draft.draft_id, expected_revision=other_draft.revision,
                                                   request_id="publish")
        assert other_version.skill_id != first.skill_id
        assert other_version.content_sha256 == first.content_sha256
    assert store.skills.list_available() == [first]
