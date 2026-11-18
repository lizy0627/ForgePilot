"""Transactional leaf locks, rollback, and committed-patch recovery evidence."""
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from forgepilot.changes import ChangeSet
from forgepilot.errors import ForgePilotError
import forgepilot.file_access as access
from forgepilot.repository import Repository
from forgepilot.trace import MemoryTrace
import forgepilot.windows_transaction as transactions


def sha(data):
    return hashlib.sha256(data).hexdigest()


def changes_at(root):
    return ChangeSet(Repository(root.resolve(), "unused"), lambda path: None, MemoryTrace(), "txf-security")


async def patch(changes):
    return await changes.apply_patch("main.cpp", sha(b"old"), [{"old_text": "old", "new_text": "new"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stage", "rename", "commit"])
async def test_transaction_blocks_leaf_rename_and_writes_through_commit(tmp_path, monkeypatch, phase):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    attempts = []
    def probe():
        for operation in (lambda: target.rename(tmp_path / "racing.cpp"),
                          lambda: target.write_bytes(b"racing user")):
            with pytest.raises(OSError):
                operation()
            attempts.append(True)
    if phase == "stage":
        actual = access.write_staged_bytes
        def stage(stream, data):
            probe()
            actual(stream, data)
        monkeypatch.setattr(access, "write_staged_bytes", stage)
    else:
        actual = getattr(transactions.FileTransaction, "replace" if phase == "rename" else "commit")
        def guarded(self, *args):
            probe()
            return actual(self, *args)
        monkeypatch.setattr(transactions.FileTransaction, "replace" if phase == "rename" else "commit", guarded)
    changes = changes_at(tmp_path)
    await patch(changes)
    assert attempts == [True, True] and target.read_bytes() == b"new"
    assert not (tmp_path / "racing.cpp").exists()
    assert not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["open", "rename", "commit"])
@pytest.mark.parametrize("created", [False, True])
async def test_transaction_failures_roll_back_without_visible_partial_files(tmp_path, monkeypatch, failure, created):
    target = tmp_path / "main.cpp"
    if not created:
        target.write_bytes(b"old")
    method = {"open": "open_handle", "rename": "replace", "commit": "commit"}[failure]
    def fail(*args, **kwargs):
        raise OSError(5, "transaction fault")
    monkeypatch.setattr(transactions.FileTransaction, method, fail)
    changes = changes_at(tmp_path)
    with pytest.raises(ForgePilotError, match="file_access"):
        if created:
            await changes.create_file("main.cpp", "new")
        else:
            await patch(changes)
    assert (target.read_bytes() if target.exists() else None) == (None if created else b"old")
    assert not changes.snapshots and not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["type", "link", "hardlink", "path"])
async def test_unsafe_staging_handle_cannot_commit(tmp_path, monkeypatch, fault):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    original_stat = access.os.fstat
    original_path = access.opened_path
    def unsafe_stat(fd):
        result = original_stat(fd)
        if ".forgepilot-patch-" not in str(original_path(fd)):
            return result
        return SimpleNamespace(st_dev=result.st_dev, st_ino=result.st_ino,
            st_mode=stat.S_IFDIR if fault == "type" else result.st_mode,
            st_nlink=2 if fault == "hardlink" else result.st_nlink,
            st_file_attributes=0x400 if fault == "link" else 0)
    def unsafe_path(fd):
        path = original_path(fd)
        return tmp_path / "outside.cpp" if fault == "path" and ".forgepilot-patch-" in str(path) else path
    monkeypatch.setattr(access.os, "fstat", unsafe_stat)
    monkeypatch.setattr(access, "opened_path", unsafe_path)
    changes = changes_at(tmp_path)
    with pytest.raises(ForgePilotError, match="repository_boundary"):
        await patch(changes)
    assert target.read_bytes() == b"old" and not changes.snapshots
    assert not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [False, True])
