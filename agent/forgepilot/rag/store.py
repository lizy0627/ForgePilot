"""Minimal rag_files persistence on SqliteEventStore's connection and lock."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from ..errors import ForgePilotError
from .models import (IndexFileState, IndexRunRecord, checked_relative_path,
                     checked_repository_id)


def create_rag_schema(db):
    db.execute("""CREATE TABLE rag_files(
        repository_id TEXT NOT NULL CHECK(length(repository_id) BETWEEN 1 AND 256),
        path TEXT NOT NULL CHECK(length(path) BETWEEN 1 AND 1024),
        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
        indexed_at TEXT NOT NULL,
        chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0),
        status TEXT NOT NULL CHECK(status IN ('pending','indexed','failed')),
        PRIMARY KEY(repository_id,path))""")


def create_rag_run_schema(db):
    db.execute("""CREATE TABLE rag_index_runs(
        repository_id TEXT PRIMARY KEY CHECK(length(repository_id) BETWEEN 1 AND 256),
        operation_id TEXT NOT NULL CHECK(length(operation_id)=32),
        operation TEXT NOT NULL CHECK(operation IN ('index','clear')),
        status TEXT NOT NULL CHECK(status IN
            ('indexing','cancelling','ready','empty','degraded','failed','cancelled')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        total_files INTEGER NOT NULL CHECK(total_files >= 0),
        processed INTEGER NOT NULL CHECK(processed >= 0 AND processed <= total_files),
        skipped INTEGER NOT NULL CHECK(skipped >= 0),
        failed INTEGER NOT NULL CHECK(failed >= 0),
        chunks INTEGER NOT NULL CHECK(chunks >= 0),
        error_code TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        CHECK(skipped + failed <= processed))""")


class RagFileStore:
    def __init__(self, event_store):
        self.events = event_store

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return IndexFileState(
            repository_id=row["repository_id"],
            path=row["path"],
            content_hash=row["content_hash"],
            indexed_at=datetime.fromisoformat(row["indexed_at"]),
            chunk_count=row["chunk_count"],
            status=row["status"],
        )

    def get(self, repository_id: str, path: str) -> IndexFileState | None:
        repository_id, path = checked_repository_id(repository_id), checked_relative_path(path)
        with self.events._lock:
            return self._record(self.events._db.execute(
                "SELECT * FROM rag_files WHERE repository_id=? AND path=?", (repository_id, path)
            ).fetchone())

    def list(self, repository_id: str) -> list[IndexFileState]:
        repository_id = checked_repository_id(repository_id)
        with self.events._lock:
            return [self._record(row) for row in self.events._db.execute(
                "SELECT * FROM rag_files WHERE repository_id=? ORDER BY path", (repository_id,)
            ).fetchall()]

    def upsert(self, state: IndexFileState) -> IndexFileState:
        state = IndexFileState.model_validate(state)
        try:
            with self.events._transaction():
                self.events._db.execute("""INSERT INTO rag_files(
                    repository_id,path,content_hash,indexed_at,chunk_count,status) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(repository_id,path) DO UPDATE SET
                    content_hash=excluded.content_hash,indexed_at=excluded.indexed_at,
                    chunk_count=excluded.chunk_count,status=excluded.status""",
                    (state.repository_id, state.path, state.content_hash, state.indexed_at.isoformat(),
                     state.chunk_count, state.status))
            return state
        except sqlite3.Error:
            raise ForgePilotError("rag_storage", "RAG file state was not saved") from None

    def delete(self, repository_id: str, path: str) -> bool:
        repository_id, path = checked_repository_id(repository_id), checked_relative_path(path)
        try:
            with self.events._transaction():
                return bool(self.events._db.execute(
                    "DELETE FROM rag_files WHERE repository_id=? AND path=?", (repository_id, path)
                ).rowcount)
        except sqlite3.Error:
            raise ForgePilotError("rag_storage", "RAG file state was not deleted") from None

    def clear_repository(self, repository_id: str) -> int:
        repository_id = checked_repository_id(repository_id)
        try:
            with self.events._transaction():
                return self.events._db.execute(
                    "DELETE FROM rag_files WHERE repository_id=?", (repository_id,)
                ).rowcount
        except sqlite3.Error:
            raise ForgePilotError("rag_storage", "RAG repository state was not cleared") from None


class RagRunStore:
    """Atomic, repository-scoped ownership and latest-snapshot persistence."""

    _COLUMNS = ("repository_id,operation_id,operation,status,revision,total_files,processed,"
                "skipped,failed,chunks,error_code,started_at,updated_at,finished_at")

    def __init__(self, event_store):
        self.events = event_store

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return IndexRunRecord(
            repository_id=row["repository_id"], operation_id=row["operation_id"],
            operation=row["operation"], status=row["status"], revision=row["revision"],
            total_files=row["total_files"], processed=row["processed"],
            skipped=row["skipped"], failed=row["failed"], chunks=row["chunks"],
            error_code=row["error_code"], started_at=datetime.fromisoformat(row["started_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        )

    def get(self, repository_id: str) -> IndexRunRecord | None:
        repository_id = checked_repository_id(repository_id)
        with self.events._lock:
            row = self.events._db.execute(
                f"SELECT {self._COLUMNS} FROM rag_index_runs WHERE repository_id=?",
                (repository_id,),
            ).fetchone()
        return self._record(row)

    def begin(self, repository_id: str, operation: str) -> IndexRunRecord:
        repository_id = checked_repository_id(repository_id)
        now = datetime.now(timezone.utc)
        record = IndexRunRecord(
            repository_id=repository_id, operation_id=uuid4().hex, operation=operation,
            status="indexing", revision=1, started_at=now, updated_at=now,
        )
        try:
            with self.events._transaction():
                current = self.events._db.execute(
                    "SELECT status FROM rag_index_runs WHERE repository_id=?", (repository_id,)
                ).fetchone()
                if current is not None and current["status"] in {"indexing", "cancelling"}:
                    raise ForgePilotError("rag_index_busy", "This repository index is busy")
                self.events._db.execute("""INSERT INTO rag_index_runs(
                    repository_id,operation_id,operation,status,revision,total_files,processed,
                    skipped,failed,chunks,error_code,started_at,updated_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repository_id) DO UPDATE SET
                    operation_id=excluded.operation_id,operation=excluded.operation,
                    status=excluded.status,revision=excluded.revision,total_files=0,processed=0,
                    skipped=0,failed=0,chunks=0,error_code=NULL,
                    started_at=excluded.started_at,updated_at=excluded.updated_at,finished_at=NULL""",
                    self._values(record))
            return record
        except ForgePilotError:
            raise
        except sqlite3.Error:
            raise ForgePilotError("rag_storage", "RAG operation state was not started") from None

    @staticmethod
    def _values(record: IndexRunRecord):
        return (
            record.repository_id, record.operation_id, record.operation, record.status,
            record.revision, record.total_files, record.processed, record.skipped,
            record.failed, record.chunks, record.error_code,
            record.started_at.isoformat(), record.updated_at.isoformat(),
            record.finished_at.isoformat() if record.finished_at else None,
        )

    def update(self, record: IndexRunRecord, *, previous_revision: int) -> IndexRunRecord:
        record = IndexRunRecord.model_validate(record)
        try:
            with self.events._transaction():
                changed = self.events._db.execute("""UPDATE rag_index_runs SET
                    status=?,revision=?,total_files=?,processed=?,skipped=?,failed=?,chunks=?,
                    error_code=?,updated_at=?,finished_at=?
                    WHERE repository_id=? AND operation_id=? AND revision=?""", (
                    record.status, record.revision, record.total_files, record.processed,
                    record.skipped, record.failed, record.chunks, record.error_code,
                    record.updated_at.isoformat(),
                    record.finished_at.isoformat() if record.finished_at else None,
                    record.repository_id, record.operation_id, previous_revision,
                )).rowcount
                if changed != 1:
                    raise ForgePilotError("rag_index_conflict", "RAG operation state changed concurrently")
            return record
        except ForgePilotError:
            raise
        except sqlite3.Error:
            raise ForgePilotError("rag_storage", "RAG operation state was not saved") from None


def recover_rag_runs(db) -> None:
    """Close operations left nonterminal by a process interruption."""
    stamp = datetime.now(timezone.utc).isoformat()
    db.execute("""UPDATE rag_index_runs SET status='failed',revision=revision+1,
        error_code='rag_index_interrupted',updated_at=?,finished_at=?
        WHERE status IN ('indexing','cancelling')""", (stamp, stamp))
