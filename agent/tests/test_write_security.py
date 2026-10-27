"""Security outcomes through real Windows handles, not mocked pathname checks."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from forgepilot.changes import ChangeSet
from forgepilot.errors import ForgePilotError
import forgepilot.file_access as access
from forgepilot.repository import Repository
from forgepilot.trace import MemoryTrace


def sha(data):
    return hashlib.sha256(data).hexdigest()


def changes_at(root, approved=("main.cpp", "new.cpp")):
    repo = Repository(root.resolve(), "unused")
    def authorize(path):
        if path not in approved:
            raise ForgePilotError("approval_required", "Outside approved scope")
    return ChangeSet(repo, authorize, MemoryTrace(), "security")


@pytest.mark.parametrize("path", [
    "", "a\0.cpp", "a\n.cpp", "C:/source.cpp", "C:relative.cpp", "/source.cpp",
    "\\source.cpp", "\\\\host\\share\\source.cpp", "\\\\?\\C:\\x.cpp",
    "\\\\.\\GLOBALROOT\\x", "main.cpp:hidden", "main.cpp::$DATA", "a/../main.cpp",
    "a\\..\\main.cpp", "a.../../../x", ".git/config", ".GIT/config", "a/.git/HEAD",
    "Oracle/answers.cpp", "protected-oracle/test.cpp", ".hidden-tests/x", "a/file. ",
    "a/file.", "CON", "NUL.txt", "COM1.cpp", "LPT9", "a|b", "a?b", "a*b",
    "x" * 1025, None,
])
def test_every_path_operation_uses_windows_boundary(tmp_path, path):
    repo = Repository(tmp_path.resolve(), "unused")
    for operation in (lambda: repo._path(path, allow_missing=True), lambda: repo.normalize_plan_path(path)):
        with pytest.raises(ForgePilotError, match="repository_"):
            operation()


@pytest.mark.asyncio
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_explicit_patch_hash_atomic_replace_and_exact_restore(tmp_path, bom, newline):
    target = tmp_path / "main.cpp"
    original = bom + b"int value = 1;" + newline
    target.write_bytes(original)
    changes = changes_at(tmp_path)
    changes.capture_scope()
    inode = target.stat().st_ino
    result = json.loads(await changes.apply_patch(".\\main.cpp", sha(original), [
        {"old_text": "value = 1;\n", "new_text": "value = 2;\n"}]))
    expected = original.replace(b"= 1", b"= 2")
    assert target.read_bytes() == expected and target.stat().st_ino != inode
    assert result["before_sha256"] == sha(original) and result["after_sha256"] == sha(expected)
    assert changes.verify_scope() is None
    assert (await changes.restore())["conflicts"] == 0 and target.read_bytes() == original
    assert not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0" * 64, "BAD", None, "F" * 64])
async def test_patch_cannot_supply_stale_or_invalid_hash(tmp_path, bad):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    with pytest.raises(ForgePilotError):
        await changes.apply_patch("main.cpp", bad, [{"old_text": "old", "new_text": "new"}])
    assert target.read_bytes() == b"old" and not changes.snapshots


@pytest.mark.asyncio
async def test_new_file_tools_never_delete_or_replace_user_files(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"user")
    changes = changes_at(tmp_path)
    changes.capture_scope()
    with pytest.raises(ForgePilotError, match="create_conflict"):
        await changes.create_file("main.cpp", "override")
    with pytest.raises(ForgePilotError, match="delete_denied"):
        await changes.delete_new_file("main.cpp", sha(b"user"))
    result = json.loads(await changes.create_file("new.cpp", "new"))
    with pytest.raises(ForgePilotError, match="write_conflict"):
        await changes.delete_new_file("new.cpp", "0" * 64)
    await changes.delete_new_file("new.cpp", result["after_sha256"])
    assert target.read_bytes() == b"user" and not (tmp_path / "new.cpp").exists()
    assert changes.diff()["files"][0]["deleted"]
    assert (await changes.restore())["conflicts"] == 0
    with pytest.raises(ForgePilotError, match="delete_denied"):
        await changes.delete_new_file("new.cpp", sha(b"new"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "corrupt", "fsync"])
async def test_staging_failure_preserves_destination_and_cleans_temp(tmp_path, monkeypatch, failure):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    changes.capture_scope()
    if failure == "fsync":
        monkeypatch.setattr(access.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    else:
        def writer(stream, data):
            stream.write(data[:1])
            if failure == "partial":
                raise OSError("partial staging")
        monkeypatch.setattr(access, "write_staged_bytes", writer)
    with pytest.raises(ForgePilotError, match="file_access|patch_integrity"):
        await changes.apply_patch("main.cpp", sha(b"old"), [{"old_text": "old", "new_text": "new"}])
    assert target.read_bytes() == b"old" and not changes.snapshots
    assert sorted(path.name for path in tmp_path.iterdir()) == ["main.cpp"]


@pytest.mark.asyncio
async def test_destination_rebind_during_staging_never_overwrites_new_user_file(tmp_path, monkeypatch):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    writer = access.write_staged_bytes
    blocked = []
    def race(stream, data):
        with pytest.raises(OSError):
            target.rename(tmp_path / "moved.cpp")
        with pytest.raises(OSError):
            target.write_bytes(b"racing user")
        blocked.append(True)
        writer(stream, data)
    monkeypatch.setattr(access, "write_staged_bytes", race)
    await changes.apply_patch("main.cpp", sha(b"old"), [{"old_text": "old", "new_text": "new"}])
    assert blocked and target.read_bytes() == b"new" and not (tmp_path / "moved.cpp").exists()
    assert changes.snapshots and not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
async def test_after_patch_scope_scan_detects_unrecorded_changes(tmp_path, monkeypatch):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    other = tmp_path / "other.cpp"
    other.write_bytes(b"unapproved")
    changes = changes_at(tmp_path)
    changes.capture_scope()
    writer = access.write_staged_bytes
    def bypass(stream, data):
        other.write_bytes(b"tampered")
        writer(stream, data)
    monkeypatch.setattr(access, "write_staged_bytes", bypass)
    with pytest.raises(ForgePilotError, match="scope_violation"):
        await changes.apply_patch("main.cpp", sha(b"old"), [{"old_text": "old", "new_text": "new"}])
    assert target.read_bytes() == b"new" and changes.snapshots["main.cpp"].last_written == b"new"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,content", [("main.py", "def invalid(:"), ("main.json", "{bad}")])
async def test_patch_syntax_is_checked_before_replacement(tmp_path, path, content):
    original = b"value = 1" if path.endswith(".py") else b'{"value": 1}'
    target = tmp_path / path
    target.write_bytes(original)
    changes = changes_at(tmp_path, (path,))
    with pytest.raises(ForgePilotError, match="patch_syntax"):
        await changes.apply_patch(path, sha(original), [{"old_text": original.decode(), "new_text": content}])
    assert target.read_bytes() == original and not changes.snapshots


def test_source_read_refuses_hardlinks(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"user")
    os.link(target, tmp_path / "alias.cpp")
    repo = Repository(tmp_path.resolve(), "unused")
    with pytest.raises(ForgePilotError, match="repository_boundary"):
        repo._read("main.cpp")


@pytest.mark.parametrize("operation", ["checkpoint_commit", "deliver"])
@pytest.mark.parametrize("role", ["orchestrator", "analyst", "implementer", "reviewer", "validator"])
def test_only_workspace_manager_can_commit_or_deliver(tmp_path, git_exe, operation, role):
    from forgepilot.policy import Principal
    from forgepilot.workspace_manager import WorkspaceManager
    manager = WorkspaceManager(tmp_path / "manager", git_executable=git_exe, principal=Principal(role))
    with pytest.raises(ForgePilotError, match="policy_denied"):
        if operation == "deliver":
            manager.deliver("task", "patch", approved=True, token="none")
        else:
            manager.checkpoint_commit("task", kind="implementation", expected=None)
