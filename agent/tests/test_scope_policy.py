"""Fail-closed path and approved-workspace boundaries using real Windows files."""

from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

import forgepilot.changes as change_module
import forgepilot.file_access as access
import forgepilot.repository as repository_module
from forgepilot.changes import ChangeSet, FileSnapshot
from forgepilot.errors import ForgePilotError
from forgepilot.repository import Repository
from forgepilot.trace import MemoryTrace


def sha(value):
    return hashlib.sha256(value).hexdigest()


def changes_for(root, *, approved=("main.cpp", "new.cpp"), authorize=None):
    repository = Repository(root.resolve(), "unused")
    def scope(path):
        if path.casefold() not in {item.casefold() for item in approved}:
            raise ForgePilotError("approval_required", "Path outside exact approved scope")
    return ChangeSet(repository, authorize or scope, MemoryTrace(), "scope-test")


def junction(path, destination):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(destination)],
                   check=True, capture_output=True)


@pytest.mark.parametrize("path", [None, "", "a\0.cpp", "D:/absolute.cpp", "\\\\host\\share\\x",
    "/absolute.cpp", "file.cpp:stream", "../outside.cpp", ".git/config", "Oracle/answer.cpp",
    "file. ", "CON", "a|b", "a?b", "a*b", "x" * 1025])
def test_every_untrusted_path_form_is_denied_before_access(tmp_path, path):
    repo = Repository(tmp_path.resolve(), "unused")
    with pytest.raises(ForgePilotError, match="repository_"):
        repo._path(path, allow_missing=True)


def test_existing_directory_junction_is_denied_before_canonical_access(tmp_path):
    root, outside = tmp_path / "work", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    junction(root / "link", outside)
    try:
        with pytest.raises(ForgePilotError, match="Links and reparse points"):
            Repository(root.resolve(), "unused")._path("link")
    finally:
        os.rmdir(root / "link")


@pytest.mark.parametrize("protected", [False, True])
def test_final_realpath_rejects_directory_swap_after_lstat(tmp_path, monkeypatch, protected):
    root = tmp_path / "work"
    root.mkdir()
    candidate = root / "mutable"
    candidate.mkdir()
    destination = (root / ".git") if protected else (tmp_path / "outside")
    destination.mkdir()
    repo = Repository(root.resolve(), "unused")
    original = Path.lstat
    swapped = False
    def race(path):
        nonlocal swapped
        result = original(path)
        if path == candidate and not swapped:
            swapped = True
            candidate.rename(root / "old-directory")
            junction(candidate, destination)
        return result
    monkeypatch.setattr(Path, "lstat", race)
    try:
        with pytest.raises(ForgePilotError, match="Canonical path is excluded|outside the repository"):
            repo._path("mutable")
        assert swapped
    finally:
        # Remove the junction itself; never recurse into its destination.
        if swapped:
            os.rmdir(candidate)
    assert destination.exists()


