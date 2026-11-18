"""Immutable, independently durable snapshots of completed graph nodes.

A checkpoint is prepared before publishing the node result. If the process
stops in that window, recovery can verify and publish the same result without
calling a writer twice. Workspace inspection and restoration belong to the
WorktreeManager boundary, not this SQLite repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import sqlite3
from types import MappingProxyType
from typing import Mapping

from ..errors import ForgePilotError
from .models import NodeResult, NodeStatus, _canonical, _identifier, SHA256


class CheckpointIntegrityError(ForgePilotError):
    def __init__(self, message: str = "Checkpoint integrity verification failed") -> None:
        super().__init__("checkpoint_integrity", message)


def checkpoint_id_for(task_id: str, node_id: str, attempt: int) -> str:
    """Stable identity usable before a WorktreeManager creates its commit."""
    _identifier(task_id, "task ID")
    _identifier(node_id, "node ID")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Checkpoint attempt must be a positive integer")
    return "checkpoint-" + sha256(_canonical([task_id, node_id, attempt])).hexdigest()


def _file_hashes(value: Mapping[str, str | None]) -> dict[str, str | None]:
    result = dict(value)
    for path, digest in result.items():
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise ValueError("Checkpoint file paths must be safe relative paths")
        pure, windows = PurePosixPath(path), PureWindowsPath(path)
        if (pure.is_absolute() or windows.drive or path != pure.as_posix()
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(PureWindowsPath(part).is_reserved() or ":" in part for part in pure.parts)):
            raise ValueError("Checkpoint file paths must be safe relative paths")
        if digest is not None and (not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise ValueError("Checkpoint file hashes must be SHA256 or None for absent files")
    return result


def _artifacts(value) -> tuple[tuple[str, str], ...]:
    pairs = tuple(tuple(pair) for pair in value)
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError("Checkpoint Artifacts must contain ID/hash pairs")
    if len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError("Checkpoint contains duplicate Artifact IDs")
    for artifact_id, digest in pairs:
        _identifier(artifact_id, "Artifact ID")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("Checkpoint Artifact hashes must be SHA256")
    return pairs


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str
    task_id: str
    node_id: str
    attempt: int
    checkpoint_type: str
    result: NodeResult
    workspace_id: str | None
    head_commit: str | None
    file_hashes: Mapping[str, str | None]
    input_artifacts: tuple[tuple[str, str], ...]
    output_artifacts: tuple[tuple[str, str], ...]
    idempotency_key: str
    ordinal: int
    created_at: str
    data_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_hashes", MappingProxyType(dict(self.file_hashes)))

    @property
    def data_hash(self) -> str:
        return self.data_sha256

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id, "task_id": self.task_id,
            "node_id": self.node_id, "attempt": self.attempt,
            "checkpoint_type": self.checkpoint_type, "result": self.result.to_dict(),
            "workspace_id": self.workspace_id, "head_commit": self.head_commit,
            "file_hashes": dict(self.file_hashes),
            "input_artifacts": [list(pair) for pair in self.input_artifacts],
            "output_artifacts": [list(pair) for pair in self.output_artifacts],
            "idempotency_key": self.idempotency_key, "ordinal": self.ordinal,
            "created_at": self.created_at,
        }


class CheckpointStore:
    def __init__(self, event_store) -> None:
        self.events_store = event_store

    checkpoint_id_for = staticmethod(checkpoint_id_for)

    @staticmethod
    def _intent(row) -> dict:
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise CheckpointIntegrityError("Checkpoint intent payload is invalid")
            record = {
                "checkpoint_id": row["checkpoint_id"], "task_id": row["task_id"],
                "node_id": row["node_id"], "attempt": row["attempt"], "payload": payload,
            }
            if (record["checkpoint_id"] != checkpoint_id_for(
                    record["task_id"], record["node_id"], record["attempt"])
                    or sha256(_canonical(record)).hexdigest() != row["data_sha256"]):
                raise CheckpointIntegrityError("Checkpoint intent integrity verification failed")
            return {**record, "data_sha256": row["data_sha256"]}
        except CheckpointIntegrityError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CheckpointIntegrityError("Checkpoint intent payload is invalid") from None

    def save_intent(self, task_id: str, node_id: str, attempt: int, payload: dict) -> dict:
        """Durably freeze expected effects before creating an external checkpoint.

        An intent never authorizes execution. Recovery must verify the external
        checkpoint against this immutable intent and await user confirmation
        before publishing a missing SQLite checkpoint/result.
        """
        checkpoint_id = checkpoint_id_for(task_id, node_id, attempt)
        if not isinstance(payload, dict):
            raise ValueError("Checkpoint intent payload must be a JSON object")
        safe_payload = self.events_store.redactor.data(payload)
        payload_json = _canonical(safe_payload).decode("utf-8")
        canonical_payload = json.loads(payload_json)
        proposed = {"checkpoint_id": checkpoint_id, "task_id": task_id,
                    "node_id": node_id, "attempt": attempt, "payload": canonical_payload}
        digest = sha256(_canonical(proposed)).hexdigest()
        try:
            with self.events_store.transaction() as db:
                if db.execute(
                    "SELECT 1 FROM task_node_attempts WHERE task_id=? AND node_id=? AND attempt=?",
                    (task_id, node_id, attempt),
                ).fetchone() is None:
                    raise ForgePilotError("checkpoint_attempt", "Checkpoint intent has no matching node attempt")
                existing = db.execute(
                    "SELECT * FROM checkpoint_intents WHERE task_id=? AND node_id=? AND attempt=?",
                    (task_id, node_id, attempt),
                ).fetchone()
                if existing is not None:
                    saved = self._intent(existing)
                    if saved != {**proposed, "data_sha256": digest}:
                        raise ForgePilotError("checkpoint_conflict", "A node checkpoint intent is immutable")
                    return saved
                db.execute(
                    """INSERT INTO checkpoint_intents(checkpoint_id,task_id,node_id,attempt,payload_json,data_sha256)
                       VALUES(?,?,?,?,?,?)""",
                    (checkpoint_id, task_id, node_id, attempt, payload_json, digest),
                )
                return {**proposed, "data_sha256": digest}
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint intent was not saved") from None

    def intent_for_attempt(self, task_id: str, node_id: str, attempt: int) -> dict | None:
        try:
            with self.events_store.locked_connection() as db:
                row = db.execute(
                    "SELECT * FROM checkpoint_intents WHERE task_id=? AND node_id=? AND attempt=?",
                    (task_id, node_id, attempt),
                ).fetchone()
                return self._intent(row) if row is not None else None
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint intent could not be read") from None

    def intents_for_task(self, task_id: str) -> tuple[dict, ...]:
        try:
            with self.events_store.locked_connection() as db:
                rows = db.execute(
                    "SELECT * FROM checkpoint_intents WHERE task_id=? ORDER BY rowid", (task_id,)
                ).fetchall()
                return tuple(self._intent(row) for row in rows)
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint intents could not be read") from None

    @staticmethod
    def _record(row) -> CheckpointRecord:
        try:
            record = CheckpointRecord(
                checkpoint_id=row["checkpoint_id"], task_id=row["task_id"],
                node_id=row["node_id"], attempt=row["attempt"],
                checkpoint_type=row["checkpoint_type"],
                result=NodeResult.from_dict(json.loads(row["result_json"])),
                workspace_id=row["workspace_id"], head_commit=row["commit_hash"],
                file_hashes=_file_hashes(json.loads(row["file_hashes_json"])),
                input_artifacts=_artifacts(json.loads(row["input_artifacts_json"])),
                output_artifacts=_artifacts(json.loads(row["output_artifacts_json"])),
                idempotency_key=row["idempotency_key"], ordinal=row["ordinal"],
                created_at=row["created_at"], data_sha256=row["data_sha256"],
            )
            if (row["status"] != "completed" or record.result.status is not NodeStatus.SUCCEEDED
                    or record.checkpoint_id != checkpoint_id_for(record.task_id, record.node_id, record.attempt)
                    or sha256(_canonical(record.to_dict())).hexdigest() != record.data_sha256):
                raise CheckpointIntegrityError()
            return record
        except CheckpointIntegrityError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CheckpointIntegrityError("Checkpoint payload is invalid") from None

    def create(self, task_id: str, node_id: str, attempt: int, checkpoint_type: str,
               result: NodeResult, *, workspace_id: str | None = None,
               head_commit: str | None = None, file_hashes: Mapping[str, str | None] | None = None,
               input_artifacts=(), output_artifacts=(),
               idempotency_key: str | None = None) -> CheckpointRecord:
        checkpoint_id = checkpoint_id_for(task_id, node_id, attempt)
        _identifier(checkpoint_type, "checkpoint type")
        if not isinstance(result, NodeResult) or result.status is not NodeStatus.SUCCEEDED:
            raise ValueError("Only successful completed nodes can create safe checkpoints")
        if workspace_id is not None:
            _identifier(workspace_id, "workspace ID")
        if head_commit is not None and (not isinstance(head_commit, str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_commit)):
            raise ValueError("Checkpoint HEAD must be a Git object hash")
        files = _file_hashes(file_hashes or {})
        inputs, outputs = _artifacts(input_artifacts), _artifacts(output_artifacts)
        if tuple(artifact_id for artifact_id, _ in outputs) != tuple(result.output_artifact_ids):
            raise ValueError("Checkpoint output Artifact IDs must match NodeResult")
        # Use the same redaction boundary as GraphStore before hashing/persisting.
        result = NodeResult.from_dict(self.events_store.redactor.data(result.to_dict()))
        try:
            with self.events_store.transaction() as db:
                attempt_row = db.execute(
                    """SELECT idempotency_key FROM task_node_attempts
                       WHERE task_id=? AND node_id=? AND attempt=?""",
                    (task_id, node_id, attempt),
                ).fetchone()
                if attempt_row is None:
                    raise ForgePilotError("checkpoint_attempt", "Checkpoint has no matching node attempt")
                key = idempotency_key if idempotency_key is not None else attempt_row[0]
                if not isinstance(key, str) or not SHA256.fullmatch(key) or key != attempt_row[0]:
                    raise ForgePilotError("checkpoint_conflict", "Checkpoint idempotency key differs from its attempt")
                existing = db.execute(
                    "SELECT * FROM checkpoints WHERE task_id=? AND node_id=? AND attempt=?",
                    (task_id, node_id, attempt),
                ).fetchone()
                if existing is not None:
                    saved = self._record(existing)
                    proposed = {**saved.to_dict(), "checkpoint_type": checkpoint_type,
                                "result": result.to_dict(), "workspace_id": workspace_id,
                                "head_commit": head_commit, "file_hashes": files,
                                "input_artifacts": [list(pair) for pair in inputs],
                                "output_artifacts": [list(pair) for pair in outputs],
                                "idempotency_key": key}
                    if proposed != saved.to_dict():
                        raise ForgePilotError("checkpoint_conflict", "A node attempt checkpoint is immutable")
                    return saved
                # Explicit allocation lets the canonical hash cover the ordinal.
                # AUTOINCREMENT retains the high-water mark even if rows are deleted.
                sequence = db.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='checkpoints'"
                ).fetchone()
                ordinal = (sequence[0] if sequence is not None else 0) + 1
                stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                record = CheckpointRecord(
                    checkpoint_id, task_id, node_id, attempt, checkpoint_type, result,
                    workspace_id, head_commit, files, inputs, outputs, key, ordinal, stamp, "",
                )
                digest = sha256(_canonical(record.to_dict())).hexdigest()
                db.execute(
                    """INSERT INTO checkpoints(ordinal,checkpoint_id,task_id,node_id,attempt,
                       checkpoint_type,status,workspace_id,commit_hash,created_at,result_json,
                       file_hashes_json,input_artifacts_json,output_artifacts_json,idempotency_key,data_sha256)
                       VALUES(?,?,?,?,?,?,'completed',?,?,?,?,?,?,?,?,?)""",
                    (ordinal, checkpoint_id, task_id, node_id, attempt, checkpoint_type,
                     workspace_id, head_commit, stamp, _canonical(result.to_dict()).decode(),
                     _canonical(files).decode(), _canonical(inputs).decode(),
                     _canonical(outputs).decode(), key, digest),
                )
                row = db.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
                ).fetchone()
                return self._record(row)
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint was not saved") from None

    def for_attempt(self, task_id: str, node_id: str, attempt: int) -> CheckpointRecord | None:
        try:
            with self.events_store.locked_connection() as db:
                row = db.execute(
                    "SELECT * FROM checkpoints WHERE task_id=? AND node_id=? AND attempt=?",
                    (task_id, node_id, attempt),
                ).fetchone()
                return self._record(row) if row is not None else None
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint could not be read") from None

    def latest(self, task_id: str) -> CheckpointRecord | None:
        try:
            with self.events_store.locked_connection() as db:
                row = db.execute(
                    """SELECT * FROM checkpoints WHERE task_id=? AND node_id IS NOT NULL
                       ORDER BY ordinal DESC LIMIT 1""", (task_id,)
                ).fetchone()
                return self._record(row) if row is not None else None
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoint could not be read") from None

    def list_for_task(self, task_id: str) -> tuple[CheckpointRecord, ...]:
        try:
            with self.events_store.locked_connection() as db:
                rows = db.execute(
                    """SELECT * FROM checkpoints WHERE task_id=? AND node_id IS NOT NULL
                       ORDER BY ordinal""", (task_id,)
                ).fetchall()
                return tuple(self._record(row) for row in rows)
        except sqlite3.Error:
            raise ForgePilotError("checkpoint_storage", "Checkpoints could not be read") from None

    def verify(self, record: CheckpointRecord) -> None:
        """Verify the entire durable snapshot; workspace/Artifact checks follow upstream."""
        saved = self.for_attempt(record.task_id, record.node_id, record.attempt)
        if saved is None or saved != record:
            raise CheckpointIntegrityError("Checkpoint no longer matches its durable snapshot")


__all__ = ["CheckpointIntegrityError", "CheckpointRecord", "CheckpointStore", "checkpoint_id_for"]
