"""Mission storage operations retain atomic outbox and external payload recovery."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from hashlib import sha256
import sqlite3
from threading import Event

import pytest

from forgepilot.artifacts import (
    ArtifactIntegrityError, ArtifactStorageError, DuplicateArtifactError, canonical_json_bytes,
)
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import MissionEventStream
from forgepilot.orchestration import GraphDefinition, NodeDefinition, NodeType, validate_graph
from forgepilot.orchestration.store import GraphStore
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID
from test_mission_events import make_mission
from test_sqlite_artifact_store import _large_change, _plan, _spec


@pytest.fixture
def pending_mission(tmp_path):
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    graph = GraphDefinition("admission-boundary", 1, (
        NodeDefinition("work", NodeType.DETERMINISTIC, "work"),))
    GraphStore(store).create_run(TASK_ID, validate_graph(graph))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def admitted_mission(tmp_path):
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    make_mission(store)
    try:
        yield store
    finally:
        store.close()


def _admission():
    record = dict(workspace_id="workspace-admission", task_id=TASK_ID,
        repository_path="D:/offline/repository", worktree_path="D:/offline/worktree",
        base_ref="HEAD", head_commit="a" * 40, status="active")
    fields = dict(repository_path=record["repository_path"], request="Offline fixture",
        task_type="auto", orchestration_mode="multi_agent", base_ref="HEAD",
        toolchain_profile="auto", mcp_server_revisions=[])
    return record, fields


@pytest.mark.parametrize("event_type", ["task.created", "workspace.created"])
def test_admission_failure_rolls_back_workspace_revision_and_outbox(pending_mission, event_type):
    store = pending_mission
    record, fields = _admission()
    before = GraphStore(store).snapshot(TASK_ID)
    # Fail after the workspace insert and graph linkage, including after the
    # first event when the second event is selected.
    store._db.execute("""CREATE TEMP TRIGGER fail_admission BEFORE INSERT ON graph_events
        WHEN NEW.event_type='""" + event_type + """'
        BEGIN SELECT RAISE(ABORT,'injected admission failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected admission failure"):
        store.missions.admit_mission(TASK_ID, record, fields)
    assert not store._db.in_transaction
    assert GraphStore(store).snapshot(TASK_ID) == before
    assert store.missions.get_graph_run(TASK_ID)["workspace_id"] is None
    assert store._db.execute("SELECT count(*) FROM workspace_records").fetchone()[0] == 0
    assert [event.event_type for event in GraphStore(store).graph_events(TASK_ID)] == ["graph_created"]
    with pytest.raises(ForgePilotError, match="unknown_mission"):
        store.missions.metadata(TASK_ID)

    store._db.execute("DROP TRIGGER fail_admission")
    store.missions.admit_mission(TASK_ID, record, fields)
    assert store.missions.metadata(TASK_ID)["request"] == fields["request"]
    assert store.missions.get_graph_run(TASK_ID)["workspace_id"] == record["workspace_id"]
    assert [event.event_type for event in GraphStore(store).graph_events(TASK_ID)] == [
        "graph_created", "task.created", "workspace.created"]
    assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 2
    with pytest.raises(sqlite3.IntegrityError):
        store.missions.admit_mission(TASK_ID, record, fields)
    assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 2
    assert store._db.execute("SELECT count(*) FROM workspace_records").fetchone()[0] == 1


def test_subscription_cannot_observe_uncommitted_admission(pending_mission):
    store = pending_mission
    record, fields = _admission()
    entered, release, reading = Event(), Event(), Event()

    def gate():
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Admission gate was not released")
        return 0

    store._db.create_function("admission_gate", 0, gate)
    store._db.execute("""CREATE TEMP TRIGGER block_admission BEFORE INSERT ON graph_events
        WHEN NEW.event_type='workspace.created' BEGIN SELECT admission_gate(); END""")
    subscription = MissionEventStream(store).subscribe(TASK_ID)

    def read():
        reading.set()
        return subscription.read()

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            writer = workers.submit(store.missions.admit_mission, TASK_ID, record, fields)
            reader = None
            try:
                assert entered.wait(10)
                reader = workers.submit(read)
                assert reading.wait(10)
                with pytest.raises(TimeoutError):
                    reader.result(timeout=.05)
            finally:
                release.set()
            writer.result(timeout=10)
            events, finished = reader.result(timeout=10)
        assert [event.event_type for event in events] == [
            "task.graph_created", "task.created", "workspace.created"]
        assert [event.task_revision for event in events] == [1, 2, 3]
        assert not finished
    finally:
        release.set()
        subscription.close()


def _external_candidate(store):
    spec = _spec(TASK_ID)
    execution = _plan(TASK_ID, spec.artifact_id)
    store.artifacts.put(spec)
    store.artifacts.put(execution)
    artifact = _large_change(TASK_ID, execution.artifact_id)
    path = store.artifacts.root / TASK_ID / "artifacts" / f"{artifact.artifact_id}.json"
    return artifact, execution, path


@pytest.mark.parametrize("committed", [False, True], ids=["before_commit", "lost_commit_ack"])
def test_external_artifact_commit_outcome_controls_payload_cleanup(admitted_mission, monkeypatch, committed):
    store = admitted_mission
    artifact, parent, path = _external_candidate(store)
    before = GraphStore(store).snapshot(TASK_ID)
    transaction = store._transaction

    @contextmanager
    def fail_commit():
        with transaction():
            yield
            if not committed:
                raise sqlite3.OperationalError("injected before COMMIT")
        raise sqlite3.OperationalError("injected lost COMMIT acknowledgement")

    monkeypatch.setattr(store, "_transaction", fail_commit)
    with pytest.raises(ArtifactStorageError, match="metadata could not be saved"):
        store.artifacts.put(artifact)
    assert not store._db.in_transaction
    monkeypatch.setattr(store, "_transaction", transaction)
    if committed:
        payload = canonical_json_bytes(artifact)
        assert path.read_bytes() == payload
        assert store.artifacts.get(artifact.artifact_id) == artifact
        assert [(edge.parent_artifact_id, edge.child_artifact_id) for edge in
                store.artifacts.parents_of(artifact.artifact_id)] == [(parent.artifact_id, artifact.artifact_id)]
        row = store._db.execute("SELECT content_sha256,payload_sha256 FROM artifacts WHERE artifact_id=?",
            (artifact.artifact_id,)).fetchone()
        assert tuple(row) == (artifact.content_sha256, sha256(payload).hexdigest())
        assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 1
        assert GraphStore(store).graph_events(TASK_ID)[-1].event_type == "artifact.created"
        with pytest.raises(DuplicateArtifactError):
            store.artifacts.put(artifact)
        assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 1
    else:
        assert not path.exists()
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
        assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
            (artifact.artifact_id,)).fetchone()
        assert not store._db.execute("SELECT 1 FROM artifact_relations WHERE child_artifact_id=?",
            (artifact.artifact_id,)).fetchone()
        assert GraphStore(store).snapshot(TASK_ID) == before
        store.artifacts.put(artifact)
        assert store.artifacts.get(artifact.artifact_id) == artifact
        assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 1