async def test_postcommit_read_failure_retains_exact_snapshot_for_restore(tmp_path, monkeypatch, created):
    target = tmp_path / "main.cpp"
    if not created:
        target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    @contextmanager
    def unavailable(*args, **kwargs):
        raise ForgePilotError("file_access", "Postcommit read unavailable")
        yield
    with monkeypatch.context() as scope:
        scope.setattr(access, "pinned_file", unavailable)
        with pytest.raises(access.CommittedPatchError):
            if created:
                await changes.create_file("main.cpp", "new")
            else:
                await patch(changes)
    snapshot = changes.snapshots["main.cpp"]
    assert snapshot.original == (None if created else b"old")
    assert snapshot.last_written == target.read_bytes() == b"new"
    result = await changes.restore()
    assert result["conflicts"] == 0
    assert (target.read_bytes() if target.exists() else None) == (None if created else b"old")


@pytest.mark.asyncio
async def test_postcommit_user_edit_is_preserved_and_reported_as_recovery_conflict(tmp_path, monkeypatch):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    actual = access.pinned_file
    @contextmanager
    def user_edit(*args, **kwargs):
        target.write_bytes(b"new user bytes")
        with actual(*args, **kwargs) as result:
            yield result
    with monkeypatch.context() as scope:
        scope.setattr(access, "pinned_file", user_edit)
        with pytest.raises(access.CommittedPatchError):
            await patch(changes)
    assert changes.snapshots["main.cpp"].last_written == b"new"
    assert (await changes.restore())["conflicts"] == 1
    assert target.read_bytes() == b"new user bytes"


def test_ordinary_handle_cannot_bypass_transactional_atomic_replace(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    repo = Repository(tmp_path.resolve(), "unused")
    with access.pinned_file(repo, "main.cpp", writable=True) as (stream, _):
        with pytest.raises(ForgePilotError, match="atomic_unsupported"):
            access.atomic_replace(repo, "main.cpp", stream, b"new", expected_sha256=sha(b"old"))
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("fault", ["cas", "opened_path"])
def test_atomic_rechecks_baseline_and_handle_location_inside_transaction(tmp_path, monkeypatch, fault):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    repo = Repository(tmp_path.resolve(), "unused")
    with access.pinned_file(repo, "main.cpp", writable=True, replaceable=True) as (stream, _):
        if fault == "opened_path":
            original = access.opened_path
            monkeypatch.setattr(access, "opened_path", lambda fd:
                tmp_path / "outside.cpp" if fd == stream.fileno() else original(fd))
        with pytest.raises(ForgePilotError, match="write_conflict|repository_boundary"):
            access.atomic_replace(repo, "main.cpp", stream, b"new",
                                  expected_sha256="0" * 64 if fault == "cas" else sha(b"old"))
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".forgepilot-patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("open_number", [2, 3, 4])
async def test_descriptor_conversion_faults_close_transaction_handles(tmp_path, monkeypatch, open_number):
    import msvcrt
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    original = msvcrt.open_osfhandle
    calls = 0
    def fail_selected(handle, flags):
        nonlocal calls
        calls += 1
        if calls == open_number:
            raise OSError(5, "descriptor conversion failed")
        return original(handle, flags)
    with monkeypatch.context() as scope:
        scope.setattr(msvcrt, "open_osfhandle", fail_selected)
        with pytest.raises(ForgePilotError, match="file_access|patch_integrity"):
            await patch(changes)
    if open_number == 4:
        assert target.read_bytes() == b"new" and changes.snapshots["main.cpp"].last_written == b"new"
        assert (await changes.restore())["conflicts"] == 0
    else:
        assert not changes.snapshots
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".forgepilot-patch-*"))


def test_transaction_refuses_non_windows_platform(monkeypatch):
    with monkeypatch.context() as scope:
        scope.setattr(transactions.os, "name", "posix")
        with pytest.raises(ForgePilotError, match="platform_unsupported"):
            transactions.FileTransaction()
        with pytest.raises(ForgePilotError, match="platform_unsupported"):
            access.kernel()