def test_path_identity_replacement_and_normalization_fail_closed(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    repo = Repository(root.resolve(), "unused")
    (root / "nested").mkdir()
    assert repo.normalize_plan_path(".\\nested//./new.cpp") == "nested/new.cpp"
    with pytest.raises(ForgePilotError, match="not a directory"):
        repo.normalize_plan_path("nested")
    with pytest.raises(ForgePilotError, match="repository_path"):
        repo._path("missing.cpp")
    root.rename(tmp_path / "original")
    root.mkdir()
    with pytest.raises(ForgePilotError, match="root changed"):
        repo.normalize_plan_path("new.cpp")


def test_normalization_rechecks_inaccessible_canonical_path(tmp_path, monkeypatch):
    repo = Repository(tmp_path.resolve(), "unused")
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    boundary = repo._path
    ready = False
    def checked(*args, **kwargs):
        nonlocal ready
        result = boundary(*args, **kwargs)
        ready = True
        return result
    original = Path.resolve
    def inaccessible(path, *args, **kwargs):
        if ready and path == target:
            raise PermissionError("File system boundary became inaccessible")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(repo, "_path", checked)
    monkeypatch.setattr(Path, "resolve", inaccessible)
    with pytest.raises(ForgePilotError, match="Cannot normalize"):
        repo.normalize_plan_path("main.cpp")


def test_approval_manifest_has_complete_hashes_and_omits_protected_directories(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/二进制.bin").write_bytes(b"\0\xfffixture")
    (tmp_path / "main.cpp").write_bytes(b"original\r\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("private", encoding="utf-8")
    repo = Repository(tmp_path.resolve(), "unused")
    assert repo.audit_manifest() == {"main.cpp": sha(b"original\r\n"), "nested/二进制.bin": sha(b"\0\xfffixture")}


@pytest.mark.parametrize("limit", ["MAX_SCAN_ENTRIES", "MAX_FILES", "MAX_SEARCH_BYTES"])
def test_approval_manifest_quantity_and_size_limits_never_truncate(tmp_path, monkeypatch, limit):
    (tmp_path / "main.cpp").write_bytes(b"original")
    repo = Repository(tmp_path.resolve(), "unused")
    monkeypatch.setattr(repository_module, limit, 0)
    with pytest.raises(ForgePilotError, match="scope_limit"):
        repo.audit_manifest()


@pytest.mark.parametrize("target_type", ["hardlink", "junction"])
def test_approval_manifest_rejects_file_aliases(tmp_path, target_type):
    root = tmp_path / "work"
    root.mkdir()
    target = root / "main.cpp"
    target.write_bytes(b"original")
    if target_type == "hardlink":
        os.link(target, root / "alias.cpp")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        junction(root / "alias", outside)
    try:
        with pytest.raises(ForgePilotError, match="repository_boundary"):
            Repository(root.resolve(), "unused").audit_manifest()
    finally:
        if target_type == "junction":
            os.rmdir(root / "alias")


def test_approval_manifest_inaccessible_scan_fails_closed(tmp_path, monkeypatch):
    repo = Repository(tmp_path.resolve(), "unused")
    def inaccessible(_):
        raise PermissionError("Directory cannot be read")
    monkeypatch.setattr(repository_module.os, "scandir", inaccessible)
    with pytest.raises(ForgePilotError, match="Cannot finish approval"):
        repo.audit_manifest()


def test_approval_manifest_rejects_special_files(tmp_path, monkeypatch):
    repo = Repository(tmp_path.resolve(), "unused")
    candidate = tmp_path / "device"
    candidate.write_bytes(b"")
    original = repository_module.os.scandir
    class SpecialEntry:
        name = "device"
        path = str(candidate)
        def stat(self, **_):
            # Windows does not provide mkfifo; emulate only the OS type metadata.
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0)
    @contextmanager
    def scan(path):
        if path == tmp_path:
            yield [SpecialEntry()]
        else:
            with original(path) as entries:
                yield entries
    monkeypatch.setattr(repository_module.os, "scandir", scan)
    with pytest.raises(ForgePilotError, match="Only ordinary files"):
        repo.audit_manifest()


def test_scope_reapproval_does_not_refresh_an_unrecorded_file(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    with pytest.raises(ForgePilotError, match="scope_unapproved"):
        changes.verify_scope()
    changes.capture_scope()
    changes.capture_scope()
    target.write_bytes(b"unrecorded")
    with pytest.raises(ForgePilotError, match="scope_violation"):
        changes.capture_scope()
    assert changes._scope_baseline["main.cpp"] == sha(b"original")


def test_prepared_unwritten_snapshot_does_not_change_expected_scope(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    changes.capture_scope()
    # A snapshot prepared before atomic commit is never evidence of a completed write.
    changes.snapshots["main.cpp"] = FileSnapshot("main.cpp", b"original")
    changes.verify_scope()
    target.write_bytes(b"outside committed patches")
    with pytest.raises(ForgePilotError, match="scope_violation"):
        changes.verify_scope()


@pytest.mark.asyncio
async def test_scope_tracks_only_actual_recorded_created_deleted_and_restored_bytes(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    changes.capture_scope()
    await changes.apply_patch("main.cpp", sha(b"original"), [{"old_text": "original", "new_text": "patched"}])
    created = json.loads(await changes.create_file("new.cpp", "created"))
    changes.verify_scope()
    await changes.delete_new_file("new.cpp", created["after_sha256"])
    changes.verify_scope()
    assert (await changes.restore())["conflicts"] == 0
    changes.verify_scope()
    assert target.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_unapproved_path_or_revoked_authority_cannot_create_or_patch(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    with pytest.raises(ForgePilotError, match="approval_required"):
        await changes.create_file("outside.cpp", "forbidden")
    assert not (tmp_path / "outside.cpp").exists()
    calls = 0
    def revoking(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ForgePilotError("approval_required", "Grant revoked before mutation")
    changes = changes_for(tmp_path, authorize=revoking)
    with pytest.raises(ForgePilotError, match="Grant revoked"):
        await changes.create_file("new.cpp", "forbidden")
    assert not (tmp_path / "new.cpp").exists() and not changes.snapshots
    assert target.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_patch_api_requires_cas_even_after_read(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    await changes.repository.read_file("main.cpp")
    for value in [None, True, "0" * 63, "F" * 64, "z" * 64]:
        with pytest.raises(ForgePilotError, match="expected_hash"):
            await changes.apply_patch("main.cpp", value, [{"old_text": "original", "new_text": "patched"}])
    assert target.read_bytes() == b"original" and not changes.snapshots


@pytest.mark.asyncio
async def test_changed_file_count_limit_preserves_last_unmodified_file(tmp_path):
    changes = changes_for(tmp_path, approved=tuple(f"file-{index}.cpp" for index in range(101)))
    for index in range(100):
        await changes.create_file(f"file-{index}.cpp", "created")
    with pytest.raises(ForgePilotError, match="100 file snapshots"):
        await changes.create_file("file-100.cpp", "beyond scope capacity")
    assert len(changes.snapshots) == 100 and not (tmp_path / "file-100.cpp").exists()


@pytest.mark.asyncio
async def test_total_original_size_limit_preserves_next_cas_target(tmp_path, monkeypatch):
    (tmp_path / "main.cpp").write_bytes(b"first-file")
    (tmp_path / "new.cpp").write_bytes(b"second-file")
    changes = changes_for(tmp_path)
    monkeypatch.setattr(change_module, "MAX_TOTAL_SNAPSHOT_BYTES", 15)
    await changes.apply_patch("main.cpp", sha(b"first-file"), [{"old_text": "first", "new_text": "fixed"}])
    with pytest.raises(ForgePilotError, match="Original snapshots"):
        await changes.apply_patch("new.cpp", sha(b"second-file"), [{"old_text": "second", "new_text": "fixed"}])
    assert len(changes.snapshots) == 1 and (tmp_path / "new.cpp").read_bytes() == b"second-file"


@pytest.mark.asyncio
@pytest.mark.parametrize("edits", [None, [], [{}], [{"old_text": "", "new_text": "new"}],
    [{"old_text": 1, "new_text": "new"}], [{"old_text": "original", "new_text": None}],
    [{"old_text": "original", "new_text": "new\0"}],
    [{"old_text": "original", "new_text": "new", "approved": True}]])
async def test_direct_patch_arguments_are_strict_before_open(tmp_path, edits):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    with pytest.raises(ForgePilotError, match="invalid_arguments"):
        await changes.apply_patch("main.cpp", sha(b"original"), edits)
    assert target.read_bytes() == b"original" and not changes.snapshots


@pytest.mark.asyncio
async def test_write_byte_limit_and_nul_preserve_original(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"original")
    changes = changes_for(tmp_path)
    for text, code in [("\0", "repository_binary"), ("x" * (change_module.MAX_SNAPSHOT_BYTES + 1), "write_limit")]:
        with pytest.raises(ForgePilotError, match=code):
            await changes.write_file("main.cpp", text)
    assert target.read_bytes() == b"original" and not changes.snapshots


@pytest.mark.asyncio
async def test_patch_result_cannot_be_invalid_utf8_or_nul(tmp_path):
    (tmp_path / "main.cpp").write_bytes(b"original")
    changes = changes_for(tmp_path)
    for invalid in (b"\xff", b"\0"):
        with pytest.raises(ForgePilotError, match="Patch must produce complete UTF-8"):
            changes._mutate_file("main.cpp", lambda _: invalid, allow_create=False, expected_hash=sha(b"original"))
    assert not changes.snapshots and (tmp_path / "main.cpp").read_bytes() == b"original"