@pytest.mark.parametrize("matches", [False, True], ids=["conflicting_orphan", "exact_orphan"])
def test_external_orphan_requires_explicit_exact_payload_retry(admitted_mission, matches):
    store = admitted_mission
    artifact, parent, path = _external_candidate(store)
    before = GraphStore(store).snapshot(TASK_ID)
    payload = canonical_json_bytes(artifact)
    orphan = payload if matches else payload[:-1] + bytes([payload[-1] ^ 1])
    store.artifacts._atomic_write(path, orphan)
    # Reconciliation never imports a file whose SQLite metadata did not commit.
    store.artifacts.reconcile_for_task(TASK_ID)
    assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
        (artifact.artifact_id,)).fetchone()
    if matches:
        store.artifacts.put(artifact)
        assert store.artifacts.get(artifact.artifact_id) == artifact
        assert store.artifacts.parents_of(artifact.artifact_id)[0].parent_artifact_id == parent.artifact_id
        assert GraphStore(store).snapshot(TASK_ID).revision == before.revision + 1
    else:
        with pytest.raises(ArtifactIntegrityError, match="SHA256"):
            store.artifacts.put(artifact)
        assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
            (artifact.artifact_id,)).fetchone()
        assert GraphStore(store).snapshot(TASK_ID) == before
    assert path.read_bytes() == orphan