def test_invalid_native_handle_fails_closed():
    api = SimpleNamespace(CreateFileW=lambda *args: ctypes.c_void_p(-1).value)
    with pytest.raises(OSError):
        access._open(api, "file", 1, 0, 3, 0)


def test_missing_parent_is_never_created_by_a_read(tmp_path):
    repo = Repository(tmp_path.resolve(), "unused")
    with pytest.raises(ForgePilotError, match="repository_path"):
        with access.pinned_file(repo, "absent/main.cpp"):
            pytest.fail("missing parent was accepted")
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("fault", ["ancestor", "type", "link", "hardlink", "path"])
def test_pinned_handles_recheck_metadata_and_resolved_location(tmp_path, monkeypatch, fault):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    repo = Repository(tmp_path.resolve(), "unused")
    actual_stat = access.os.fstat
    if fault == "ancestor":
        monkeypatch.setattr(access, "is_link", lambda info: True)
    elif fault == "path":
        monkeypatch.setattr(access, "opened_path", lambda fd: tmp_path / "elsewhere.cpp")
    else:
        def unsafe(fd):
            info = actual_stat(fd)
            return SimpleNamespace(st_mode=stat.S_IFDIR if fault == "type" else info.st_mode,
                st_file_attributes=0x400 if fault == "link" else 0,
                st_nlink=2 if fault == "hardlink" else info.st_nlink)
        monkeypatch.setattr(access.os, "fstat", unsafe)
    with pytest.raises(ForgePilotError, match="repository_boundary"):
        with access.pinned_file(repo, "main.cpp", writable=True, replaceable=True):
            pytest.fail("unsafe handle was yielded")
    assert target.read_bytes() == b"old"


def test_delete_requires_delete_access_on_the_exact_handle(tmp_path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    with access.pinned_file(Repository(tmp_path.resolve(), "unused"), "main.cpp") as (stream, _):
        with pytest.raises(ForgePilotError, match="restore_io"):
            access.delete_open_file(stream)
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("write_count", [0, 1, 2])
def test_staged_writer_handles_short_or_zero_writes(monkeypatch, write_count):
    output = bytearray()
    fsynced = []
    class Writer:
        def write(self, data):
            length = min(write_count, len(data))
            output.extend(data[:length])
            return length
        def flush(self):
            pass
        def fileno(self):
            return 123
    monkeypatch.setattr(access.os, "fsync", fsynced.append)
    if write_count:
        access.write_staged_bytes(Writer(), b"complete")
        assert output == b"complete" and fsynced == [123]
    else:
        with pytest.raises(OSError, match="Incomplete"):
            access.write_staged_bytes(Writer(), b"complete")
        assert not output and not fsynced


@pytest.mark.asyncio
async def test_transactional_destination_is_verified_before_commit(tmp_path, monkeypatch):
    import msvcrt
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    actual = transactions.FileTransaction.replace
    def corrupt(self, source, destination):
        actual(self, source, destination)
        handle = self.open_handle(destination, 0xC0000000, 0, 3, 0x00200000)
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
        with os.fdopen(fd, "r+b", buffering=0) as stream:
            stream.write(b"bad")
    monkeypatch.setattr(transactions.FileTransaction, "replace", corrupt)
    with pytest.raises(ForgePilotError, match="patch_integrity"):
        await patch(changes)
    assert target.read_bytes() == b"old" and not changes.snapshots


@pytest.mark.asyncio
async def test_postcommit_disappearance_retains_snapshot_and_refuses_unsafe_restore(tmp_path, monkeypatch):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"old")
    changes = changes_at(tmp_path)
    @contextmanager
    def disappeared(*args, **kwargs):
        target.unlink()
        yield None, False
    with monkeypatch.context() as scope:
        scope.setattr(access, "pinned_file", disappeared)
        with pytest.raises(access.CommittedPatchError):
            await patch(changes)
    assert changes.snapshots["main.cpp"].last_written == b"new"
    assert (await changes.restore())["conflicts"] == 1 and not target.exists()


