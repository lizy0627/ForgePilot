"""Checkpoint durability, schema migration, and SQLite fault injection."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.checkpoint import (
    CheckpointIntegrityError, CheckpointStore, checkpoint_id_for,
)
from forgepilot.orchestration.graph import validate_graph
from forgepilot.orchestration.models import (
    GraphDefinition, NodeDefinition, NodeResult, NodeStatus, NodeType, idempotency_key,
)
from forgepilot.orchestration.policies import DefaultFailurePolicy
from forgepilot.orchestration.store import GraphStore
from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION, SchemaVersion
from forgepilot.sqlite_store import SqliteEventStore
import forgepilot.sqlite_store as sqlite_store_module


def _attempt(store, task_id="task-checkpoint", node_id="implement", attempt=1):
    if not store._db.execute(
            "SELECT 1 FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone():
        store.missions.create_graph_run(task_id=task_id, graph_id="g", graph_version=1)
    store.missions.start_node_attempt(
        task_id=task_id, node_id=node_id, attempt=attempt,
        node_type="agent", status="running", idempotency_key="a" * 64,
        input_hash="b" * 64,
    )


def _create(checkpoints, *, task_id="task-checkpoint", node_id="implement", attempt=1, **kwargs):
    return checkpoints.create(task_id, node_id, attempt, "implementation",
                              NodeResult(NodeStatus.SUCCEEDED), **kwargs)


def _v9_database(path):
    store = SqliteEventStore(path)
    _attempt(store)
    store.missions.record_checkpoint(
        checkpoint_id="legacy", task_id="task-checkpoint", checkpoint_type="legacy",
        status="completed", workspace_id="worktree", commit_hash="c" * 40,
    )
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE writer_scope_expansions")
        db.execute("DROP TABLE checkpoint_intents")
        db.execute("ALTER TABLE task_graph_runs DROP COLUMN recovery_required")
        db.execute("ALTER TABLE task_graph_runs DROP COLUMN recovery_plan_json")
        db.execute("ALTER TABLE task_graph_runs DROP COLUMN definition_json")
        db.execute("ALTER TABLE task_graph_runs DROP COLUMN policy_hash")
        db.execute("ALTER TABLE task_node_attempts DROP COLUMN input_artifacts_json")
        db.execute("ALTER TABLE checkpoints RENAME TO schema10_checkpoints")
        db.execute("DROP INDEX checkpoints_task_created")
        db.execute("DROP INDEX checkpoints_task_ordinal")
        db.execute("""CREATE TABLE checkpoints(
            checkpoint_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,checkpoint_type TEXT NOT NULL,
            status TEXT NOT NULL,workspace_id TEXT,commit_hash TEXT,artifact_id TEXT,
            created_at TEXT NOT NULL,error_code TEXT)""")
        db.execute("""INSERT INTO checkpoints SELECT checkpoint_id,task_id,checkpoint_type,status,
            workspace_id,commit_hash,artifact_id,created_at,error_code FROM schema10_checkpoints""")
        db.execute("DROP TABLE schema10_checkpoints")
        db.execute("CREATE INDEX checkpoints_task_created ON checkpoints(task_id,created_at,checkpoint_id)")
        db.execute("PRAGMA user_version=9")


def test_v9_migration_preserves_legacy_records_backups_and_gates_between_nodes(tmp_path):
    path = tmp_path / "migration.sqlite3"
    _v9_database(path)
    store = SqliteEventStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == int(CURRENT_SCHEMA_VERSION)
        legacy = store._db.execute("SELECT * FROM checkpoints WHERE checkpoint_id='legacy'").fetchone()
        assert legacy["commit_hash"] == "c" * 40 and legacy["node_id"] is None
        assert CheckpointStore(store).latest("task-checkpoint") is None
        assert store.missions.get_graph_run("task-checkpoint")["recovery_required"] == 1
        assert not store._db.execute("SELECT 1 FROM graph_events").fetchone()
        backup = store.migration_backup_path
        record = _create(CheckpointStore(store))
        assert record.ordinal > legacy["ordinal"]
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("SELECT checkpoint_id FROM checkpoints").fetchone()[0] == "legacy"


def test_v10_migration_failure_rolls_back_schema_and_preserves_backup(tmp_path, monkeypatch):
    path = tmp_path / "failed-migration.sqlite3"
    _v9_database(path)
    original = sqlite_store_module.create_graph_checkpoint_schema

    def fail_after_migration(db):
        original(db)
        raise sqlite3.OperationalError("injected SQLite migration failure")

    monkeypatch.setattr(sqlite_store_module, "create_graph_checkpoint_schema", fail_after_migration)
    with pytest.raises(ForgePilotError, match="storage_unavailable"):
        SqliteEventStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert "recovery_required" not in {row[1] for row in db.execute("PRAGMA table_info(task_graph_runs)")}
        assert db.execute("SELECT checkpoint_id FROM checkpoints").fetchone()[0] == "legacy"
        assert "ordinal" not in {row[1] for row in db.execute("PRAGMA table_info(checkpoints)")}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='checkpoint_intents'").fetchone()
    assert len(list(tmp_path.glob("*.v9-backup-*.sqlite3"))) == 1


def test_checkpoint_prepared_before_state_update_is_durable_immutable_and_idempotent(tmp_path):
    path = tmp_path / "prepared.sqlite3"
    store = SqliteEventStore(path)
    _attempt(store)
    checkpoints = CheckpointStore(store)
    record = _create(checkpoints, workspace_id="workspace", head_commit="c" * 40,
                     file_hashes={"src/main.cpp": "d" * 64, "deleted.cpp": None})
    assert record.checkpoint_id == checkpoint_id_for("task-checkpoint", "implement", 1)
    assert len(record.data_sha256) == 64
    assert checkpoints.latest(record.task_id) == record
    assert checkpoints.for_attempt(record.task_id, record.node_id, record.attempt) == record
    assert checkpoints.list_for_task(record.task_id) == (record,)
    assert _create(checkpoints, workspace_id="workspace", head_commit="c" * 40,
                   file_hashes={"deleted.cpp": None, "src/main.cpp": "d" * 64}) == record
    checkpoints.verify(record)
    with pytest.raises(TypeError):
        record.file_hashes["unwanted.cpp"] = "d" * 64
    with pytest.raises(ForgePilotError, match="immutable"):
        _create(checkpoints, workspace_id="workspace", head_commit="e" * 40)
    assert store.missions.get_node_attempt(record.task_id, record.node_id, 1)["status"] == "running"
    assert not store._db.execute("SELECT 1 FROM graph_node_results").fetchone()
    assert not store._db.execute("SELECT 1 FROM graph_events").fetchone()
    store.close()

    reopened = SqliteEventStore(path)
    try:
        assert CheckpointStore(reopened).for_attempt(record.task_id, record.node_id, 1) == record
        CheckpointStore(reopened).verify(record)
        assert reopened.missions.get_graph_run(record.task_id)["recovery_required"] == 1
    finally:
        reopened.close()


def test_checkpoint_ordinal_is_monotonic_after_deletion_and_ignores_clock_order():
    store = SqliteEventStore()
    try:
        checkpoints = CheckpointStore(store)
        _attempt(store)
        first = _create(checkpoints)
        _attempt(store, attempt=2)
        second = _create(checkpoints, attempt=2)
        # Created timestamps are covered by the hash; changing one cannot select
        # a false latest checkpoint or silently turn it into a verified snapshot.
        assert second.ordinal > first.ordinal and checkpoints.latest(first.task_id) == second
        store._db.execute("DELETE FROM checkpoints WHERE ordinal=?", (second.ordinal,))
        _attempt(store, attempt=3)
        third = _create(checkpoints, attempt=3)
        assert third.ordinal > second.ordinal
        assert checkpoints.list_for_task(first.task_id) == (first, third)
    finally:
        store.close()


@pytest.mark.parametrize("column,value", [
    ("result_json", '{"status":"succeeded","decision_summary":"tampered"}'),
    ("file_hashes_json", '{"src.cpp":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}'),
    ("commit_hash", "e" * 40), ("input_artifacts_json", "[]"),
    ("created_at", "2000-01-01T00:00:00Z"), ("status", "failed"),
])
def test_checkpoint_data_tampering_blocks_reads_and_verification(column, value):
    store = SqliteEventStore()
    try:
        _attempt(store)
        checkpoints = CheckpointStore(store)
        record = _create(checkpoints, input_artifacts=(("input", "c" * 64),))
        store._db.execute(f"UPDATE checkpoints SET {column}=? WHERE checkpoint_id=?",
                          (value, record.checkpoint_id))
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.latest(record.task_id)
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.verify(record)
    finally:
        store.close()


def test_checkpoint_snapshot_cannot_be_replaced_by_a_changed_in_memory_record():
    store = SqliteEventStore()
    try:
        _attempt(store)
        checkpoints = CheckpointStore(store)
        record = _create(checkpoints)
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.verify(replace(record, head_commit="e" * 40))
    finally:
        store.close()


def test_checkpoint_readback_error_rolls_back_the_insert():
    store = SqliteEventStore()
    try:
        _attempt(store)
        checkpoints = CheckpointStore(store)
        with store.transaction() as db:
            db.execute("""CREATE TEMP TRIGGER corrupt_checkpoint AFTER INSERT ON checkpoints
                BEGIN UPDATE checkpoints SET created_at='invalid' WHERE ordinal=NEW.ordinal; END""")
        with pytest.raises(CheckpointIntegrityError):
            _create(checkpoints)
        assert checkpoints.list_for_task("task-checkpoint") == ()
        with store.transaction() as db:
            db.execute("DROP TRIGGER corrupt_checkpoint")
        assert _create(checkpoints).ordinal == 1
    finally:
        store.close()


def test_sqlite_insert_failure_and_ambiguous_commit_converge_on_one_checkpoint(monkeypatch):
    store = SqliteEventStore()
    try:
        _attempt(store)
        checkpoints = CheckpointStore(store)
        store._db.execute("""CREATE TEMP TRIGGER fail_checkpoint_insert BEFORE INSERT ON checkpoints
            BEGIN SELECT RAISE(ABORT, 'injected SQLite write failure'); END""")
        with pytest.raises(ForgePilotError, match="checkpoint_storage"):
            _create(checkpoints)
        assert checkpoints.list_for_task("task-checkpoint") == ()
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store._db.execute("DROP TRIGGER fail_checkpoint_insert")

        real_transaction = store._transaction

        @contextmanager
        def fail_after_commit():
            with real_transaction():
                yield
            raise sqlite3.OperationalError("injected error after durable COMMIT")

        monkeypatch.setattr(store, "_transaction", fail_after_commit)
        with pytest.raises(ForgePilotError, match="checkpoint_storage"):
            _create(checkpoints)
        monkeypatch.setattr(store, "_transaction", real_transaction)
        durable = checkpoints.latest("task-checkpoint")
        assert durable is not None and _create(checkpoints) == durable
        assert checkpoints.list_for_task("task-checkpoint") == (durable,)
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [
    ("file_hashes", {"../main.cpp": "d" * 64}),
    ("file_hashes", {"C:/main.cpp": "d" * 64}),
    ("file_hashes", {"src\\main.cpp": "d" * 64}),
    ("file_hashes", {"src/main.cpp": "invalid"}),
    ("head_commit", "unknown HEAD"),
    ("output_artifacts", (("wrong-output", "d" * 64),)),
])
def test_checkpoint_rejects_unverifiable_snapshot_inputs(field, value):
    store = SqliteEventStore()
    try:
        _attempt(store)
        with pytest.raises(ValueError):
            _create(CheckpointStore(store), **{field: value})
        assert not store._db.execute("SELECT 1 FROM checkpoints").fetchone()
    finally:
        store.close()


def test_checkpoint_rejects_unowned_attempt_and_changed_idempotency_key():
    store = SqliteEventStore()
    try:
        checkpoints = CheckpointStore(store)
        with pytest.raises(ForgePilotError, match="checkpoint_attempt"):
            _create(checkpoints)
        _attempt(store)
        with pytest.raises(ForgePilotError, match="checkpoint_conflict"):
            _create(checkpoints, idempotency_key="f" * 64)
        with pytest.raises(ValueError, match="successful"):
            checkpoints.create("task-checkpoint", "implement", 1, "repair", NodeResult(NodeStatus.CANCELLED))
    finally:
        store.close()


def test_checkpoint_intent_is_durable_immutable_redacted_and_independent_of_checkpoint(tmp_path):
    path = tmp_path / "intent.sqlite3"
    store = SqliteEventStore(path)
    _attempt(store)
    checkpoints = CheckpointStore(store)
    payload = {"boundary": "implementation", "result": NodeResult(NodeStatus.SUCCEEDED).to_dict(),
               "expected": {"head": "b" * 40, "files": {"main.cpp": "c" * 64}},
               "api_key": "sk-this-is-a-fake-secret-value"}
    intent = checkpoints.save_intent("task-checkpoint", "implement", 1, payload)
    assert intent["checkpoint_id"] == checkpoint_id_for("task-checkpoint", "implement", 1)
    assert "sk-this-is-a-fake-secret-value" not in str(intent)
    assert checkpoints.intent_for_attempt("task-checkpoint", "implement", 1) == intent
    assert checkpoints.intents_for_task("task-checkpoint") == (intent,)
    assert checkpoints.save_intent("task-checkpoint", "implement", 1, payload) == intent
    with pytest.raises(ForgePilotError, match="immutable"):
        checkpoints.save_intent("task-checkpoint", "implement", 1, {**payload, "boundary": "repair"})
    assert checkpoints.for_attempt("task-checkpoint", "implement", 1) is None
    store.close()

    reopened = SqliteEventStore(path)
    try:
        durable = CheckpointStore(reopened)
        assert durable.intent_for_attempt("task-checkpoint", "implement", 1) == intent
        assert durable.intents_for_task("task-checkpoint") == (intent,)
        _create(durable)
        assert durable.intent_for_attempt("task-checkpoint", "implement", 1) == intent
    finally:
        reopened.close()


@pytest.mark.parametrize("column,value", [
    ("payload_json", '{"boundary":"tampered"}'),
    ("data_sha256", "e" * 64),
    ("node_id", "tampered-node"),
])
def test_checkpoint_intent_tampering_blocks_reads_and_resave(column, value):
    store = SqliteEventStore()
    try:
        _attempt(store)
        _attempt(store, node_id="tampered-node")
        checkpoints = CheckpointStore(store)
        intent = checkpoints.save_intent("task-checkpoint", "implement", 1, {"boundary": "implementation"})
        store._db.execute(f"UPDATE checkpoint_intents SET {column}=? WHERE checkpoint_id=?",
                          (value, intent["checkpoint_id"]))
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.intents_for_task("task-checkpoint")
        node_id = "tampered-node" if column == "node_id" else "implement"
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.intent_for_attempt("task-checkpoint", node_id, 1)
        with pytest.raises(CheckpointIntegrityError):
            checkpoints.save_intent("task-checkpoint", node_id, 1, {"boundary": "implementation"})
    finally:
        store.close()


def test_checkpoint_intent_sqlite_failures_converge_without_duplicate_effects(monkeypatch):
    store = SqliteEventStore()
    try:
        _attempt(store)
        checkpoints = CheckpointStore(store)
        payload = {"boundary": "implementation", "expected": {"head": "b" * 40}}
        store._db.execute("""CREATE TEMP TRIGGER fail_intent BEFORE INSERT ON checkpoint_intents
            BEGIN SELECT RAISE(ABORT, 'injected intent SQLite failure'); END""")
        with pytest.raises(ForgePilotError, match="checkpoint_storage"):
            checkpoints.save_intent("task-checkpoint", "implement", 1, payload)
        assert checkpoints.intents_for_task("task-checkpoint") == ()
        store._db.execute("DROP TRIGGER fail_intent")
        original = store._transaction

        @contextmanager
        def fail_after_commit():
            with original():
                yield
            raise sqlite3.OperationalError("injected intent failure after durable COMMIT")

        monkeypatch.setattr(store, "_transaction", fail_after_commit)
        with pytest.raises(ForgePilotError, match="checkpoint_storage"):
            checkpoints.save_intent("task-checkpoint", "implement", 1, payload)
        monkeypatch.setattr(store, "_transaction", original)
        intent = checkpoints.intent_for_attempt("task-checkpoint", "implement", 1)
        assert intent is not None
        assert checkpoints.save_intent("task-checkpoint", "implement", 1, payload) == intent
        assert checkpoints.intents_for_task("task-checkpoint") == (intent,)
        assert checkpoints.latest("task-checkpoint") is None
    finally:
        store.close()


def test_checkpoint_intent_requires_existing_attempt_and_canonical_json():
    store = SqliteEventStore()
    try:
        checkpoints = CheckpointStore(store)
        with pytest.raises(ForgePilotError, match="checkpoint_attempt"):
            checkpoints.save_intent("task-checkpoint", "implement", 1, {})
        _attempt(store)
        for payload in ([], {"not_json": object()}, {"not_finite": float("nan")}):
            with pytest.raises((ValueError, TypeError)):
                checkpoints.save_intent("task-checkpoint", "implement", 1, payload)
        assert checkpoints.intents_for_task("task-checkpoint") == ()
    finally:
        store.close()


def test_startup_inspects_persisted_definition_and_publishes_plan_without_execution(tmp_path):
    path = tmp_path / "startup-plan.sqlite3"
    store = SqliteEventStore(path)
    graph = GraphDefinition("startup", 1, (
        NodeDefinition("analyze", NodeType.AGENT, "analyst"),
    ))
    repository = GraphStore(store)
    created = repository.create_run("task-startup", validate_graph(graph))
    ready = repository.transition("task-startup", "analyze", expected_revision=created.revision,
                                  source=NodeStatus.PENDING, target=NodeStatus.READY)
    repository.transition("task-startup", "analyze", expected_revision=ready.revision,
        source=NodeStatus.READY, target=NodeStatus.RUNNING, attempt=1,
        idempotency_key=idempotency_key(task_id="task-startup", node_id="analyze", attempt=1,
            input_artifact_hashes=(), agent_definition_hash=graph.nodes[0].effective_agent_definition_hash,
            policy_hash=DefaultFailurePolicy().definition_hash), input_hash=sha256(b"[]").hexdigest())
    store.close()

    reopened = SqliteEventStore(path)
    try:
        run = reopened.missions.get_graph_run("task-startup")
        plan = json.loads(run["recovery_plan_json"])
        assert run["recovery_required"] == 1
        assert run["status"] == "interrupted"
        assert plan["blocked_reason"] is None and plan["token"]
        assert plan["requires_confirmation"] is True
        assert plan["actions"] == [{"node_id": "analyze", "action": "retry_frozen"}]
        assert plan["checks"] == ["sqlite", "worktree_record", "head", "checkpoint", "file_hash", "artifact_hash"]
        assert GraphStore(reopened).snapshot("task-startup").nodes[0].attempt == 1
        assert not reopened._db.execute("SELECT 1 FROM graph_events WHERE event_type='recovery_confirmed'").fetchone()
    finally:
        reopened.close()


@pytest.mark.parametrize("missing,reason", [
    ("definition_json", "legacy_graph_definition_missing"),
    ("policy_hash", "legacy_graph_policy_missing"),
])
def test_startup_missing_legacy_definition_or_policy_is_durably_blocked(tmp_path, missing, reason):
    path = tmp_path / "legacy-definition.sqlite3"
    store = SqliteEventStore(path)
    graph = GraphDefinition("legacy", 1, (NodeDefinition("work", NodeType.AGENT, "analyst"),))
    GraphStore(store).create_run("task-legacy", validate_graph(graph))
    store._db.execute(f"UPDATE task_graph_runs SET {missing}=NULL WHERE task_id='task-legacy'")
    store.close()
    reopened = SqliteEventStore(path)
    try:
        run = reopened.missions.get_graph_run("task-legacy")
        plan = json.loads(run["recovery_plan_json"])
        assert run["recovery_required"] == 1 and run["status"] == "pending"
        assert plan["blocked_reason"] == reason and not plan["token"]
        assert plan["actions"] == [] and plan["requires_confirmation"] is True
    finally:
        reopened.close()


@pytest.mark.parametrize("value", ["not JSON", '{"graph_id":"tampered","version":1,"nodes":[]}'])
def test_startup_changed_or_invalid_definition_fails_closed(tmp_path, value):
    path = tmp_path / "invalid-definition.sqlite3"
    store = SqliteEventStore(path)
    graph = GraphDefinition("intact", 1, (NodeDefinition("work", NodeType.AGENT, "analyst"),))
    GraphStore(store).create_run("task-invalid", validate_graph(graph))
    store._db.execute("UPDATE task_graph_runs SET definition_json=? WHERE task_id='task-invalid'", (value,))
    store.close()
    reopened = SqliteEventStore(path)
    try:
        run = reopened.missions.get_graph_run("task-invalid")
        plan = json.loads(run["recovery_plan_json"])
        assert plan["blocked_reason"] == "recovery_integrity" and not plan["token"]
        assert run["recovery_required"] == 1
    finally:
        reopened.close()


def test_startup_workspace_requires_injected_backend_and_only_inspects(tmp_path):
    from forgepilot.orchestration import GraphRunner
    from forgepilot.orchestration.node_handlers import HandlerRegistry
    from forgepilot.orchestration.workspace import StrictFakeWorkspaceBackend, WorkspaceSnapshot

    class ProcessCrash(BaseException):
        pass

    path = tmp_path / "startup-workspace.sqlite3"
    backend = StrictFakeWorkspaceBackend()
    store = SqliteEventStore(path, workspace_backend=backend)
    graph = GraphDefinition("workspace-startup", 1, (
        NodeDefinition("safe", NodeType.AGENT, "safe", settings={"checkpoint_type": "task_spec"}),
        NodeDefinition("analyze", NodeType.AGENT, "analyst", dependencies=("safe",)),
    ))
    created = GraphStore(store).create_run("task-workspace", validate_graph(graph))
    record = store.missions.save_workspace(workspace_id="workspace", task_id="task-workspace",
        repository_path="D:/fake/repository", worktree_path="D:/fake/worktree", base_ref="main",
        status="active", head_commit="a" * 40)
    store.missions.update_graph_run("task-workspace", previous_revision=created.revision, workspace_id="workspace")
    backend.register(WorkspaceSnapshot("workspace", "task-workspace", "a" * 40, {}), record)
    calls = []

    def interrupted(context):
        calls.append(context.attempt)
        raise ProcessCrash()

    runner = GraphRunner(store, HandlerRegistry({"safe": lambda context: NodeResult(NodeStatus.SUCCEEDED),
                                               "analyst": interrupted}), workspace_backend=backend)
    with pytest.raises(ProcessCrash):
        runner.run("task-workspace", graph)
    assert backend.checkpoint_count == 1 and calls == [1]
    store.close()

    unavailable = SqliteEventStore(path)
    try:
        blocked = json.loads(unavailable.missions.get_graph_run("task-workspace")["recovery_plan_json"])
        assert blocked["blocked_reason"] == "workspace_unavailable" and not blocked["token"]
    finally:
        unavailable.close()

    reopened = SqliteEventStore(path, workspace_backend=backend)
    try:
        run = reopened.missions.get_graph_run("task-workspace")
        prepared = json.loads(run["recovery_plan_json"])
        assert prepared["blocked_reason"] is None and prepared["token"]
        assert prepared["actions"] == [{"node_id": "analyze", "action": "retry_frozen"}]
        assert run["recovery_required"] == 1
        assert backend.checkpoint_count == 1 and backend.restore_count == 0 and calls == [1]
    finally:
        reopened.close()
