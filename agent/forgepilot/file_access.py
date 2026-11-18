"""Windows file handles for bounded, conflict-checked mutations; no shell operations."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat
import threading
import hashlib
import uuid

from .errors import ForgePilotError
from .path_identity import path_key, path_within, same_path
from .repository import is_link, opened_path
from .windows_transaction import FileTransaction


def kernel():
    if os.name != "nt":
        raise ForgePilotError("platform_unsupported", "Mutations require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    return api


def _open(api, path, access, share, disposition, flags):
    handle = api.CreateFileW(str(path), access, share, None, disposition, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Cannot acquire a safe file handle")
    return handle


@contextmanager
def pinned_file(repository, relative: str, *, create: bool = False, writable: bool = False,
                replaceable: bool = False):
    """Pin ancestor directories against rename and open the final file without truncation.

    Caller must authorize before create=True. Missing parent directories are created
    only then. All mutation/compare operations use the same exclusive file handle.
    """
    import msvcrt
    api = kernel()
    path = repository._path(relative, allow_missing=True)
    handles = []
    stream = None
    created = False
    transaction = None
    try:
        # Pin from the volume root down, preventing parent replacement during access.
        for directory in reversed(path.parents):
            if not directory.exists():
                if not create or not path_within(directory, repository.root):
                    raise ForgePilotError("repository_path", "Parent directory is missing")
                directory.mkdir()
            # Metadata-only access does not enforce delete sharing on Windows.
            # GENERIC_READ participates in sharing checks and pins directory rename.
            handle = _open(api, directory, 0x80000000, 3, 3, 0x02000000 | 0x00200000)
            handles.append(handle)
            if is_link(directory.lstat()) or not same_path(directory.resolve(), directory):
                raise ForgePilotError("repository_boundary", "Directory links are excluded")
        repository._path(relative, allow_missing=True)
        exists = path.exists()
        if not exists and not create:
            yield None, False
            return
        # CREATE_NEW fails if a file appeared since the existence check. Never truncate on open.
        access = 0x80000000 | (0x40000000 | 0x10000 if writable else 0)
        if replaceable:
            transaction = FileTransaction()
            handle = transaction.open_handle(path, access, 0, 3 if exists else 1, 0x00200000)
        else:
            handle = _open(api, path, access, 0, 3 if exists else 1, 0x00200000)
        created = not exists
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY))
        except Exception:
            api.CloseHandle(handle)
            raise
        stream = os.fdopen(fd, "r+b" if writable else "rb", buffering=0)
        stream._forgepilot_transaction = transaction
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or is_link(info) or info.st_nlink != 1:
            raise ForgePilotError("repository_boundary", "Only ordinary files with one hard link are supported")
        actual = opened_path(fd)
        if not same_path(actual, path) or not path_within(actual, repository.root):
            raise ForgePilotError("repository_boundary", "Opened file escaped the repository")
        # No path reopening here: the file is exclusively held and ancestors remain pinned.
        yield stream, created
    except OSError:
        raise ForgePilotError("file_access", "File is busy, changed, or inaccessible; no unsafe retry") from None
    finally:
        if stream is not None:
            stream.close()
        if transaction is not None:
            transaction.close()
        for handle in reversed(handles):
            api.CloseHandle(handle)


def delete_open_file(stream) -> None:
    """Delete this exact open file on close, without reopening a path."""
    import msvcrt
    if stream.closed and getattr(stream, "_forgepilot_transaction", None) is not None:
        return  # Its uncommitted transaction rolls back the invisible placeholder.
    api = kernel()
    value = wintypes.BOOL(True)
    if not api.SetFileInformationByHandle(msvcrt.get_osfhandle(stream.fileno()), 4,
                                        ctypes.byref(value), ctypes.sizeof(value)):
        raise ForgePilotError("restore_io", "Cannot remove the verified task-created file")


class CommittedPatchError(ForgePilotError):
    """Replacement committed; retain its recovery snapshot even if verification fails."""


def write_staged_bytes(stream, data):
    """Write and fsync a temporary file; the destination is never partially written."""
    view = memoryview(data)
    while view:
        count = stream.write(view)
        if not count:
            raise OSError("Incomplete staged write")
        view = view[count:]
    stream.flush()
    os.fsync(stream.fileno())


def atomic_replace(repository, relative, stream, data, *, expected_sha256):
    """Replace within the transaction that already locks the exact CAS target.

    Staging names and replacements are isolated until commit. The target's lock
    survives closing its handle, so another process cannot rename/rebind it in
    the interval between the final hash check and transactional rename.
    """
    import msvcrt
    api = kernel()
    transaction = getattr(stream, "_forgepilot_transaction", None)
    if transaction is None:
        raise ForgePilotError("atomic_unsupported", "Atomic replacement requires a transactional target lock")
    path = repository._path(relative, allow_missing=True)
    temporary = path.with_name(".forgepilot-patch-" + uuid.uuid4().hex + ".tmp")
    staged = None
    try:
        handle = transaction.open_handle(temporary, 0x80000000 | 0x40000000 | 0x10000, 0, 1, 0x00200000)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
        except BaseException:
            api.CloseHandle(handle)
            raise
        staged = os.fdopen(fd, "r+b", buffering=0)
        info = os.fstat(fd)
        staged_identity = (info.st_dev, info.st_ino)
        if (not stat.S_ISREG(info.st_mode) or is_link(info) or info.st_nlink != 1
                or not same_path(opened_path(fd), temporary)):
            raise ForgePilotError("repository_boundary", "Unsafe patch staging file")
        write_staged_bytes(staged, data)
        staged.seek(0)
        if staged.read(len(data) + 1) != data:
            raise ForgePilotError("patch_integrity", "Staged patch verification failed")
        repository._path(relative, allow_missing=True)
        stream.seek(0)
        if hashlib.sha256(stream.read(len(data) + 262_145)).hexdigest() != expected_sha256:
            raise ForgePilotError("write_conflict", "Patch baseline changed before replacement")
        if not same_path(opened_path(stream.fileno()), path):
            raise ForgePilotError("repository_boundary", "Patch destination changed")
        # TxF retains namespace isolation after these handles close. In contrast,
        # FILE_SHARE_DELETE plus an ordinary rename permits a leaf rebind race.
        stream.close()
        staged.close()
        transaction.replace(temporary, path)
        handle = transaction.open_handle(path, 0x80000000, 0, 3, 0x00200000)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
        except BaseException:
            api.CloseHandle(handle)
            raise
        with os.fdopen(fd, "rb", buffering=0) as verified:
            info = os.fstat(fd)
            if ((info.st_dev, info.st_ino) != staged_identity or is_link(info)
                    or info.st_nlink != 1 or verified.read(len(data) + 1) != data):
                raise ForgePilotError("patch_integrity", "Transactional patch verification failed")
        transaction.commit()
        # Reopen only after commit, then pin and verify the committed file itself.
        # Any error now retains ChangeSet's snapshot for conflict-aware recovery.
        with pinned_file(repository, relative) as (verified, _):
            if verified is None:
                raise ForgePilotError("patch_integrity", "Committed patch disappeared")
            info = os.fstat(verified.fileno())
            actual = verified.read(len(data) + 1)
            if (info.st_dev, info.st_ino) != staged_identity or actual != data:
                raise ForgePilotError("patch_integrity", "Committed patch changed during verification")
            return hashlib.sha256(actual).hexdigest()
    except BaseException as exc:
        if transaction.committed:
            raise CommittedPatchError("patch_integrity", "Patch replacement committed but final verification failed; recovery snapshot retained") from None
        if isinstance(exc, OSError):
            raise ForgePilotError("file_access", f"Atomic patch staging or replacement failed ({exc.errno})") from None
        raise
    finally:
        if staged is not None:
            staged.close()


_HELD: set[str] = set()
_GUARD = threading.Lock()


class RepositoryLock:
    """Non-waiting cross-process Windows mutex plus a guard against thread reentrancy."""
    def __init__(self, root: Path):
        self.key = path_key(str(root.resolve())).replace("/", os.sep)
        self.handle = None

    def acquire(self):
        api = kernel()
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        with _GUARD:
            if self.key in _HELD:
                raise ForgePilotError("repository_busy", "Another modification or restore task owns this repository")
            name = "Global\\ForgePilot-" + hashlib.sha256(self.key.encode()).hexdigest()
            handle = api.CreateMutexW(None, False, name)
            if not handle:
                raise ForgePilotError("repository_lock", "Cannot create repository lock")
            result = api.WaitForSingleObject(handle, 0)
            if result not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED (crashed previous owner)
                api.CloseHandle(handle)
                raise ForgePilotError("repository_busy", "Another modification or restore task owns this repository")
            self.handle = handle
            _HELD.add(self.key)

    def release(self):
        if self.handle is not None:
            api = kernel()
            api.ReleaseMutex.argtypes = [wintypes.HANDLE]
            api.ReleaseMutex.restype = wintypes.BOOL
            with _GUARD:
                api.ReleaseMutex(self.handle)
                api.CloseHandle(self.handle)
                self.handle = None
                _HELD.discard(self.key)
