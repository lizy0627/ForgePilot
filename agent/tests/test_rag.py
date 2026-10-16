"""RAG contracts and storage only; no chunking, embeddings or retrieval."""
import argparse
from datetime import datetime, timezone
import hashlib
import sqlite3

import pytest
from pydantic import ValidationError

from forgepilot.context import TaskContext
from forgepilot.rag import (CodeChunk, IndexFileState, RagConfig, RagFileStore,
                            RetrievalQuery, add_rag_arguments, protect_embedding_api_key,
                            rag_config_from_args, rag_runtime_from_args, unprotect_embedding_api_key)
from forgepilot.errors import ForgePilotError
import forgepilot.rag.config as rag_config_module
from forgepilot.recording import now, snapshot_context
from forgepilot.sqlite_store import SqliteEventStore


FILE_HASH = hashlib.sha256(b"complete file").hexdigest()


def chunk(**changes):
    values = dict(
        repository_id="repository-fixture",
        path="src/main.py",
        language="python",
        symbol="calculate",
        symbol_type="function",
        start_line=3,
        end_line=5,
        content="def calculate():\n    return 1\n",
        file_hash=FILE_HASH,
    )
    return CodeChunk(**{**values, **changes})


def test_models_are_strict_forbid_extra_and_validate_ranges():
    original = chunk()
    assert original.chunk_id and len(original.chunk_id) == 64
    with pytest.raises(ValidationError, match="extra_forbidden"):
        chunk(unexpected="value")
    with pytest.raises(ValidationError, match="int_type"):
        chunk(start_line="3")
    with pytest.raises(ValidationError, match="end_line"):
        chunk(start_line=6, end_line=5)
    with pytest.raises(ValidationError, match="relative repository path"):
        chunk(path="../secret.py")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RetrievalQuery(repository_id="repository-fixture", query="where", unknown=True)


def test_chunk_id_is_stable_and_changes_with_content():
    first = chunk()
    assert chunk().chunk_id == first.chunk_id
    assert chunk(content=first.content + "# changed\n").chunk_id != first.chunk_id
    with pytest.raises(ValidationError, match="chunk_id"):
        chunk(chunk_id="0" * 64)
    assert first.model_dump()["chunk_id"] == first.chunk_id


def test_rag_config_is_disabled_and_credentials_use_secret_boundary(monkeypatch):
    config = RagConfig()
    assert not config.enabled
    assert "api_key" not in RagConfig.model_fields
    assert "api_key" not in config.model_dump_json()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RagConfig(api_key="must-not-be-plain-config")
    with pytest.raises(ValidationError):
        RagConfig(enabled=True)

    protected = []
    monkeypatch.setattr(rag_config_module, "protect_secrets",
                        lambda values: protected.append(values) or b"ciphertext")
    monkeypatch.setattr(rag_config_module, "unprotect_secrets",
                        lambda value: {"rag_embedding_api_key": "secret"})
    assert protect_embedding_api_key("secret") == b"ciphertext"
    assert protected == [{"rag_embedding_api_key": "secret"}]
    assert unprotect_embedding_api_key(b"ciphertext") == "secret"


def test_rag_arguments_default_off_and_runtime_secret_is_not_represented(monkeypatch):
    parser = argparse.ArgumentParser()
    add_rag_arguments(parser)
    defaults = parser.parse_args([])
    assert rag_config_from_args(defaults) == RagConfig()
    assert rag_runtime_from_args(defaults, api_key="unused") is None
    defaults.embedding_base_url = "not a URL"
    assert rag_runtime_from_args(defaults, api_key=None) is None

    enabled = parser.parse_args([
        "--enable-rag", "--embedding-model", "embedding-fixture",
        "--embedding-base-url", "https://provider.invalid/v1",
    ])
    monkeypatch.delenv("FORGEPILOT_EMBEDDING_API_KEY", raising=False)
    runtime = rag_runtime_from_args(enabled, api_key="private-runtime-key")
    assert runtime.config.enabled
    assert "private-runtime-key" not in repr(runtime)
    assert "embedding_api_key" not in runtime.model_dump()
    assert "api_key" not in runtime.config.model_dump()
    with pytest.raises(ForgePilotError, match="rag_config"):
        rag_config_from_args(parser.parse_args(["--rag-max-file-bytes", "0"]))


def state(path="src/main.py", *, content_hash=FILE_HASH, chunks=2, status="indexed"):
    return IndexFileState(
        repository_id="repository-fixture",
        path=path,
        content_hash=content_hash,
        indexed_at=datetime(2026, 10, 14, 1, 2, 3, tzinfo=timezone.utc),
        chunk_count=chunks,
        status=status,
    )


def test_rag_files_crud_is_repository_scoped():
    events = SqliteEventStore()
    store = RagFileStore(events)
    try:
        first = state()
        second = state("src/other.py", chunks=1)
        assert store.get(first.repository_id, first.path) is None
        assert store.upsert(first) == first
        store.upsert(second)
        assert store.get(first.repository_id, first.path) == first
        assert store.list(first.repository_id) == [first, second]

        changed = state(content_hash="1" * 64, chunks=3, status="failed")
        store.upsert(changed)
        assert store.get(changed.repository_id, changed.path) == changed
        assert store.delete(first.repository_id, second.path)
        assert not store.delete(first.repository_id, second.path)
        assert store.clear_repository(first.repository_id) == 1
        assert store.list(first.repository_id) == []
    finally:
        events.close()


def test_v5_upgrade_preserves_tasks_conversations_and_memories_and_is_repeatable(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = SqliteEventStore(path)
    task = snapshot_context(
        TaskContext("D:/development-test", "legacy task payload",
                    task_id="00000000-0000-4000-8000-000000000001"),
        created_at="2026-10-14T00:00:00+00:00",
        updated_at=now(),
    )
    store.create(task["task_id"], task)
    store._db.execute("UPDATE tasks SET closed=1 WHERE task_id=?", (task["task_id"],))
    conversation = store.create_conversation(title="legacy conversation")
    memory = store.save_memory({"scope": "global", "content": "legacy memory"})
    store.close()

    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE rag_index_runs")
        db.execute("DROP TABLE rag_files")
        db.execute("PRAGMA user_version=5")

    for _ in range(2):
        upgraded = SqliteEventStore(path)
        try:
            assert upgraded._db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert upgraded.get(task["task_id"])["prompt"] == "legacy task payload"
            assert upgraded.conversation_detail(conversation["conversation_id"])["title"] == "legacy conversation"
            assert upgraded.memory_list()["items"][0]["content"] == memory["content"]
            assert RagFileStore(upgraded).list("repository-fixture") == []
        finally:
            upgraded.close()
    assert len(list(tmp_path.glob("*.v5-backup-*.sqlite3"))) == 1


def test_v6_upgrade_adds_durable_rag_runs_with_a_single_verified_backup(tmp_path):
    path = tmp_path / "v6.sqlite3"
    SqliteEventStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE rag_index_runs")
        db.execute("PRAGMA user_version=6")

    for _ in range(2):
        store = SqliteEventStore(path)
        try:
            assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert store._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rag_index_runs'"
            ).fetchone()
        finally:
            store.close()
    assert len(list(tmp_path.glob("*.v6-backup-*.sqlite3"))) == 1
