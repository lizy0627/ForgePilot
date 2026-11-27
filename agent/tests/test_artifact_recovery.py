"""Artifact durability boundaries used by graph checkpoint recovery."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from forgepilot.artifacts import (
    ArtifactIntegrityError, ArtifactStorageError, DuplicateArtifactError,
    canonical_json_bytes,
)
from forgepilot.sqlite_store import SqliteEventStore
from test_sqlite_artifact_store import _large_change, _plan, _spec


def _artifacts(store, task_id):
    spec = _spec(task_id)
    execution_plan = _plan(task_id, spec.artifact_id)
    change = _large_change(task_id, execution_plan.artifact_id)
    for artifact in (spec, execution_plan, change):
        store.artifacts.put(artifact)
    return spec, execution_plan, change


def test_artifact_payload_committed_without_relations_reconciles_repeatedly(tmp_path):
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=tmp_path / "tasks")
    task_id = str(uuid4())
    try:
        spec, execution_plan, change = _artifacts(store, task_id)
        store._db.execute("DELETE FROM artifact_relations")
        for _ in range(3):
            recovered = store.artifacts.reconcile_for_task(task_id)
            assert {artifact.artifact_id for artifact in recovered} == {
                spec.artifact_id, execution_plan.artifact_id, change.artifact_id,
            }
            assert len(store.artifacts.parents_of(execution_plan.artifact_id)) == 1
            assert len(store.artifacts.parents_of(change.artifact_id)) == 1
            assert store._db.execute("SELECT count(*) FROM artifact_relations").fetchone()[0] == 2
    finally:
        store.close()


def test_sqlite_failure_rolls_back_the_entire_relationship_repair(tmp_path):
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=tmp_path / "tasks")
    task_id = str(uuid4())
    try:
        _, _, change = _artifacts(store, task_id)
        store._db.execute("DELETE FROM artifact_relations")
        # The first INSERT succeeds; the second fails, so partial repair must roll back.
        store._db.execute(f"""CREATE TRIGGER fail_relation BEFORE INSERT ON artifact_relations
            WHEN NEW.child_artifact_id='{change.artifact_id}'
            BEGIN SELECT RAISE(ABORT, 'injected SQLite relation failure'); END""")
        with pytest.raises(ArtifactStorageError, match="reconciled"):
            store.artifacts.reconcile_for_task(task_id)
        assert store._db.execute("SELECT count(*) FROM artifact_relations").fetchone()[0] == 0
        store._db.execute("DROP TRIGGER fail_relation")
        store.artifacts.reconcile_for_task(task_id)
        assert store._db.execute("SELECT count(*) FROM artifact_relations").fetchone()[0] == 2
    finally:
        store.close()


@pytest.mark.parametrize("external", [False, True], ids=["inline", "external"])
def test_tampering_blocks_reconciliation_before_any_relationship_is_repaired(tmp_path, external):
    root = tmp_path / "tasks"
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=root)
    task_id = str(uuid4())
    try:
        spec, _, change = _artifacts(store, task_id)
        store._db.execute("DELETE FROM artifact_relations")
        if external:
            target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
            payload = target.read_bytes()
            target.write_bytes(payload[:-1] + b" ")
        else:
            store._db.execute("UPDATE artifacts SET inline_json=? WHERE artifact_id=?",
                              (b"{}", spec.artifact_id))
        with pytest.raises(ArtifactIntegrityError, match="SHA256"):
            store.artifacts.reconcile_for_task(task_id)
        assert store._db.execute("SELECT count(*) FROM artifact_relations").fetchone()[0] == 0
    finally:
        store.close()


def test_hard_process_exit_after_external_rename_allows_only_an_exact_explicit_retry(tmp_path):
    database, root = tmp_path / "artifacts.sqlite3", tmp_path / "tasks"
    task_id = str(uuid4())
    store = SqliteEventStore(database, artifact_root=root)
    spec = _spec(task_id)
    execution_plan = _plan(task_id, spec.artifact_id)
    change = _large_change(task_id, execution_plan.artifact_id)
    store.artifacts.put(spec)
    store.artifacts.put(execution_plan)
    store.close()
    envelope = tmp_path / "change.json"
    envelope.write_bytes(canonical_json_bytes(change))
    child = subprocess.run(
        [sys.executable, "-c", """
import os
from pathlib import Path
import sys
from forgepilot.artifacts import validate_artifact
from forgepilot.sqlite_store import SqliteEventStore
store = SqliteEventStore(sys.argv[1], artifact_root=sys.argv[2])
write = store.artifacts._atomic_write
def crash_after_rename(target, payload):
    write(target, payload)
    os._exit(73)
