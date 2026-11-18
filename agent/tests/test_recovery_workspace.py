"""Strict fake contract for the future task-isolated WorktreeManager."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.workspace import (
    SimulatedWorkspaceCrash, StrictFakeWorkspaceBackend, UnavailableWorkspaceBackend,
    WorkspaceBackend, WorkspaceSnapshot,
)


HEAD = "a" * 40
HASH = sha256(b"safe").hexdigest()
CHANGED = sha256(b"partial writer output").hexdigest()


def record():
    return {
        "workspace_id": "worktree-1", "task_id": "task-1", "head_commit": HEAD,
        "repository_path": "D:/source", "worktree_path": "D:/isolated/task-1",
        "base_ref": HEAD, "status": "active", "released_at": None,
    }


def registered():
    backend = StrictFakeWorkspaceBackend()
    original = WorkspaceSnapshot("worktree-1", "task-1", HEAD, {"src/main.py": HASH})
    backend.register(original, record())
    return backend, original


def test_snapshot_owns_an_immutable_complete_manifest():
    files = {"src/main.py": HASH, "removed.py": None}
    snapshot = WorkspaceSnapshot("w", "t", HEAD, files)
    files["src/main.py"] = CHANGED
    assert snapshot.file_hashes == {"src/main.py": HASH, "removed.py": None}
    with pytest.raises(TypeError):
        snapshot.file_hashes["src/main.py"] = CHANGED
    with pytest.raises(FrozenInstanceError):
        snapshot.known = False


@pytest.mark.parametrize("path", [
    "", "/root.py", "../file.py", "a/../file.py", "a/./b.py", "a//b.py",
    "a\\b.py", "C:/file.py", ".git/HEAD", "a/.GiT/config", "name\0.py",
    "name\n.py", "name .", "name.", "con", "AUX.txt", "a/LPT9.bin", "file:stream",
])
def test_snapshot_rejects_ambiguous_or_escaping_paths(path):
    with pytest.raises(ValueError, match="path"):
        WorkspaceSnapshot("w", "t", HEAD, {path: HASH})


@pytest.mark.parametrize("files", [
    {"A.py": HASH, "a.py": HASH}, {"a": HASH, "a/b": HASH},
    {"file": "x" * 64}, {"file": "A" * 64}, {"file": 1},
])
def test_snapshot_rejects_invalid_manifests(files):
    with pytest.raises(ValueError):
        WorkspaceSnapshot("w", "t", HEAD, files)


@pytest.mark.parametrize("head", ["main", "", "a" * 39, "A" * 40, None])
def test_known_snapshot_requires_a_verified_git_object_id(head):
    with pytest.raises(ValueError):
        WorkspaceSnapshot("w", "t", head, {})


def test_unknown_snapshot_can_have_no_head_and_never_authorizes_writes():
    backend, original = registered()
    backend.checkpoint(record(), "safe", original)
    unknown = replace(original, known=False, head_commit=None)
    backend.set_snapshot(unknown)
    assert backend.inspect(record()) == unknown
    for operation in (
        lambda: backend.checkpoint(record(), "new", unknown),
        lambda: backend.restore(record(), "safe", expected=unknown),
    ):
        with pytest.raises(ForgePilotError, match="workspace_corrupted"):
            operation()
    assert backend.restore_count == 0
    assert backend.checkpoint_count == 1


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "other"), ("task_id", "other"),
    ("repository_path", "D:/other"), ("worktree_path", "D:/other"),
    ("base_ref", "b" * 40), ("status", "released"), ("status", "unknown"),
    ("released_at", "2026-10-25T00:00:00Z"),
])
def test_fake_binds_worktree_record_and_task_identity(field, value):
    backend, original = registered()
    invalid = {**record(), field: value}
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.inspect(invalid)
    assert backend.inspect(record()) == original


@pytest.mark.parametrize("invalid", [None, {}, {"workspace_id": []}])
def test_fake_rejects_malformed_records(invalid):
    backend, _ = registered()
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.inspect(invalid)


@pytest.mark.parametrize("change", [
    {"head_commit": "b" * 40},
    {"file_hashes": {"src/main.py": CHANGED}},
    {"file_hashes": {"src/main.py": HASH, "new.py": HASH}},
    {"file_hashes": {}},
])
def test_checkpoint_rejects_head_edit_addition_and_deletion_races(change):
    backend, original = registered()
    backend.set_snapshot(replace(original, **change))
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.checkpoint(record(), "safe", original)
    assert backend.checkpoint_count == 0


def test_checkpoint_is_idempotent_across_commit_before_sqlite_crash():
    backend, original = registered()
    backend.fail_after_next_checkpoint()
    with pytest.raises(SimulatedWorkspaceCrash):
        backend.checkpoint(record(), "safe", original)
    committed = backend.inspect(record())
    assert committed.head_commit != original.head_commit
    assert committed.file_hashes == original.file_hashes
    assert backend.checkpoint(record(), "safe", original) == committed
    assert backend.checkpoint(record(), "safe", committed) == committed
    assert backend.checkpoint_count == 1


def test_reusing_checkpoint_id_does_not_commit_later_edits():
    backend, original = registered()
    committed = backend.checkpoint(record(), "safe", original)
    changed = replace(committed, file_hashes={"src/main.py": CHANGED})
    backend.set_snapshot(changed)
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.checkpoint(record(), "safe", changed)
    assert backend.inspect(record()) == changed
    assert backend.checkpoint_count == 1


def test_checkpoint_verification_is_read_only_and_independent_of_current_tree():
    backend, original = registered()
    safe = backend.checkpoint(record(), "safe", original)
    changed = replace(safe, head_commit="b" * 40, file_hashes={"new.py": CHANGED})
    backend.set_snapshot(changed)
    backend.verify_checkpoint(record(), "safe", safe.head_commit, safe.file_hashes)
    assert backend.inspect(record()) == changed
    for checkpoint_id, head, files in (
        ("unknown", safe.head_commit, safe.file_hashes),
        ("safe", changed.head_commit, safe.file_hashes),
        ("safe", safe.head_commit, changed.file_hashes),
    ):
        with pytest.raises(ForgePilotError, match="workspace_corrupted"):
            backend.verify_checkpoint(record(), checkpoint_id, head, files)
    assert backend.restore_count == 0


def test_restore_rolls_back_complete_manifest_once_after_uncertain_outcome():
    backend, original = registered()
    safe = backend.checkpoint(record(), "safe", original)
    partial = replace(safe, file_hashes={"partial.py": CHANGED, "src/main.py": None})
    backend.set_snapshot(partial)
    backend.fail_after_next_restore()
    with pytest.raises(SimulatedWorkspaceCrash):
        backend.restore(record(), "safe", expected=partial)
    assert backend.inspect(record()) == safe
    assert backend.restore(record(), "safe", expected=partial) == safe
    assert backend.restore(record(), "safe", expected=safe) == safe
    assert backend.restore_count == 1
    assert backend.checkpoint_count == 1


def test_restore_refuses_stale_expected_snapshot_and_missing_checkpoint():
    backend, original = registered()
    safe = backend.checkpoint(record(), "safe", original)
    changed = replace(safe, file_hashes={"src/main.py": CHANGED})
    backend.set_snapshot(changed)
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.restore(record(), "safe", expected=safe)
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.restore(record(), "missing", expected=changed)
    assert backend.inspect(record()) == changed
    assert backend.restore_count == 0


def test_restore_never_crosses_workspace_or_task_identity():
    backend, original = registered()
    backend.checkpoint(record(), "safe", original)
    other = WorkspaceSnapshot("worktree-2", "task-2", HEAD, {})
    backend.register(other)
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        backend.restore({"workspace_id": "worktree-2", "task_id": "task-2"},
                        "safe", expected=other)
    assert backend.inspect(record()).task_id == "task-1"


def test_unavailable_backend_always_fails_closed():
    backend = UnavailableWorkspaceBackend()
    original = WorkspaceSnapshot("worktree-1", "task-1", HEAD, {})
    assert isinstance(backend, WorkspaceBackend)
    assert isinstance(StrictFakeWorkspaceBackend(), WorkspaceBackend)
    for operation in (
        lambda: backend.inspect(record()),
        lambda: backend.checkpoint(record(), "safe", original),
        lambda: backend.restore(record(), "safe", expected=original),
        lambda: backend.verify_checkpoint(record(), "safe", HEAD, {}),
    ):
        with pytest.raises(ForgePilotError, match="workspace_unavailable"):
            operation()