@pytest.mark.parametrize("phase", ["create", "wait"])
def test_repository_mutex_failures_never_grant_ownership(tmp_path, monkeypatch, phase):
    calls = []
    api = SimpleNamespace(CreateMutexW=Function(0 if phase == "create" else 123, calls, "create"),
        WaitForSingleObject=Function(258, calls, "wait"), CloseHandle=Function(True, calls, "close"))
    monkeypatch.setattr(access, "kernel", lambda: api)
    lock = access.RepositoryLock(tmp_path)
    with pytest.raises(ForgePilotError, match="repository_lock|repository_busy"):
        lock.acquire()
    assert lock.handle is None and lock.key not in access._HELD
    assert [name for name, _ in calls] == (["create"] if phase == "create" else ["create", "wait", "close"])


class Function:
    def __init__(self, result, calls, name):
        self.result, self.calls, self.name = result, calls, name
    def __call__(self, *args):
        self.calls.append((self.name, args))
        return self.result


def fake_api(monkeypatch, *, create=123, opened=456, moved=True, committed=True):
    calls = []
    files = SimpleNamespace(**{name: Function(value, calls, name) for name, value in {
        "CreateFileTransactedW": opened, "MoveFileTransactedW": moved, "CloseHandle": True}.items()})
    ktm = SimpleNamespace(**{name: Function(value, calls, name) for name, value in {
        "CreateTransaction": create, "CommitTransaction": committed, "RollbackTransaction": True}.items()})
    monkeypatch.setattr(transactions.ctypes, "WinDLL", lambda name, **kwargs: files if name == "kernel32" else ktm)
    return calls


@pytest.mark.parametrize("handle", [None, ctypes.c_void_p(-1).value])
def test_transaction_creation_failure_is_explicitly_unsupported(monkeypatch, handle):
    fake_api(monkeypatch, create=handle)
    with pytest.raises(ForgePilotError, match="atomic_unsupported"):
        transactions.FileTransaction()


@pytest.mark.parametrize("fault", ["load", "symbol"])
def test_missing_transaction_support_never_uses_ordinary_rename(monkeypatch, fault):
    def unavailable(*args, **kwargs):
        if fault == "load":
            raise OSError("unavailable")
        return SimpleNamespace()
    monkeypatch.setattr(transactions.ctypes, "WinDLL", unavailable)
    with pytest.raises(ForgePilotError, match="atomic_unsupported"):
        transactions.FileTransaction()


@pytest.mark.parametrize("phase", ["open", "replace", "commit"])
def test_transaction_api_errors_always_rollback_and_close(monkeypatch, phase):
    calls = fake_api(monkeypatch, opened=ctypes.c_void_p(-1).value if phase == "open" else 456,
                     moved=phase != "replace", committed=phase != "commit")
    tx = transactions.FileTransaction()
    with pytest.raises(OSError):
        try:
            if phase == "open":
                tx.open_handle("target", 1, 0, 3, 0)
            elif phase == "replace":
                tx.replace("source", "target")
            else:
                tx.commit()
        finally:
            tx.close()
    assert tx.handle is None
    assert [name for name, _ in calls][-2:] == ["RollbackTransaction", "CloseHandle"]
    tx.close()  # Idempotent cleanup must not close a reused kernel handle.


def test_committed_transaction_cleanup_never_rolls_back_or_closes_twice(monkeypatch):
    calls = fake_api(monkeypatch)
    tx = transactions.FileTransaction()
    assert tx.open_handle("target", 1, 0, 3, 0) == 456
    tx.replace("source", "target")
    tx.commit()
    tx.close()
    tx.close()
    assert tx.committed and tx.handle is None
    assert [name for name, _ in calls].count("CloseHandle") == 1
    assert not any(name == "RollbackTransaction" for name, _ in calls)
