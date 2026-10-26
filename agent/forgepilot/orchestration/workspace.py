"""Fail-closed worktree boundary for node checkpoints and recovery.

The production ``forgepilot.workspace_manager.WorkspaceManager`` implements this
contract with local Git and durable writer ownership. It is injected explicitly;
the unavailable default cannot touch files. The fake stores *only hashes in memory*: it neither
opens repository files nor executes Git or a shell. A snapshot's file map is the
complete controlled-file manifest, including explicit ``None`` tombstones when
the manager tracks a deleted file. Comparing selected files is insufficient.

Manager implementations must atomically compare ``expected`` immediately before
changing a worktree, retain checkpoint IDs across process restarts, and make the
same checkpoint/restore request repeatable after an uncertain outcome. Inspection
must identify the task's isolated worktree, its HEAD, and unknown/unowned files;
an unverifiable state must fail closed. None of these methods grants user approval.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import json
import re
from threading import RLock
from types import MappingProxyType
from typing import Any, NoReturn, Protocol, runtime_checkable

from ..errors import ForgePilotError
from .models import IDENTIFIER, SHA256


_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
})


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _manifest(values: Mapping[str, str | None]) -> Mapping[str, str | None]:
    if not isinstance(values, Mapping):
        raise ValueError("File hashes must be a complete mapping")
    result: dict[str, str | None] = {}
    canonical: set[str] = set()
    for path, digest in values.items():
        if not isinstance(path, str) or not path or len(path) > 4096:
            raise ValueError("Invalid controlled file path")
        components = path.split("/")
        if ("\\" in path or any(ord(character) < 32 or ord(character) == 127
                                for character in path)
                or any(component in {"", ".", ".."} or component.casefold() == ".git"
                       or component.endswith((" ", "."))
                       or any(character in component for character in ':*?"<>|')
                       or component.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
                       for component in components)):
            raise ValueError("Invalid controlled file path")
        if path.casefold() in canonical:
            raise ValueError("Controlled file paths have a case collision")
        canonical.add(path.casefold())
        if digest is not None and (not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise ValueError("Invalid controlled file SHA256")
        result[path] = digest
    present = {path.casefold() for path, digest in result.items() if digest is not None}
    if any("/".join(path.split("/")[:index]) in present
           for path in present for index in range(1, len(path.split("/")))):
        raise ValueError("Controlled file paths contain a file/directory collision")
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    workspace_id: str
    task_id: str
    head_commit: str | None
    file_hashes: Mapping[str, str | None]
    known: bool = True

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "workspace ID")
        _identifier(self.task_id, "task ID")
        if not isinstance(self.known, bool):
            raise ValueError("Workspace known flag must be boolean")
        if (self.head_commit is not None
                and (not isinstance(self.head_commit, str)
                     or not _COMMIT.fullmatch(self.head_commit))):
            raise ValueError("Invalid worktree HEAD commit")
        if self.known and self.head_commit is None:
            raise ValueError("Known worktree requires a verified HEAD commit")
        object.__setattr__(self, "file_hashes", _manifest(self.file_hashes))


@runtime_checkable
class WorkspaceBackend(Protocol):
    """Task-isolated worktree operations; implementations must not merge changes."""

    def inspect(self, record: Mapping[str, Any]) -> WorkspaceSnapshot: ...

    def checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                   expected: WorkspaceSnapshot) -> WorkspaceSnapshot: ...

    def restore(self, record: Mapping[str, Any], checkpoint_id: str, *,
                expected: WorkspaceSnapshot) -> WorkspaceSnapshot: ...

    def verify_checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                          head_commit: str, file_hashes: Mapping[str, str | None]) -> None: ...


class UnavailableWorkspaceBackend:
    """Default until a real WorktreeManager is explicitly connected."""

    @staticmethod
    def _unavailable() -> NoReturn:
        raise ForgePilotError("workspace_unavailable", "WorktreeManager is not connected")

    def inspect(self, record: Mapping[str, Any]) -> WorkspaceSnapshot:
        self._unavailable()

    def checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                   expected: WorkspaceSnapshot) -> WorkspaceSnapshot:
        self._unavailable()

    def restore(self, record: Mapping[str, Any], checkpoint_id: str, *,
                expected: WorkspaceSnapshot) -> WorkspaceSnapshot:
        self._unavailable()

    def verify_checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                          head_commit: str, file_hashes: Mapping[str, str | None]) -> None:
        self._unavailable()


class SimulatedWorkspaceCrash(BaseException):
    """Test-only hard interruption after a worktree side effect became durable."""


def _locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class StrictFakeWorkspaceBackend:
    """Explicitly registered, hash-only test worktrees with optimistic preconditions.

    ``register`` seeds a workspace and optionally binds its persisted identity
    (repository/worktree paths and base ref). ``set_snapshot`` simulates external
    edits, HEAD movement, or an unknown workspace. The two ``fail_after_next_*``
    methods raise once *after* a durable fake operation, modelling a process death
    before SQLite receives the result. Reusing this fake across application-store
    reopenings represents the persistent state a real manager must retain.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._current: dict[str, WorkspaceSnapshot] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._checkpoints: dict[tuple[str, str], WorkspaceSnapshot] = {}
        self._checkpoint_inputs: dict[tuple[str, str], WorkspaceSnapshot] = {}
        self._restore_inputs: dict[tuple[str, str], list[WorkspaceSnapshot]] = {}
        self._checkpoint_crash = False
        self._restore_crash = False
        self.checkpoint_count = 0
        self.restore_count = 0

    @_locked
    def register(self, snapshot: WorkspaceSnapshot,
                 record: Mapping[str, Any] | None = None) -> None:
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise ValueError("Expected WorkspaceSnapshot")
        if snapshot.workspace_id in self._current:
            raise ValueError("Workspace is already registered")
        if record is not None:
            self._validate_record(record, snapshot)
            self._records[snapshot.workspace_id] = {
                key: record.get(key) for key in ("repository_path", "worktree_path", "base_ref")
            }
        self._current[snapshot.workspace_id] = snapshot

    @_locked
    def set_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise ValueError("Expected WorkspaceSnapshot")
        current = self._current.get(snapshot.workspace_id)
        if current is None or snapshot.task_id != current.task_id:
            raise ForgePilotError("workspace_corrupted", "Unknown workspace identity")
        self._current[snapshot.workspace_id] = snapshot

    @_locked
    def fail_after_next_checkpoint(self) -> None:
        self._checkpoint_crash = True

    @_locked
    def fail_after_next_restore(self) -> None:
        self._restore_crash = True

    @staticmethod
    def _validate_record(record: Mapping[str, Any], snapshot: WorkspaceSnapshot) -> None:
        if (not isinstance(record, Mapping)
                or record.get("workspace_id") != snapshot.workspace_id
                or record.get("task_id") != snapshot.task_id
                or record.get("released_at") is not None
                or not isinstance(record.get("status", "active"), str)
                or record.get("status") in {"released", "deleted", "corrupted", "unknown"}):
            raise ForgePilotError("workspace_corrupted", "Worktree record does not match its owner")

    def _registered(self, record: Mapping[str, Any]) -> WorkspaceSnapshot:
        if not isinstance(record, Mapping) or not isinstance(record.get("workspace_id"), str):
            raise ForgePilotError("workspace_corrupted", "Invalid worktree record")
        snapshot = self._current.get(record.get("workspace_id"))
        if snapshot is None:
            raise ForgePilotError("workspace_corrupted", "Worktree is not registered")
        self._validate_record(record, snapshot)
        bound = self._records.get(snapshot.workspace_id)
        if bound is not None and any(record.get(key) != value for key, value in bound.items()):
            raise ForgePilotError("workspace_corrupted", "Worktree location or base ref changed")
        return snapshot

    @staticmethod
    def _known(snapshot: WorkspaceSnapshot) -> None:
        if not snapshot.known:
            raise ForgePilotError("workspace_corrupted", "Worktree state cannot be verified")

    @staticmethod
    def _key(snapshot: WorkspaceSnapshot, checkpoint_id: str) -> tuple[str, str]:
        _identifier(checkpoint_id, "checkpoint ID")
        return snapshot.workspace_id, checkpoint_id

    @_locked
    def inspect(self, record: Mapping[str, Any]) -> WorkspaceSnapshot:
        # An unknown snapshot is returned explicitly so recovery can report the
        # failed check; mutations always reject it, even if expected matches.
        return self._registered(record)

    @_locked
    def checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                   expected: WorkspaceSnapshot) -> WorkspaceSnapshot:
        current = self._registered(record)
        self._known(current)
        key = self._key(current, checkpoint_id)
        saved = self._checkpoints.get(key)
        if saved is not None:
            if (current != saved
                    or expected not in (self._checkpoint_inputs[key], saved)):
                raise ForgePilotError("workspace_corrupted", "Checkpoint request no longer matches worktree")
            return saved
        if current != expected:
            raise ForgePilotError("workspace_corrupted", "Worktree changed before checkpoint")
        payload = json.dumps({
            "workspace_id": current.workspace_id, "task_id": current.task_id,
            "checkpoint_id": checkpoint_id, "parent": current.head_commit,
            "files": dict(current.file_hashes),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        result = WorkspaceSnapshot(current.workspace_id, current.task_id,
                                   sha256(b"fake-worktree-checkpoint\0" + payload).hexdigest(),
                                   current.file_hashes)
        self._checkpoint_inputs[key] = current
        self._checkpoints[key] = result
        self._current[current.workspace_id] = result
        self.checkpoint_count += 1
        if self._checkpoint_crash:
            self._checkpoint_crash = False
            raise SimulatedWorkspaceCrash("Process interrupted after checkpoint commit")
        return result

    @_locked
    def restore(self, record: Mapping[str, Any], checkpoint_id: str, *,
                expected: WorkspaceSnapshot) -> WorkspaceSnapshot:
        current = self._registered(record)
        self._known(current)
        key = self._key(current, checkpoint_id)
        saved = self._checkpoints.get(key)
        if saved is None:
            raise ForgePilotError("workspace_corrupted", "Safe checkpoint does not exist")
        prior_inputs = self._restore_inputs.setdefault(key, [])
        if current == saved and (expected == current or expected in prior_inputs):
            return saved
        if current != expected:
            raise ForgePilotError("workspace_corrupted", "Worktree changed before restore")
        self._known(expected)
        self._current[current.workspace_id] = saved
        prior_inputs.append(expected)
        self.restore_count += 1
        if self._restore_crash:
            self._restore_crash = False
            raise SimulatedWorkspaceCrash("Process interrupted after checkpoint restore")
        return saved

    @_locked
    def verify_checkpoint(self, record: Mapping[str, Any], checkpoint_id: str,
                          head_commit: str, file_hashes: Mapping[str, str | None]) -> None:
        current = self._registered(record)
        key = self._key(current, checkpoint_id)
        saved = self._checkpoints.get(key)
        expected = WorkspaceSnapshot(current.workspace_id, current.task_id,
                                     head_commit, file_hashes)
        if saved is None or saved != expected:
            raise ForgePilotError("workspace_corrupted", "Safe checkpoint HEAD or file manifest does not match")


__all__ = [
    "SimulatedWorkspaceCrash", "StrictFakeWorkspaceBackend", "UnavailableWorkspaceBackend",
    "WorkspaceBackend", "WorkspaceSnapshot",
]
