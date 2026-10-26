"""Private fail-closed filesystem boundaries and cross-process writer leases.

Windows operations hold directory handles without FILE_SHARE_DELETE while using
paths. This closes the checked-parent/renamed-parent window for NTFS junctions.
Other platforms use O_NOFOLLOW plus before/after identity checks; they remain
experimental, as do filesystems without stable inode identity or handle paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import threading
import time
import uuid

from .errors import ForgePilotError
from .repository import is_link, opened_path

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_FILES = 2_000
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
             *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_HELD_REPOSITORIES: set[str] = set()
_HELD_REPOSITORIES_LOCK = threading.Lock()


def _error(message: str) -> ForgePilotError:
    return ForgePilotError("workspace_boundary", message)


def validate_relative(value: str) -> str:
    """Normalize separators without resolving traversal or Windows aliases."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith(("/", "\\")) or PureWindowsPath(value).drive
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _error("Use a relative controlled path")
    parts = value.replace("\\", "/").split("/")
    if any(p in {"", ".", ".."} or p.casefold() == ".git"
           or p.endswith((" ", ".")) or any(c in p for c in ':*?"<>|')
           or p.split(".", 1)[0].casefold() in _RESERVED for p in parts):
        raise _error("Path contains a protected component or Windows alias")
    return "/".join(parts)


def _absolute(value: str | Path) -> Path:
    raw = os.fspath(value)
    if (not raw or raw.startswith(("\\\\", "//")) or any(ord(c) < 32 for c in raw)
            or ".." in raw.replace("\\", "/").split("/")):
        raise _error("UNC, device paths and parent traversal are forbidden")
    path = Path(raw).absolute()
    for part in path.parts[1:]:
        if (":" in part or part.endswith((" ", "."))
                or part.split(".", 1)[0].casefold() in _RESERVED):
            raise _error("Unsafe absolute path component")
    return path


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = os.lstat(_native_path(path))
    if is_link(info):
        raise _error("Symlinks, junctions and reparse points are forbidden")
    # Windows clears FILE_ATTRIBUTE_UNPINNED (0x10000000) on first handle
    # access; archive/indexing flags also change during ordinary writes. Only
    # the reparse attribute affects boundary identity (and is rejected above).
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
            getattr(info, "st_file_attributes", 0) & 0x400)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _native_path(path: Path) -> str:
    """Use Win32 long-path syntax only after callers validated a local path."""
    value = str(path)
    return "\\\\?\\" + value if os.name == "nt" else value


def _exists(path: Path) -> bool:
    try:
        os.lstat(_native_path(path))
        return True
    except FileNotFoundError:
        return False


