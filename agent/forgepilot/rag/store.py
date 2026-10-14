"""Minimal rag_files persistence on SqliteEventStore's connection and lock."""
from __future__ import annotations

from datetime import datetime
import sqlite3

from ..errors import ForgePilotError
from .models import IndexFileState, checked_relative_path, checked_repository_id


def create_rag_schema(db):
    db.execute("""CREATE TABLE rag_files(
        repository_id TEXT NOT NULL CHECK(length(repository_id) BETWEEN 1 AND 256),
        path TEXT NOT NULL CHECK(length(path) BETWEEN 1 AND 1024),
        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
        indexed_at TEXT NOT NULL,
        chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0),
        status TEXT NOT NULL CHECK(status IN ('pending','indexed','failed')),
        PRIMARY KEY(repository_id,path))""")


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
