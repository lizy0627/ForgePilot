"""Schema-8 migration, repository boundaries, and durable Artifact failure tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
import sqlite3
from uuid import uuid4

import pytest

from forgepilot.artifacts import (
    ArtifactIntegrityError, ArtifactStorageError, ChangeSet, ChangedFile, create_artifact,
)
import forgepilot.artifacts.sqlite_store as artifact_sqlite
from forgepilot.errors import ForgePilotError
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION
import forgepilot.sqlite_store as sqlite_store_module
from test_artifact_protocol import HASH_A, HASH_B, plan, task_spec


SCHEMA8_TABLES = (
    "graph_node_results", "graph_node_states", "graph_run_inputs", "graph_events",
    "security_events", "validation_runs", "workspace_records", "checkpoint_intents", "checkpoints",
    "model_calls", "approvals", "artifact_relations", "artifacts",
    "task_node_attempts", "task_graph_runs",
)


def _v7_database(path: Path) -> str:
    store = SqliteEventStore(path)
    conversation_id = store.create_conversation(title="schema seven survives")["conversation_id"]
    store.close()
    with sqlite3.connect(path) as db:
        for table in SCHEMA8_TABLES:
            db.execute(f'DROP TABLE "{table}"')
        db.execute("PRAGMA user_version=7")
    return conversation_id


def _spec(task_id: str, artifact_id: str | None = None):
    return create_artifact(
        task_id=task_id, artifact_id=artifact_id, producer="orchestrator.v1",
        producer_node="draft-task", content=task_spec(),
    )


def _plan(task_id: str, spec_id: str, artifact_id: str | None = None):
    content = plan().model_copy(update={"task_spec_artifact_id": spec_id})
    return create_artifact(
        task_id=task_id, artifact_id=artifact_id, producer="planner.v1", producer_node="plan",
        parent_artifact_ids=(spec_id,), content=content,
    )


def _large_change(task_id: str, plan_id: str, artifact_id: str | None = None):
    content = ChangeSet(
        execution_plan_artifact_id=plan_id, summary="Large integrity-checked patch.",
        files=(ChangedFile(
            path="src/window.cpp", change_type="modified",
            unified_diff="@@ -1 +1 @@\n-" + ("a" * 140_000) + "\n+" + ("b" * 140_000),
            before_sha256=HASH_A, after_sha256=HASH_B, plan_step_ids=("STEP-1",),
            acceptance_criteria_ids=("AC-1",),
        ),),
    )
    return create_artifact(
        task_id=task_id, artifact_id=artifact_id, producer="implementer.v1",
        producer_node="implement", parent_artifact_ids=(plan_id,), content=content,
    )


def test_v7_to_v8_verified_backup_reopen_and_failure_rollback(tmp_path, monkeypatch):
    path = tmp_path / "schema7.sqlite3"
    conversation_id = _v7_database(path)
    store = SqliteEventStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert store.conversation_detail(conversation_id)["title"] == "schema seven survives"
        assert set(SCHEMA8_TABLES) <= {
            row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        backup = Path(store.migration_backup_path)
    finally:
        store.close()
    assert backup.parent == tmp_path and ".v7-backup-" in backup.name
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    SqliteEventStore(path).close()
    assert len(list(tmp_path.glob("*.v7-backup-*.sqlite3"))) == 1

    failed = tmp_path / "failed.sqlite3"
    failed_conversation = _v7_database(failed)
    real_create = sqlite_store_module.create_mission_schema

    def fail_after_ddl(db):
        real_create(db)
        db.execute("UPDATE conversations SET title='must roll back'")
        raise sqlite3.OperationalError("injected schema-eight failure")

    monkeypatch.setattr(sqlite_store_module, "create_mission_schema", fail_after_ddl)
    with pytest.raises(ForgePilotError, match="storage_unavailable"):
        SqliteEventStore(failed)
    with sqlite3.connect(failed) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone()
        assert db.execute("SELECT title FROM conversations WHERE conversation_id=?", (failed_conversation,)).fetchone()[0] == "schema seven survives"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(list(tmp_path.glob("failed.sqlite3.v7-backup-*.sqlite3"))) == 1

    corrupt = tmp_path / "corrupt.sqlite3"
    original = b"not a sqlite database; do not replace me"
    corrupt.write_bytes(original)
    with pytest.raises(ForgePilotError, match="storage_unavailable"):
        SqliteEventStore(corrupt)
    assert corrupt.read_bytes() == original


def test_inline_external_windows_paths_tamper_and_partial_write(tmp_path, monkeypatch):
    database = tmp_path / "store.sqlite3"
    root = tmp_path / "任务 artifacts with spaces"
    store = SqliteEventStore(database, artifact_root=root)
    task_id = str(uuid4())
    spec = _spec(task_id)
    execution_plan = _plan(task_id, spec.artifact_id)
    large = _large_change(task_id, execution_plan.artifact_id)
    try:
        store.artifacts.put(spec)
        store.artifacts.put(execution_plan)
        store.artifacts.put(large)
        rows = store._db.execute(
            "SELECT artifact_id,storage_kind,relative_path FROM artifacts ORDER BY created_at,artifact_id"
        ).fetchall()
        by_id = {row["artifact_id"]: row for row in rows}
        assert by_id[spec.artifact_id]["storage_kind"] == "inline"
        assert by_id[large.artifact_id]["storage_kind"] == "external"
        relative = by_id[large.artifact_id]["relative_path"]
        assert relative == f"{task_id}/artifacts/{large.artifact_id}.json"
        assert "\\" not in relative and not PureWindowsPath(relative).is_absolute()
        external = root / Path(*relative.split("/"))
        assert external.is_file() and store.artifacts.get(large.artifact_id) == large

        original = external.read_bytes()
        external.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with pytest.raises(ArtifactIntegrityError, match="SHA256"):
            store.artifacts.get(large.artifact_id)
        external.write_bytes(original)

        store._db.execute(
            "UPDATE artifacts SET relative_path=? WHERE artifact_id=?",
            (r"C:\outside\artifact.json", large.artifact_id),
        )
        with pytest.raises(ArtifactIntegrityError, match="unsafe relative path"):
            store.artifacts.get(large.artifact_id)
        store._db.execute(
            "UPDATE artifacts SET relative_path=? WHERE artifact_id=?", (relative, large.artifact_id),
        )

        partial = _large_change(task_id, execution_plan.artifact_id)
        real_write = artifact_sqlite.os.write
        calls = 0

        def fail_partial(descriptor, value):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(descriptor, value[:37])
            raise OSError("injected partial disk write")

        monkeypatch.setattr(artifact_sqlite.os, "write", fail_partial)
        with pytest.raises(ArtifactStorageError, match="atomically"):
            store.artifacts.put(partial)
        assert not store._db.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id=?", (partial.artifact_id,)).fetchone()
        expected = root / task_id / "artifacts" / f"{partial.artifact_id}.json"
        assert not expected.exists() and not list(expected.parent.glob(f".{expected.name}.*.tmp"))
    finally:
        store.close()


def test_external_artifact_root_resolves_after_first_creation(tmp_path, monkeypatch):
    """Packaged Windows apps resolve newly materialized app-data paths differently."""
    alias, physical = tmp_path / "new-appdata", tmp_path / "physical-appdata"
    physical.mkdir()
    resolve = Path.resolve

    def redirected(path, strict=False):
        if path == alias and path.exists():
            return resolve(physical, strict=strict)
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", redirected)
    store = SqliteEventStore(tmp_path / "virtualized.sqlite3", artifact_root=alias)
    try:
        task_id = str(uuid4())
        spec = _spec(task_id)
        execution_plan = _plan(task_id, spec.artifact_id)
        large = _large_change(task_id, execution_plan.artifact_id)
        for item in (spec, execution_plan, large):
            store.artifacts.put(item)
        assert store.artifacts.root == physical
        assert store.artifacts.get(large.artifact_id) == large
        assert (physical / task_id / "artifacts" / f"{large.artifact_id}.json").is_file()
    finally:
        store.close()


def test_concurrent_writers_approval_invalidation_and_safe_model_metadata(tmp_path):
    store = SqliteEventStore(tmp_path / "concurrent.sqlite3", artifact_root=tmp_path / "tasks")
    task_id = str(uuid4())
    try:
        artifacts = [_spec(task_id) for _ in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(store.artifacts.put, artifacts))
        assert len(store.artifacts.list_for_task(task_id)) == 12
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        approved_spec = artifacts[0]
        execution_plan = _plan(task_id, approved_spec.artifact_id)
        store.artifacts.put(execution_plan)
        scope_hash = "c" * 64
        graph = store.missions.create_graph_run(
            task_id=task_id, graph_id="mission.v1", graph_version=1,
            task_spec_artifact_id=approved_spec.artifact_id,
            plan_artifact_id=execution_plan.artifact_id, scope_hash=scope_hash,
        )
        approval_id = str(uuid4())
        approval = store.missions.save_approval(
            approval_id=approval_id, task_id=task_id,
            task_spec_artifact_id=approved_spec.artifact_id,
            plan_artifact_id=execution_plan.artifact_id, scope_hash=scope_hash,
            approved_by="human.local",
        )
        assert approval["valid"]
        store.missions.update_graph_run(task_id, previous_revision=graph["current_revision"], scope_hash="d" * 64)
        assert not store.missions.get_approval(approval_id)["valid"]

        secret = "sk-this-is-a-fake-secret-value"
        call_id = str(uuid4())
        saved = store.missions.record_model_call(
            call_id=call_id, task_id=task_id, role="planner", model="openai/gpt-test",
            prompt_version="plan.v1", input_hash="e" * 64,
            tool_schema={
                "type": "object", "api_key": secret,
                "chain_of_thought": "private reasoning",
                "properties": {"query": {"description": f"API Key: {secret}"}},
            }, duration_ms=25, input_tokens=10, output_tokens=5, total_tokens=15,
            retry_count=1, fallback_model="local/fallback", response_hash="f" * 64,
        )
        encoded = store._db.execute(
            "SELECT tool_schema_json FROM model_calls WHERE call_id=?", (call_id,)).fetchone()[0]
        assert secret not in encoded and "private reasoning" not in encoded
        assert saved["tool_schema"]["api_key"] == "[REDACTED]"
        columns = {row[1] for row in store._db.execute("PRAGMA table_info(model_calls)")}
        assert not {"prompt", "response", "api_key", "reasoning", "chain_of_thought"} & columns
    finally:
        store.close()
