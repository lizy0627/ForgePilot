"""Durable SQLite metadata plus integrity-checked external Artifact payloads."""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import stat
from uuid import uuid4

from ..errors import ForgePilotError
from ..repository import is_link, opened_path
from .canonical import canonical_json_bytes
from .models import ArtifactContent, ArtifactEnvelope, ArtifactEnvelopeBase, DeliveryBundle
from .store import (
    ArtifactNotFoundError, ArtifactRelation, DuplicateArtifactError, validate_relationships,
)
from .validation import ArtifactProtocolError, ArtifactRelationshipError, create_artifact, validate_artifact


INLINE_LIMIT_BYTES = 256 * 1024
MAX_EXTERNAL_BYTES = 1024 * 1024 * 1024


class ArtifactIntegrityError(ArtifactProtocolError):
    pass


class ArtifactStorageError(ArtifactProtocolError):
    pass


def default_artifact_root() -> Path:
    configured = os.environ.get("FORGEPILOT_ARTIFACT_ROOT")
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ForgePilot" / "data" / "tasks"


class SqliteArtifactStore:
    """Immutable Artifact store sharing the event store's one-writer transaction lock."""

    def __init__(self, event_store, root: str | os.PathLike[str] | None = None) -> None:
        self.events = event_store
        candidate = Path(root) if root is not None else default_artifact_root()
        try:
            self.root = candidate.resolve(strict=False)
        except (OSError, ValueError, RuntimeError):
            raise ForgePilotError("artifact_path", "Artifact root is invalid") from None

    @staticmethod
    def _relative_path(task_id: str, artifact_id: str) -> str:
        return f"{task_id}/artifacts/{artifact_id}.json"

    def _checked_path(self, relative_path: str, *, task_id: str, artifact_id: str,
                      require_file: bool) -> Path:
        expected = self._relative_path(task_id, artifact_id)
        pure = PurePosixPath(relative_path)
        if (relative_path != expected or "\\" in relative_path or pure.is_absolute()
                or ".." in pure.parts or PureWindowsPath(relative_path).drive
                or any(PureWindowsPath(part).is_reserved() for part in pure.parts)):
            raise ArtifactIntegrityError("Artifact metadata contains an unsafe relative path")
        target = self.root.joinpath(*pure.parts)
        try:
            if not target.resolve(strict=False).is_relative_to(self.root):
                raise ArtifactIntegrityError("Artifact path escapes its configured root")
            current = self.root
            for part in pure.parts[:-1]:
                current /= part
                if current.exists() and is_link(current.lstat()):
                    raise ArtifactIntegrityError("Artifact path contains a link or reparse point")
            if require_file:
                info = target.lstat()
                if is_link(info) or not stat.S_ISREG(info.st_mode):
                    raise ArtifactIntegrityError("External Artifact is not a regular file")
            return target
        except ArtifactIntegrityError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise ArtifactIntegrityError("External Artifact path cannot be verified") from None

    def _make_parent(self, target: Path) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            relative_parent = target.parent.relative_to(self.root)
            current = self.root
            if is_link(current.lstat()) or not current.is_dir():
                raise ArtifactStorageError("Artifact root must be a real directory")
            for part in relative_parent.parts:
                current /= part
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                info = current.lstat()
                if is_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise ArtifactStorageError("Artifact directory contains a link or non-directory")
        except ArtifactStorageError:
            raise
        except OSError:
            raise ArtifactStorageError("Artifact directory could not be created safely") from None

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        self._make_parent(target)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        descriptor = None
        try:
            if target.exists():
                raise ArtifactStorageError("External Artifact path already exists")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short Artifact write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if target.exists():
                raise ArtifactStorageError("External Artifact path already exists")
            os.replace(temporary, target)
            try:
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                # Windows does not expose directory fsync through Python.
                pass
        except ArtifactStorageError:
            raise
        except OSError:
            raise ArtifactStorageError("External Artifact could not be written atomically") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_external(self, path: Path, expected_size: int, expected_hash: str) -> bytes:
        if not 0 <= expected_size <= MAX_EXTERNAL_BYTES:
            raise ArtifactIntegrityError("External Artifact size is invalid")
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                raise ArtifactIntegrityError("External Artifact size has changed")
            actual = opened_path(descriptor)
            if actual is not None and actual != path:
                raise ArtifactIntegrityError("External Artifact location changed while opening")
            chunks = bytearray()
            digest = sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                chunks.extend(block)
                digest.update(block)
                if len(chunks) > expected_size:
                    raise ArtifactIntegrityError("External Artifact grew while reading")
            if len(chunks) != expected_size or digest.hexdigest() != expected_hash:
                raise ArtifactIntegrityError("External Artifact SHA256 verification failed")
            return bytes(chunks)
        except ArtifactIntegrityError:
            raise
        except OSError:
            raise ArtifactIntegrityError("External Artifact is missing or unreadable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _row_payload(self, row: sqlite3.Row) -> bytes:
        if row["storage_kind"] == "inline":
            value = row["inline_json"]
            payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        elif row["storage_kind"] == "external":
            path = self._checked_path(
                row["relative_path"], task_id=row["task_id"], artifact_id=row["artifact_id"],
                require_file=True,
            )
            payload = self._read_external(path, row["size_bytes"], row["payload_sha256"])
        else:
            raise ArtifactIntegrityError("Artifact storage kind is invalid")
        if len(payload) != row["size_bytes"] or sha256(payload).hexdigest() != row["payload_sha256"]:
            raise ArtifactIntegrityError("Artifact payload SHA256 verification failed")
        return payload

    @staticmethod
    def _matches_row(artifact: ArtifactEnvelope, row: sqlite3.Row) -> bool:
        return all((
            artifact.artifact_id == row["artifact_id"], artifact.task_id == row["task_id"],
            artifact.artifact_type == row["artifact_type"], artifact.schema_version == row["schema_version"],
            artifact.producer == row["producer"], artifact.producer_node == row["producer_node"],
            artifact.created_at == row["created_at"], artifact.content_sha256 == row["content_sha256"],
        ))

    def _artifact_from_row(self, row: sqlite3.Row) -> ArtifactEnvelope:
        try:
            artifact = validate_artifact(self._row_payload(row))
        except ArtifactProtocolError:
            raise
        except (UnicodeError, ValueError, TypeError):
            raise ArtifactIntegrityError("Artifact payload is invalid") from None
        if not self._matches_row(artifact, row):
            raise ArtifactIntegrityError("Artifact payload does not match SQLite metadata")
        return artifact

    def put(self, artifact: ArtifactEnvelopeBase) -> None:
        artifact = validate_artifact(artifact)
        payload = canonical_json_bytes(artifact)
        payload_hash = sha256(payload).hexdigest()
        external = len(payload) >= INLINE_LIMIT_BYTES or isinstance(artifact.content, DeliveryBundle)
        relative_path = self._relative_path(artifact.task_id, artifact.artifact_id) if external else None
        target = (self._checked_path(relative_path, task_id=artifact.task_id,
                                     artifact_id=artifact.artifact_id, require_file=False)
                  if relative_path else None)
        created_external = False
        try:
            with self.events._transaction():
                if self.events._db.execute(
                        "SELECT 1 FROM artifacts WHERE artifact_id=?", (artifact.artifact_id,)).fetchone():
                    raise DuplicateArtifactError(f"Artifact already exists: {artifact.artifact_id}")
                parents = []
                for parent_id in artifact.parent_artifact_ids:
                    row = self.events._db.execute(
                        "SELECT * FROM artifacts WHERE artifact_id=?", (parent_id,)).fetchone()
                    if row is None:
                        raise ArtifactNotFoundError(parent_id)
                    parents.append(self._artifact_from_row(row))
                validate_relationships(artifact, parents)
                if target is not None:
                    self._atomic_write(target, payload)
                    created_external = True
                self.events._db.execute("""INSERT INTO artifacts(
                    artifact_id,task_id,artifact_type,schema_version,producer,producer_node,created_at,
                    content_sha256,payload_sha256,storage_kind,inline_json,relative_path,size_bytes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    artifact.artifact_id, artifact.task_id, artifact.artifact_type,
                    artifact.schema_version, artifact.producer, artifact.producer_node,
                    artifact.created_at, artifact.content_sha256, payload_hash,
                    "external" if external else "inline", None if external else payload,
                    relative_path, len(payload),
                ))
                stamp = artifact.created_at
                self.events._db.executemany("""INSERT INTO artifact_relations(
                    parent_artifact_id,child_artifact_id,relation,created_at) VALUES(?,?,?,?)""",
                    ((parent.artifact_id, artifact.artifact_id, "derived_from", stamp) for parent in parents),
                )
        except BaseException as exc:
            if created_external and target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, sqlite3.Error):
                raise ArtifactStorageError("Artifact metadata could not be saved") from None
            raise

    def get(self, artifact_id: str) -> ArtifactEnvelope:
        with self.events._lock:
            row = self.events._db.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if row is None:
                raise ArtifactNotFoundError(artifact_id)
            return self._artifact_from_row(row)

    def list_for_task(self, task_id: str) -> tuple[ArtifactEnvelope, ...]:
        with self.events._lock:
            rows = self.events._db.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at,artifact_id", (task_id,)).fetchall()
            return tuple(self._artifact_from_row(row) for row in rows)

    def relations_from(self, artifact_id: str) -> tuple[ArtifactRelation, ...]:
        self.get(artifact_id)
        with self.events._lock:
            rows = self.events._db.execute("""SELECT parent_artifact_id,child_artifact_id,relation
                FROM artifact_relations WHERE parent_artifact_id=?
                ORDER BY child_artifact_id,relation""", (artifact_id,)).fetchall()
        return tuple(ArtifactRelation(*row) for row in rows)

    def parents_of(self, artifact_id: str) -> tuple[ArtifactRelation, ...]:
        self.get(artifact_id)
        with self.events._lock:
            rows = self.events._db.execute("""SELECT parent_artifact_id,child_artifact_id,relation
                FROM artifact_relations WHERE child_artifact_id=?
                ORDER BY parent_artifact_id,relation""", (artifact_id,)).fetchall()
        return tuple(ArtifactRelation(*row) for row in rows)

    def is_referenced(self, artifact_id: str) -> bool:
        return bool(self.relations_from(artifact_id))

    def revise(self, artifact_id: str, *, content: ArtifactContent, producer: str,
               producer_node: str, artifact_id_override: str | None = None) -> ArtifactEnvelope:
        original = self.get(artifact_id)
        if type(content) is not type(original.content):
            raise ArtifactRelationshipError("A revision must preserve artifact_type")
        revision = create_artifact(
            artifact_id=artifact_id_override, task_id=original.task_id, producer=producer,
            producer_node=producer_node, parent_artifact_ids=(original.artifact_id,), content=content,
        )
        self.put(revision)
        with self.events._transaction():
            changed = self.events._db.execute("""UPDATE artifact_relations SET relation='revision_of'
                WHERE parent_artifact_id=? AND child_artifact_id=? AND relation='derived_from'""",
                (original.artifact_id, revision.artifact_id),).rowcount
            if changed != 1:
                raise ArtifactStorageError("Artifact revision relation was not saved")
        return revision


__all__ = [
    "ArtifactIntegrityError", "ArtifactStorageError", "INLINE_LIMIT_BYTES",
    "SqliteArtifactStore", "default_artifact_root",
]