def _resolved(path: Path, *, strict: bool) -> Path:
    value = str(Path(_native_path(path)).resolve(strict=strict))
    if os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _open_nofollow(path: Path, *, create: bool = False, write_shared: bool = True) -> int:
    """Open the leaf reparse object itself on Windows, never its target."""
    if os.name != "nt":
        flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags, 0o600)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = kernel.CreateFileW
    api.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.restype = wintypes.HANDLE
    handle = api(_native_path(path), 0xC0000000 if create else 0x80000000,
                 3 if write_shared else 1, None, 4 if create else 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise _error("Cannot safely open controlled file")
    try:
        return msvcrt.open_osfhandle(handle, (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise


@contextmanager
def _windows_directory_handles(paths):
    """Pin Windows directory names until the protected path operation ends."""
    handles = []
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        for path in paths:
            # LIST_DIRECTORY|READ_ATTRIBUTES, SHARE_READ|SHARE_WRITE (no SHARE_DELETE),
            # OPEN_EXISTING, BACKUP_SEMANTICS|OPEN_REPARSE_POINT.
            handle = create(_native_path(path), 0x81, 3, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise _error("Cannot pin directory against replacement")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


class Guard:
    """Pin an absolute root and its existing ancestors; never follow a link."""

    def __init__(self, path: str | Path, *, create: bool = False):
        self.root = _absolute(path)
        self._pins: dict[Path, tuple] = {}
        chain = list(reversed(self.root.parents)) + [self.root]
        try:
            for candidate in chain:
                if not _exists(candidate):
                    if not create:
                        raise _error("Controlled directory is missing")
                    parent_guard = Guard(candidate.parent)
                    with parent_guard.protect():
                        os.mkdir(_native_path(candidate))
                identity = _identity(candidate)
                if identity[2] != stat.S_IFDIR:
                    raise _error("Controlled path must be a directory")
                self._pins[candidate] = identity
            self.check()
        except (OSError, ValueError, RuntimeError):
            raise _error("Cannot validate controlled directory") from None

    def check(self) -> Path:
        try:
            for path, expected in self._pins.items():
                if _identity(path) != expected:
                    raise _error("Controlled path was replaced")
            if not _same_path(_resolved(self.root, strict=True), self.root):
                raise _error("Controlled directory location changed")
            return self.root
        except (OSError, ValueError, RuntimeError):
            raise _error("Controlled path is inaccessible or replaced") from None

    def _components(self, relative: str, *, missing: bool = False):
        self.check()
        if relative == ".":
            return self.root, [self.root]
        relative = validate_relative(relative)
        current = self.root
        directories = [self.root]
        try:
            for index, part in enumerate(relative.split("/")):
                current /= part
                try:
                    identity = _identity(current)
                except FileNotFoundError:
                    if missing:
                        continue
                    raise
                if index < len(relative.split("/")) - 1 and identity[2] != stat.S_IFDIR:
                    raise _error("Parent component is not a directory")
                if identity[2] == stat.S_IFDIR:
                    directories.append(current)
                elif identity[2] != stat.S_IFREG:
                    raise _error("Only regular files and directories are allowed")
            if not _same_path(_resolved(current, strict=not missing), current):
                raise _error("Controlled file location changed")
            return current, directories
        except (OSError, ValueError, RuntimeError):
            raise _error("Controlled file is missing or inaccessible") from None

    def path(self, relative: str, *, missing: bool = False) -> Path:
        return self._components(relative, missing=missing)[0]

    @contextmanager
    def protect(self, relative: str = ".", *, missing: bool = False):
        target, directories = self._components(relative, missing=missing)
        candidates = list(dict.fromkeys([*self._pins, *directories]))
        before = {path: _identity(path) for path in candidates}
        with _windows_directory_handles(candidates):
            self.check()
            if any(_identity(path) != identity for path, identity in before.items()):
                raise _error("Directory changed while acquiring handles")
            self._components(relative, missing=missing)
            yield target
            self.check()
            if any(_identity(path) != identity for path, identity in before.items()):
                raise _error("Directory changed during operation")

    def _tree_directories(self):
        pending = [self.root]
        directories = []
        count = 0
        while pending:
            current = pending.pop()
            directories.append(current)
            with os.scandir(_native_path(current)) as listing:
                for item in listing:
                    if current == self.root and item.name == ".git":
                        continue
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ForgePilotError("workspace_limit", "Workspace has too many entries")
                    validate_relative((current / item.name).relative_to(self.root).as_posix())
                    info = item.stat(follow_symlinks=False)
                    if is_link(info):
                        raise _error("Workspace contains a link or reparse point")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(current / item.name)
                    elif not stat.S_ISREG(info.st_mode):
                        raise _error("Workspace contains a non-regular file")
        return directories

    @contextmanager
    def protect_tree(self):
        """Hold all existing worktree directories during Git file mutations."""
        with self.protect():
            directories = self._tree_directories()
            before = {path: _identity(path) for path in directories}
            with _windows_directory_handles(directories):
                if any(_identity(path) != identity for path, identity in before.items()):
                    raise _error("Workspace directory changed before Git operation")
                self._tree_directories()
                yield
                self.check()
                self._tree_directories()
                for path, identity in before.items():
                    if _exists(path) and _identity(path) != identity:
                        raise _error("Workspace directory changed during Git operation")

    def read(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        with self.protect(relative) as target:
            try:
                before = _identity(target)
                with os.fdopen(_open_nofollow(target), "rb") as stream:
                    info = os.fstat(stream.fileno())
                    actual = opened_path(stream.fileno())
                    if (is_link(info) or not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != before[:2]
                            or actual is None or not _same_path(actual, target)):
                        raise _error("Opened file identity or location changed")
                    self.path(relative)
                    data = stream.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ForgePilotError("workspace_limit", "Controlled file exceeds byte limit")
                    if _identity(target) != before:
                        raise _error("Controlled file was replaced during read")
                    return data
            except (OSError, ValueError):
                raise _error("Cannot read controlled file") from None

    def read_marker(self) -> bytes:
        """Read the one internal .git worktree pointer; unavailable to Agent paths."""
        with self.protect():
            target = self.root / ".git"
            before = _identity(target)
            if before[2] != stat.S_IFREG:
                raise _error("Expected a regular Git worktree marker")
            with os.fdopen(_open_nofollow(target), "rb") as stream:
                actual = opened_path(stream.fileno())
                if (is_link(os.fstat(stream.fileno())) or actual is None or not _same_path(actual, target)
                        or (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino) != before[:2]):
                    raise _error("Git worktree marker changed")
                data = stream.read(16_385)
                if len(data) > 16_384 or _identity(target) != before:
                    raise _error("Invalid or replaced Git worktree marker")
                return data

    def mkdir(self, relative: str) -> Path:
        relative = validate_relative(relative)
        current = []
        for part in relative.split("/"):
            current.append(part)
            name = "/".join(current)
            with self.protect(name, missing=True) as target:
                if not _exists(target):
                    os.mkdir(_native_path(target))
                if _identity(target)[2] != stat.S_IFDIR:
                    raise _error("Expected a directory")
        return self.path(relative)

    def write(self, relative: str, data: bytes, *, overwrite: bool = True) -> Path:
        if not isinstance(data, bytes) or len(data) > MAX_TOTAL_BYTES:
            raise ForgePilotError("workspace_limit", "Invalid or oversized artifact")
        relative = validate_relative(relative)
        with self.protect(relative, missing=True) as target:
            if _identity(target.parent)[2] != stat.S_IFDIR:
                raise _error("Artifact parent directory is missing")
            temporary = target.parent / (".forgepilot-" + uuid.uuid4().hex + ".tmp")
            try:
                original = _identity(target) if _exists(target) else None
                if original is not None and (not overwrite or original[2] != stat.S_IFREG):
                    raise _error("Artifact already exists or is not a regular file")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(_native_path(temporary), flags, 0o600), "wb") as stream:
                    actual = opened_path(stream.fileno())
                    if actual is None or not _same_path(actual, temporary):
                        raise _error("Temporary artifact escaped controlled directory")
                    self.path(relative, missing=True)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.path(relative, missing=True)
                if (_identity(target) if _exists(target) else None) != original:
                    raise _error("Artifact changed before atomic replacement")
                if overwrite:
                    os.replace(_native_path(temporary), _native_path(target))
                else:
                    # A hard link atomically refuses a concurrently created target.
                    os.link(_native_path(temporary), _native_path(target))
                    os.unlink(_native_path(temporary))
                self.path(relative)
                return target
            except (OSError, ValueError):
                raise _error("Cannot atomically write controlled artifact") from None
            finally:
                if _exists(temporary):
                    # Only this operation's unpredictable regular temporary file.
                    try:
                        if _identity(temporary)[2] == stat.S_IFREG:
                            os.unlink(_native_path(temporary))
                    except OSError:
                        pass

    def unlink(self, relative: str, expected_hash: str) -> None:
        """Remove one byte-verified file by Windows handle, never by its name."""
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("File deletion requires its expected SHA256")
        if os.name != "nt":
            raise ForgePilotError("workspace_cleanup_unsupported", "Handle-based deletion requires Windows")
        with self.protect(relative, missing=True) as path:
            try:
                before = _identity(path)
            except FileNotFoundError:
                return
            if before[2] != stat.S_IFREG:
                raise _error("Deletion requires a regular file")
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            handle = create(_native_path(path), 0x80010000, 1, None, 3, 0x00200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ForgePilotError("workspace_cleanup_locked", "File is busy or inaccessible")
            fd = None
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                info = os.fstat(fd)
                actual = opened_path(fd)
                if (is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != before[:2]
                        or actual is None or not _same_path(actual, path) or _identity(path) != before):
                    raise _error("Opened deletion target changed or is hardlinked")
                hasher = hashlib.sha256()
                length = 0
                while chunk := os.read(fd, 65_536):
                    length += len(chunk)
                    if length > MAX_FILE_BYTES:
                        raise ForgePilotError("workspace_limit", "Deletion target exceeds byte limit")
                    hasher.update(chunk)
                if hasher.hexdigest() != expected_hash:
                    raise _error("Deletion target bytes changed")
                value = wintypes.BOOL(True)
                disposition = kernel.SetFileInformationByHandle
                disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
                disposition.restype = wintypes.BOOL
                if not disposition(handle, 4, ctypes.byref(value), ctypes.sizeof(value)):
                    raise ForgePilotError("workspace_cleanup_locked", "Cannot delete exact opened file")
            finally:
                if fd is not None:
                    os.close(fd)
                else:
                    close = kernel.CloseHandle
                    close.argtypes = [wintypes.HANDLE]
                    close(handle)


def manifest(root: str | Path) -> dict[str, str]:
    """Complete byte manifest; only the root Git administration marker is omitted."""
    guard = Guard(root)
    result = {}
    seen = set()
    entries = total = 0
    pending = ["."]
    with guard.protect():
        while pending:
            relative = pending.pop()
            with guard.protect(relative) as directory:
                with os.scandir(_native_path(directory)) as listing:
                    children = sorted(listing, key=lambda item: item.name)
                for item in children:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        raise ForgePilotError("workspace_limit", "Workspace has too many entries")
                    if relative == "." and item.name == ".git":
                        # SafeGit separately verifies the Git administration marker.
                        if is_link(item.stat(follow_symlinks=False)):
                            raise _error("Git administration marker is a link or reparse point")
                        continue
                    name = item.name if relative == "." else relative + "/" + item.name
                    name = validate_relative(name)
                    if name.casefold() in seen:
                        raise _error("Workspace paths have a case collision")
                    seen.add(name.casefold())
                    info = item.stat(follow_symlinks=False)
                    if is_link(info):
                        raise _error("Workspace contains a link or reparse point")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(name)
                    elif stat.S_ISREG(info.st_mode):
                        if len(result) >= MAX_FILES:
                            raise ForgePilotError("workspace_limit", "Workspace has too many files")
                        data = guard.read(name)
                        total += len(data)
                        if total > MAX_TOTAL_BYTES:
                            raise ForgePilotError("workspace_limit", "Workspace exceeds total byte limit")
                        result[name] = hashlib.sha256(data).hexdigest()
                    else:
                        raise _error("Workspace contains a non-regular file")
        # Reuse recovery's complete-manifest grammar and collision validation.
        from .orchestration.workspace import _manifest
        return dict(_manifest(result))


def remove_worktree_files(root: str | Path, expected_files, marker_hash: str, *, expected_root_identity=None,
                          allowed_empty_directories=()) -> None:
    """Delete only verified Windows objects through their opened handles.

    A durable manager cleanup intent must authorize this operation. Missing
    expected objects are accepted for retry after a crash; unknown objects,
    hardlinks, changed bytes, links and active file writers stop cleanup. No
    path-based recursive removal is used, including for the final root directory.
    """
    from .orchestration.workspace import _manifest
    expected = dict(_manifest(expected_files))
    allowed_empty = {validate_relative(name) for name in allowed_empty_directories}
    if any(value is None for value in expected.values()) or not re.fullmatch(r"[0-9a-f]{64}", marker_hash):
        raise ValueError("Cleanup requires complete file and marker SHA256 values")
    path = _absolute(root)
    if os.name != "nt":
        raise ForgePilotError("workspace_cleanup_unsupported", "Handle-based cleanup requires Windows")
    parent = Guard(path.parent)
    try:
        expected_root = _identity(path)
    except FileNotFoundError:
        return
    if expected_root[2] != stat.S_IFDIR:
        raise _error("Cleanup root is not a directory")
    if expected_root_identity is not None and tuple(expected_root_identity) != expected_root[:len(expected_root_identity)]:
        raise _error("Cleanup root does not match persisted worktree identity")

    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    disposition = kernel.SetFileInformationByHandle
    disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    disposition.restype = wintypes.BOOL

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    def open_exact(candidate, *, directory):
        before = _identity(candidate)
        if before[2] != (stat.S_IFDIR if directory else stat.S_IFREG):
            raise _error("Cleanup object type changed")
        # DELETE plus LIST_DIRECTORY|READ_ATTRIBUTES for directories, or
        # GENERIC_READ for files. SHARE_READ only denies concurrent writes,
        # replacement, rename and deletion while bytes are verified/deleted.
        desired = 0x10000 | (0x81 if directory else 0x80000000)
        handle = create(_native_path(candidate), desired, 1, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ForgePilotError("workspace_cleanup_locked", "Cleanup object is busy or inaccessible")
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            close(handle)
            raise
        try:
            info = os.fstat(fd)
            actual = opened_path(fd)
            if (is_link(info) or (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != before[:3]
                    or actual is None or not _same_path(actual, candidate) or _identity(candidate) != before):
                raise _error("Cleanup opened object identity or location changed")
            if not directory and info.st_nlink != 1:
                raise _error("Cleanup refuses hardlinked files")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def mark_delete(fd):
        value = _Disposition(True)
        if not disposition(msvcrt.get_osfhandle(fd), 4, ctypes.byref(value), ctypes.sizeof(value)):
            raise ForgePilotError("workspace_cleanup_locked", "Cleanup object became busy or directory is not empty")

    directories: list[tuple[Path, int]] = []
    files: list[tuple[Path, int]] = []
    with parent.protect():
        try:
            root_fd = open_exact(path, directory=True)
            directories.append((path, root_fd))
            if _identity(path) != expected_root:
                raise _error("Cleanup root changed while acquiring its handle")
            pending = [path]
            count = total = 0
            while pending:
                current = pending.pop()
                with os.scandir(_native_path(current)) as listing:
                    children = list(listing)
                for item in children:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ForgePilotError("workspace_limit", "Cleanup entry limit exceeded")
                    candidate = current / item.name
                    name = candidate.relative_to(path).as_posix()
                    info = item.stat(follow_symlinks=False)
                    if is_link(info):
                        raise _error("Cleanup refuses links or reparse points")
                    if name == ".git":
                        if not stat.S_ISREG(info.st_mode):
                            raise _error("Cleanup refuses a main Git repository")
                        digest = marker_hash
                    else:
                        validate_relative(name)
                        if stat.S_ISDIR(info.st_mode):
                            # Unknown empty directories are harmless, but a
                            # directory unrelated to any expected file may be
                            # user-created and must be preserved.
                            if name not in allowed_empty and not any(key.startswith(name + "/") for key in expected):
                                raise _error("Cleanup found an unknown directory")
                            fd = open_exact(candidate, directory=True)
                            directories.append((candidate, fd))
                            pending.append(candidate)
                            continue
                        if name not in expected:
                            raise _error("Cleanup found an unknown file")
                        digest = expected[name]
                    fd = open_exact(candidate, directory=False)
                    files.append((candidate, fd))
                    hasher = hashlib.sha256()
                    length = 0
                    while chunk := os.read(fd, 65_536):
                        length += len(chunk)
                        total += len(chunk)
                        if length > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                            raise ForgePilotError("workspace_limit", "Cleanup byte limit exceeded")
                        hasher.update(chunk)
                    if hasher.hexdigest() != digest:
                        raise _error("Cleanup file bytes no longer match approved manifest")
            # Verify the complete acquisition before the first deletion. All
            # file handles deny WRITE and DELETE, so hashes remain stable.
            for candidate, fd in [*directories, *files]:
                actual = opened_path(fd)
                if actual is None or not _same_path(actual, candidate):
                    raise _error("Cleanup object moved during verification")
            for _, fd in files:
                mark_delete(fd)
            for _, fd in files:
                os.close(fd)
            files.clear()
            # Our own DELETE handles allow exact object disposition while
            # denying competing handles permission to rename or delete them.
            for directory, fd in sorted(directories, key=lambda pair: len(pair[0].parts), reverse=True):
                mark_delete(fd)
                os.close(fd)
                directories.remove((directory, fd))
        except (OSError, ValueError):
            raise _error("Cannot safely remove verified worktree objects") from None
        finally:
            for _, fd in [*files, *directories]:
                os.close(fd)


def remove_worktree_registration(admin: str | Path, common_dir: str | Path,
                                 worktree_path: str | Path, expected_files, *,
                                 expected_root_identity) -> None:
    """Precisely remove one verified Git worktree registration by handles.

    This deliberately never invokes ``git worktree remove`` on a now-vacant
    pathname: a user could recreate that pathname after its verified deletion.
    The full metadata hash manifest and directory identity belong in the durable
    cleanup intent, so a partial deletion can be retried without rediscovery.
    """
    from .orchestration.workspace import _manifest
    admin, common, workspace = _absolute(admin), _absolute(common_dir), _absolute(worktree_path)
    if (common.name != ".git" or admin.parent != common / "worktrees"
            or validate_relative(admin.name) != admin.name or not expected_root_identity):
        raise _error("Cleanup registration is outside the exact worktree metadata boundary")
    expected = dict(_manifest(expected_files))
    allowed = {"HEAD", "index", "commondir", "gitdir", "logs/HEAD", "ORIG_HEAD", "config.worktree"}
    if not {"HEAD", "gitdir", "commondir"}.issubset(expected) or not set(expected).issubset(allowed):
        raise _error("Cleanup registration contains unknown Git metadata")
    # Main .git and every ancestor are pinned independently of the metadata
    # root, which will itself be opened with DELETE access by the exact helper.
    parent = Guard(admin.parent)
    with parent.protect():
        if not _exists(admin):
            return
        guard = Guard(admin)
        if _exists(admin / "gitdir"):
            value = guard.read("gitdir", max_bytes=16_384).decode("utf-8").strip()
            if not _same_path(Path(value), workspace / ".git"):
                raise _error("Cleanup registration points to another worktree")
        if _exists(admin / "commondir"):
            if guard.read("commondir", max_bytes=16_384).strip().replace(b"\\", b"/") != b"../..":
                raise _error("Cleanup registration common directory changed")
        remove_worktree_files(admin, expected, hashlib.sha256(b"").hexdigest(),
                              expected_root_identity=expected_root_identity,
                              allowed_empty_directories=("refs", "logs"))


class WriterLease:
    """Non-blocking repository-wide OS lock with persistent crash evidence.

    Lock files are never unlinked: deleting/recreating them would split owners
    across inodes. An unlocked file marked active is a stale lease and requires
    explicit recover=True, even when the previous process appears dead.
    """
    def __init__(self, data_root: str | Path, repository_id: str, owner: str, *, recover: bool = False):
        if not _IDENTIFIER.fullmatch(repository_id) or not _IDENTIFIER.fullmatch(owner):
            raise ValueError("Invalid lease repository or owner")
        self.guard = Guard(data_root, create=True)
        self.guard.mkdir("leases")
        self.relative = "leases/" + repository_id + ".lock"
        self.repository_id = repository_id
        self.owner, self.recover = owner, recover
        self._stream = None
        self._identity = None
        self.previous = None
        self._mutex = None
        self._mutex_thread = None

    def _acquire_global(self):
        if os.name != "nt":
            return
        # Windows mutexes are recursive for the owning thread. Reserve the
        # repository locally first so a second manager/data root cannot take
        # the same mutex recursively and become another writer.
        with _HELD_REPOSITORIES_LOCK:
            if self.repository_id in _HELD_REPOSITORIES:
                raise ForgePilotError("workspace_lease_busy", "Repository already has a writer")
            _HELD_REPOSITORIES.add(self.repository_id)
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        wait = kernel.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = None
        try:
            handle = create(None, False, "Global\\ForgePilotWriter-" + self.repository_id)
            if not handle:
                raise ForgePilotError("workspace_lease", "Cannot open the repository OS writer mutex")
            status = wait(handle, 0)
            if status == 0x102:
                raise ForgePilotError("workspace_lease_busy", "Repository already has a writer")
            if status not in (0, 0x80):
                raise ForgePilotError("workspace_lease", "Cannot verify the repository OS writer mutex")
            self._mutex = handle
            self._mutex_thread = threading.get_ident()
            handle = None
            if status == 0x80 and not self.recover:
                raise ForgePilotError("workspace_lease_stale", "Previous OS writer was abandoned; explicit recovery is required")
        except BaseException:
            if self._mutex is not None:
                self._release_global()
            else:
                with _HELD_REPOSITORIES_LOCK:
                    _HELD_REPOSITORIES.discard(self.repository_id)
            if handle:
                close(handle)
            raise

    def _release_global(self):
        if self._mutex is None:
            return
        if self._mutex_thread != threading.get_ident():
            raise ForgePilotError("workspace_lease", "Writer lease must be released by its owning thread")
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        release = kernel.ReleaseMutex
        release.argtypes = [wintypes.HANDLE]
        release.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        if not release(self._mutex):
            raise ForgePilotError("workspace_lease", "Cannot release the repository OS writer mutex")
        close(self._mutex)
        self._mutex = self._mutex_thread = None
        with _HELD_REPOSITORIES_LOCK:
            _HELD_REPOSITORIES.discard(self.repository_id)

    def acquire(self):
        if self._stream is not None:
            raise ForgePilotError("workspace_lease", "Lease instance is already acquired")
        self._acquire_global()
        try:
            return self._acquire_file()
        except BaseException:
            self._release_global()
            raise

    def _acquire_file(self):
        with self.guard.protect(self.relative, missing=True) as path:
            try:
                stream = os.fdopen(_open_nofollow(path, create=True), "r+b", buffering=0)
                actual = opened_path(stream.fileno())
                if (actual is None or not _same_path(actual, path) or is_link(os.fstat(stream.fileno()))
                        or not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)):
                    stream.close()
                    raise _error("Lease file identity changed")
                try:
                    self._lock(stream)
                except OSError:
                    stream.close()
                    raise ForgePilotError("workspace_lease_busy", "Repository already has a writer") from None
                self._stream = stream
                self._identity = _identity(path)
                stream.seek(0)
                payload = stream.read(16_385)
                try:
                    previous = json.loads(payload) if payload else None
                    if previous is not None and (not isinstance(previous, dict) or "active" not in previous):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise ForgePilotError("workspace_lease_stale", "Lease metadata is corrupt; inspect before recovery") from None
                self.previous = previous
                if previous and previous.get("active") and not self.recover:
                    raise ForgePilotError("workspace_lease_stale", "Previous writer did not release; explicit recovery is required")
                self._record(True)
                return self
            except BaseException:
                if self._stream is not None:
                    stream = self._stream
                    self._stream = None
                    try:
                        self._unlock(stream)
                    finally:
                        stream.close()
                raise

    @staticmethod
    def _lock(stream):
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream):
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def check(self):
        if self._stream is None:
            raise ForgePilotError("workspace_lease", "Writer lease is not held")
        if os.name == "nt" and (self._mutex is None or self._mutex_thread != threading.get_ident()):
            raise ForgePilotError("workspace_lease", "Repository OS writer mutex is not held by this thread")
        path = self.guard.path(self.relative)
        if _identity(path) != self._identity:
            raise _error("Writer lease file was replaced")

    def _record(self, active):
        self.check()
        payload = json.dumps({"version": 1, "owner": self.owner, "pid": os.getpid(),
                              "active": active, "updated_at": time.time()}, sort_keys=True).encode()
        self._stream.seek(0)
        self._stream.write(payload)
        self._stream.truncate()
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def release(self):
        if self._stream is None:
            self._release_global()
            return
        if os.name == "nt" and self._mutex_thread != threading.get_ident():
            raise ForgePilotError("workspace_lease", "Writer lease must be released by its owning thread")
        try:
            self._record(False)
        finally:
            try:
                stream = self._stream
                self._stream = None
                try:
                    self._unlock(stream)
                finally:
                    stream.close()
            finally:
                self._release_global()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