store.artifacts._atomic_write = crash_after_rename
store.artifacts.put(validate_artifact(Path(sys.argv[3]).read_bytes()))
""", str(database), str(root), str(envelope)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
    assert target.read_bytes() == canonical_json_bytes(change)
    store = SqliteEventStore(database, artifact_root=root)
    try:
        recovered = store.artifacts.reconcile_for_task(task_id)
        assert change.artifact_id not in {artifact.artifact_id for artifact in recovered}
        store.artifacts.put(change)
        assert store.artifacts.get(change.artifact_id) == change
        assert len(store.artifacts.parents_of(change.artifact_id)) == 1
        with pytest.raises(DuplicateArtifactError):
            store.artifacts.put(change)
        assert target.read_bytes() == canonical_json_bytes(change)
    finally:
        store.close()


def test_external_orphan_with_different_bytes_is_never_replaced(tmp_path):
    root = tmp_path / "tasks"
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=root)
    task_id = str(uuid4())
    try:
        spec = _spec(task_id)
        execution_plan = _plan(task_id, spec.artifact_id)
        store.artifacts.put(spec)
        store.artifacts.put(execution_plan)
        change = _large_change(task_id, execution_plan.artifact_id)
        target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
        target.parent.mkdir(parents=True)
        corrupted = canonical_json_bytes(change)[:-1] + b" "
        target.write_bytes(corrupted)
        for _ in range(2):
            with pytest.raises(ArtifactIntegrityError, match="SHA256"):
                store.artifacts.put(change)
            assert target.read_bytes() == corrupted
            assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
                                         (change.artifact_id,)).fetchone()
    finally:
        store.close()


def test_sqlite_failure_during_orphan_adoption_preserves_bytes_for_a_later_retry(tmp_path):
    root = tmp_path / "tasks"
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=root)
    task_id = str(uuid4())
    try:
        spec = _spec(task_id)
        execution_plan = _plan(task_id, spec.artifact_id)
        store.artifacts.put(spec)
        store.artifacts.put(execution_plan)
        change = _large_change(task_id, execution_plan.artifact_id)
        target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
        target.parent.mkdir(parents=True)
        payload = canonical_json_bytes(change)
        target.write_bytes(payload)
        store._db.execute("""CREATE TRIGGER fail_artifact BEFORE INSERT ON artifacts
            WHEN NEW.artifact_type='ChangeSet'
            BEGIN SELECT RAISE(ABORT, 'injected SQLite Artifact failure'); END""")
        with pytest.raises(ArtifactStorageError, match="metadata"):
            store.artifacts.put(change)
        assert target.read_bytes() == payload
        assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
                                     (change.artifact_id,)).fetchone()
        store._db.execute("DROP TRIGGER fail_artifact")
        store.artifacts.put(change)
        assert store.artifacts.get(change.artifact_id) == change
    finally:
        store.close()


def test_external_artifact_survives_successful_commit_that_reports_failure(tmp_path, monkeypatch):
    database, root = tmp_path / "uncertain.sqlite3", tmp_path / "tasks"
    store = SqliteEventStore(database, artifact_root=root)
    task_id = str(uuid4())
    spec = _spec(task_id)
    execution_plan = _plan(task_id, spec.artifact_id)
    change = _large_change(task_id, execution_plan.artifact_id)
    store.artifacts.put(spec)
    store.artifacts.put(execution_plan)
    real_transaction = store._transaction

    @contextmanager
    def commit_then_report_failure():
        with real_transaction():
            yield
        raise sqlite3.OperationalError("injected error after durable COMMIT")

    monkeypatch.setattr(store, "_transaction", commit_then_report_failure)
    try:
        with pytest.raises(ArtifactStorageError, match="metadata"):
            store.artifacts.put(change)
        target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
        payload = canonical_json_bytes(change)
        assert target.read_bytes() == payload
        assert store.artifacts.get(change.artifact_id) == change
    finally:
        store.close()
    reopened = SqliteEventStore(database, artifact_root=root)
    try:
        assert reopened.artifacts.get(change.artifact_id) == change
        assert len(reopened.artifacts.parents_of(change.artifact_id)) == 1
        stamp = target.stat().st_mtime_ns
        with pytest.raises(DuplicateArtifactError):
            reopened.artifacts.put(change)
        assert target.stat().st_mtime_ns == stamp and target.read_bytes() == payload
    finally:
        reopened.close()


def test_external_artifact_is_retained_when_transaction_outcome_is_unknown(tmp_path, monkeypatch):
    database, root = tmp_path / "unknown.sqlite3", tmp_path / "tasks"
    store = SqliteEventStore(database, artifact_root=root)
    task_id = str(uuid4())
    spec = _spec(task_id)
    execution_plan = _plan(task_id, spec.artifact_id)
    change = _large_change(task_id, execution_plan.artifact_id)
    store.artifacts.put(spec)
    store.artifacts.put(execution_plan)

    @contextmanager
    def uncertain_open_transaction():
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            yield
            raise sqlite3.OperationalError("injected COMMIT outcome unknown")

    monkeypatch.setattr(store, "_transaction", uncertain_open_transaction)
    try:
        with pytest.raises(ArtifactStorageError):
            store.artifacts.put(change)
        assert store._db.in_transaction
        target = root / task_id / "artifacts" / f"{change.artifact_id}.json"
        payload = canonical_json_bytes(change)
        assert target.read_bytes() == payload
    finally:
        store.close()  # SQLite rolls the uncertain transaction back on close.
    reopened = SqliteEventStore(database, artifact_root=root)
    try:
        assert not reopened._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
                                        (change.artifact_id,)).fetchone()
        assert target.read_bytes() == payload
        reopened.artifacts.put(change)
        assert reopened.artifacts.get(change.artifact_id) == change
    finally:
        reopened.close()


@pytest.mark.parametrize("committed", [True, False], ids=["lost_commit_ack", "open_transaction"])
def test_external_package_survives_uncertain_commit_and_restart(tmp_path, monkeypatch, committed):
    monkeypatch.setattr("forgepilot.artifacts.sqlite_store.INLINE_LIMIT_BYTES", 0)
    database, root = tmp_path / "package.sqlite3", tmp_path / "tasks"
    store = SqliteEventStore(database, artifact_root=root)
    task_id = str(uuid4())
    spec = _spec(task_id)
    plan = _plan(task_id, spec.artifact_id)

    @contextmanager
    def fail_at_commit():
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            yield
            if committed:
                store._db.execute("COMMIT")
            raise sqlite3.OperationalError("injected uncertain COMMIT")

    monkeypatch.setattr(store, "_transaction", fail_at_commit)
    try:
        with pytest.raises(sqlite3.OperationalError, match="uncertain COMMIT"):
            with store.artifacts.write_transaction() as register:
                register(spec)
                register(plan)
        assert store._db.in_transaction == (not committed)
    finally:
        store.close()

    reopened = SqliteEventStore(database, artifact_root=root)
    try:
        for artifact in (spec, plan):
            path = root / task_id / "artifacts" / f"{artifact.artifact_id}.json"
            assert path.read_bytes() == canonical_json_bytes(artifact)
        if not committed:
            assert reopened.artifacts.reconcile_for_task(task_id) == ()

            def unexpected_write(target, payload):
                pytest.fail(f"Retry rewrote existing payload {target.name}")

            monkeypatch.setattr(reopened.artifacts, "_atomic_write", unexpected_write)
            with reopened.artifacts.write_transaction() as register:
                register(spec)
                register(plan)
        assert reopened.artifacts.get(spec.artifact_id) == spec
        assert reopened.artifacts.get(plan.artifact_id) == plan
        assert [edge.parent_artifact_id for edge in reopened.artifacts.parents_of(plan.artifact_id)] == [
            spec.artifact_id]
    finally:
        reopened.close()


def test_reconciliation_rejects_undeclared_and_cross_task_relations(tmp_path):
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=tmp_path / "tasks")
    task_id = str(uuid4())
    try:
        spec, execution_plan, change = _artifacts(store, task_id)
        foreign = _spec(str(uuid4()))
        store.artifacts.put(foreign)
        for parent_id in (spec.artifact_id, foreign.artifact_id):
            store._db.execute("DELETE FROM artifact_relations WHERE child_artifact_id=?",
                              (change.artifact_id,))
            store._db.execute("""INSERT INTO artifact_relations VALUES(?,?,?,?)""",
                              (parent_id, change.artifact_id, "derived_from", change.created_at))
            with pytest.raises(ArtifactIntegrityError, match="immutable envelope"):
                store.artifacts.reconcile_for_task(task_id)
            assert store._db.execute("""SELECT count(*) FROM artifact_relations
                WHERE parent_artifact_id=? AND child_artifact_id=?""",
                (execution_plan.artifact_id, change.artifact_id)).fetchone()[0] == 0
    finally:
        store.close()


def test_revision_payload_and_revision_relation_commit_together(tmp_path):
    store = SqliteEventStore(tmp_path / "artifacts.sqlite3", artifact_root=tmp_path / "tasks")
    task_id = str(uuid4())
    try:
        spec = _spec(task_id)
        store.artifacts.put(spec)
        revision_id = str(uuid4())
        store._db.execute("""CREATE TRIGGER fail_revision BEFORE INSERT ON artifact_relations
            WHEN NEW.relation='revision_of'
            BEGIN SELECT RAISE(ABORT, 'injected revision failure'); END""")
        with pytest.raises(ArtifactStorageError):
            store.artifacts.revise(
                spec.artifact_id, content=spec.content, producer="orchestrator.v1",
                producer_node="draft-task", artifact_id_override=revision_id,
            )
        assert not store._db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?",
                                     (revision_id,)).fetchone()
        store._db.execute("DROP TRIGGER fail_revision")
        revision = store.artifacts.revise(
            spec.artifact_id, content=spec.content, producer="orchestrator.v1",
            producer_node="draft-task", artifact_id_override=revision_id,
        )
        store.artifacts.reconcile_for_task(task_id)
        assert store.artifacts.parents_of(revision.artifact_id)[0].relation == "revision_of"
    finally:
        store.close()
