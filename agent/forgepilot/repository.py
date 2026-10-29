"""Bounded, read-only access to an explicitly selected Git repository."""

import asyncio
import codecs
from collections import OrderedDict
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat

from .errors import ForgePilotError
from .path_identity import path_key, path_within, same_path
from .process import run_process

MAX_FILE_BYTES = 262_144
MAX_SCAN_ENTRIES = 20_000
MAX_FILES = 2_000
MAX_SEARCH_FILES = 200
MAX_SEARCH_BYTES = 8_388_608
MAX_OUTPUT_CHARS = 12_000
DEFAULT_REPOSITORY_CACHE_CAPACITY = 16
EXCLUDED = (
    ".git", ".hg", ".svn", ".forgepilot", ".venv", "*.venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".uv-cache",
    "build", "build-*", "cmake-build-*", "cmakefiles", "out", "bin", "obj", "dist",
    "node_modules", ".vs", ".idea", ".ssh", ".aws", ".azure", "logs",
    ".hidden-tests", ".hidden_tests", "hidden-tests", "hidden_tests",
    "__hidden_tests__", ".forgepilot-tests",
    ".oracle", "oracle", "protected-oracle", "protected_oracle", "hidden-oracle", "hidden_oracle",
    ".env*", "*secret*", "*credential*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc", "config.local.*",
    "cmakeuserpresets.json", "local-env.cmd", "*.exe", "*.dll", "*.obj", "*.pdb",
    "*.lib", "*.a", "*.o", "*.db", "*.sqlite*",
)


def excluded(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.casefold(), pattern) for pattern in EXCLUDED)


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def opened_path(fd: int) -> Path | None:
    """On Windows validate the opened handle, not just the pre-open path."""
    if os.name != "nt":
        proc = Path(f"/proc/self/fd/{fd}")
        return proc.resolve() if proc.exists() else None
    import ctypes
    from ctypes import wintypes
    import msvcrt

    api = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    api.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    api.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32_768)
    size = api(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not size or size >= len(buffer):
        raise ForgePilotError("repository_boundary", "Cannot verify opened file location")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


@dataclass(frozen=True, slots=True)
class _RepositoryIdentity:
    device: int
    inode: int
    mode: int
    attributes: int
    size: int | None = None
    modified_ns: int | None = None


def _identity(path: Path, *, marker: bool = False) -> _RepositoryIdentity:
    info = path.lstat()
    return _RepositoryIdentity(
        info.st_dev, info.st_ino, info.st_mode,
        getattr(info, "st_file_attributes", 0),
        info.st_size if marker and stat.S_ISREG(info.st_mode) else None,
        getattr(info, "st_mtime_ns", None) if marker and stat.S_ISREG(info.st_mode) else None,
    )


def _canonical_root(path: str) -> Path:
    try:
        if not isinstance(path, str) or not path or path.startswith(("\\\\", "//")):
            raise ValueError()
        windows = PureWindowsPath(path)
        parts = path.replace("\\", "/").split("/")
        if (any(part in {".", ".."} or part.endswith((" ", ".")) for part in parts if part)
                or ":" in path[len(windows.drive):]
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            raise ValueError()
        selected = Path(path).absolute()
        for ancestor in (selected, *selected.parents):
            if ancestor.is_symlink() or (ancestor.exists() and is_link(ancestor.lstat())):
                raise ValueError()
        root = Path(path).resolve(strict=True)
        if not root.is_dir():
            raise OSError()
        return root
    except (OSError, ValueError, RuntimeError):
        raise ForgePilotError("repository_invalid", "Repository directory does not exist") from None


def _cache_key(root: Path) -> str:
    """Return the stable, platform-normalized absolute cache key."""
    return path_key(os.path.normpath(str(root.absolute())))


@dataclass(frozen=True, slots=True)
class _VerifiedRepositoryRoot:
    root: Path
    key: str
    root_identity: _RepositoryIdentity
    marker_identity: _RepositoryIdentity

    def valid(self) -> bool:
        try:
            return (same_path(_canonical_root(str(self.root)), self.root)
                    and _identity(self.root) == self.root_identity
                    and _identity(self.root / ".git", marker=True) == self.marker_identity)
        except (ForgePilotError, OSError, ValueError, RuntimeError):
            return False

    def session(self, rg_executable: str) -> "Repository":
        return Repository(self.root, rg_executable,
                          expected_root_identity=self.root_identity,
                          expected_marker_identity=self.marker_identity)


async def _verify_repository_root(root: Path, key: str, git_executable: str) -> _VerifiedRepositoryRoot:
    try:
        before_root = _identity(root)
        before_marker = _identity(root / ".git", marker=True)
    except OSError:
        raise ForgePilotError("repository_not_git_root",
                              "Select the root of a local Git working repository") from None
    result = await run_process(
        [git_executable, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root),
         "rev-parse", "--show-toplevel"], root, output_limit=16_384,
    )
    try:
        valid = (result.returncode == 0 and not result.truncated
                 and same_path(Path(result.stdout.decode("utf-8").strip()).resolve(), root))
        after_root = _identity(root)
        after_marker = _identity(root / ".git", marker=True)
    except (ValueError, OSError, UnicodeError):
        valid = False
        after_root = after_marker = None
    if not valid:
        raise ForgePilotError("repository_not_git_root",
                              "Select the root of a local Git working repository")
    if before_root != after_root or before_marker != after_marker or not same_path(_canonical_root(str(root)), root):
        raise ForgePilotError("repository_boundary", "Repository root changed during validation")
    return _VerifiedRepositoryRoot(root, key, after_root, after_marker)


class RepositoryCache:
    """Bounded LRU of verified roots; every open returns a fresh request session."""

    def __init__(self, capacity: int = DEFAULT_REPOSITORY_CACHE_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("invalid repository cache capacity")
        self.capacity = capacity
        self._entries: OrderedDict[str, _VerifiedRepositoryRoot] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[_VerifiedRepositoryRoot]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _consume_unobserved_failure(task: asyncio.Task[_VerifiedRepositoryRoot]) -> None:
        # Waiters still receive the same result. This only prevents an abandoned
        # single-flight (all callers cancelled) from leaking an asyncio warning.
        if not task.cancelled():
            task.exception()

    async def open(self, path: str, *, rg_executable: str,
                   git_executable: str) -> tuple["Repository", bool]:
        try:
            root = _canonical_root(path)
        except ForgePilotError:
            # A direct cached root that was deleted or moved must not remain
            # eligible if it later reappears at the same path.
            try:
                missing_key = _cache_key(Path(path))
            except (OSError, ValueError, RuntimeError):
                missing_key = None
            if missing_key is not None:
                async with self._lock:
                    self._entries.pop(missing_key, None)
            raise
        key = _cache_key(root)
        async with self._lock:
            if self._closed:
                raise ForgePilotError("server_stopping", "Repository cache is closed")
            entry = self._entries.get(key)
            if entry is not None:
                if entry.valid():
                    self._entries.move_to_end(key)
                    return entry.session(rg_executable), True
                self._entries.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._load(root, key, git_executable))
                task.add_done_callback(self._consume_unobserved_failure)
                self._inflight[key] = task
        # One cancelled request must not cancel verification shared by other requests.
        entry = await asyncio.shield(task)
        return entry.session(rg_executable), False

    async def _load(self, root: Path, key: str,
                    git_executable: str) -> _VerifiedRepositoryRoot:
        current = asyncio.current_task()
        try:
            entry = await _verify_repository_root(root, key, git_executable)
            async with self._lock:
                if self._closed:
                    raise ForgePilotError("server_stopping", "Repository cache is closed")
                self._entries[key] = entry
                self._entries.move_to_end(key)
                while len(self._entries) > self.capacity:
                    self._entries.popitem(last=False)
            return entry
        finally:
            async with self._lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            pending = tuple(self._inflight.values())
            self._entries.clear()
            for task in pending:
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            self._inflight.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def closed(self) -> bool:
        return self._closed


class Repository:
    def __init__(self, root: Path, rg_executable: str, *,
                 expected_root_identity: _RepositoryIdentity | None = None,
                 expected_marker_identity: _RepositoryIdentity | None = None) -> None:
        self.root = root
        self.rg_executable = rg_executable
        try:
            info = root.stat()
            actual_root = _identity(root)
            actual_marker = (_identity(root / ".git", marker=True)
                             if expected_marker_identity is not None else None)
        except OSError:
            raise ForgePilotError("repository_boundary", "Repository root changed") from None
        if ((expected_root_identity is not None and actual_root != expected_root_identity)
                or (expected_marker_identity is not None and actual_marker != expected_marker_identity)
                or not same_path(root.resolve(strict=True), root)):
            raise ForgePilotError("repository_boundary", "Repository root changed")
        self._root_identity = (info.st_dev, info.st_ino)
        self._read_versions: dict[str, str] = {}

    @classmethod
    async def open(cls, path: str, *, rg_executable: str, git_executable: str):
        root = _canonical_root(path)
        entry = await _verify_repository_root(root, _cache_key(root), git_executable)
        return entry.session(rg_executable)

    def _path(self, value: str, *, allow_missing: bool = False) -> Path:
        if (not isinstance(value, str) or len(value) > 1_024 or not value or any(ord(c) < 32 for c in value)
                or ":" in value or PureWindowsPath(value).drive or value.startswith(("/", "\\"))):
            raise ForgePilotError("repository_boundary", "Use a relative repository path")
        parts = value.replace("\\", "/").split("/")
        if ".." in parts or any(p and p != "." and (
                p.endswith((" ", ".")) or excluded(p) or PureWindowsPath(p).is_reserved()
                or any(c in p for c in '<>"|?*')) for p in parts):
            raise ForgePilotError("repository_denied", "Path is excluded or contains parent traversal")
        path = self.root
        try:
            root_info = self.root.stat()
            if not same_path(self.root.resolve(strict=True), self.root) or (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise ForgePilotError("repository_boundary", "Repository root changed")
            for part in parts:
                if part in ("", "."):
                    continue
                path /= part
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if allow_missing:
                        continue
                    raise
                if is_link(info):
                    raise ForgePilotError("repository_boundary", "Links and reparse points are excluded")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ForgePilotError("repository_boundary", "Files with multiple hard links are excluded")
            resolved = path.resolve(strict=not allow_missing)
            if not path_within(resolved, self.root):
                raise ForgePilotError("repository_boundary", "Path is outside the repository")
            if any(excluded(part) for part in resolved.relative_to(self.root).parts):
                raise ForgePilotError("repository_denied", "Canonical path is excluded")
            return resolved
        except (OSError, ValueError, RuntimeError):
            raise ForgePilotError("repository_path", "Path does not exist or is inaccessible") from None

    def normalize_plan_path(self, value: str) -> str:
        """Validate proposed existing/new files without creating files or directories."""
        path = self._path(value, allow_missing=True)
        try:
            if path.exists() and not path.is_file():
                raise ForgePilotError("repository_path", "Plan must name a file, not a directory")
            return path.resolve(strict=False).relative_to(self.root).as_posix()
        except (OSError, ValueError, RuntimeError):
            raise ForgePilotError("repository_boundary", "Cannot normalize proposed file location") from None

    async def _files(self, path: str) -> tuple[list[str], bool]:
        directory = self._path(path)
        if not directory.is_dir():
            raise ForgePilotError("repository_path", "Expected a directory")
        pending = [directory]
        files = []
        scanned = 0
        while pending:
            current = self._path(pending.pop().relative_to(self.root).as_posix())
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCAN_ENTRIES or len(files) >= MAX_FILES:
                            return sorted(files), True
                        if scanned % 64 == 0:
                            await asyncio.sleep(0)
                        if excluded(entry.name):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            files.append(Path(entry.path).relative_to(self.root).as_posix())
            except OSError:
                raise ForgePilotError("repository_read", "Cannot enumerate repository directory") from None
        return sorted(files), False

    def audit_manifest(self) -> dict[str, str]:
        """Complete bounded source manifest; approvals must never silently truncate."""
        from .file_access import pinned_file
        pending = ["."]
        result = {}
        scanned = total = 0
        while pending:
            directory = self._path(pending.pop())
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCAN_ENTRIES:
                            raise ForgePilotError("scope_limit", "Approval scan entry limit exceeded")
                        if excluded(entry.name):
                            continue
                        relative = Path(entry.path).relative_to(self.root).as_posix()
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            raise ForgePilotError("repository_boundary", "Approval scan found a link or reparse point")
                        self._path(relative)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(relative)
                        elif stat.S_ISREG(info.st_mode):
                            if len(result) >= MAX_FILES:
                                raise ForgePilotError("scope_limit", "Approval file count limit exceeded")
                            with pinned_file(self, relative) as (stream, _):
                                data = stream.read(MAX_SEARCH_BYTES + 1)
                            total += len(data)
                            if total > MAX_SEARCH_BYTES:
                                raise ForgePilotError("scope_limit", "Approval file or total byte limit exceeded")
                            key = path_key(relative)
                            if key in result:
                                raise ForgePilotError("scope_ambiguous", "Distinct physical files share an ambiguous path key")
                            result[key] = hashlib.sha256(data).hexdigest()
                        else:
                            raise ForgePilotError("repository_boundary", "Only ordinary files and directories are supported")
            except OSError:
                raise ForgePilotError("repository_read", "Cannot finish approval scope scan") from None
        return result

    def _read(self, path: str) -> tuple[str, bool]:
        text, truncated, _ = self._read_snapshot(path)
        return text, truncated

    def _read_snapshot(self, path: str) -> tuple[str, bool, str | None]:
        candidate = self._path(path)
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(candidate, flags), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or is_link(info) or info.st_nlink != 1:
                    raise ForgePilotError("repository_path", "Expected a regular file")
                actual = opened_path(stream.fileno())
                if actual is not None and (not path_within(actual, self.root) or not same_path(actual, candidate)):
                    raise ForgePilotError("repository_boundary", "Opened file location changed")
                self._path(path)  # Reject a changed link/parent before reading the handle.
                data = stream.read(MAX_FILE_BYTES + 1)
            truncated = len(data) > MAX_FILE_BYTES
            version = None if truncated else hashlib.sha256(data).hexdigest()
            data = data[:MAX_FILE_BYTES]
            if b"\x00" in data:
                raise ForgePilotError("repository_binary", "Binary files are not supported")
            text = codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=not truncated)
            return text, truncated, version
        except UnicodeError:
            raise ForgePilotError("repository_encoding", "Only UTF-8 text is supported in Phase 1") from None
        except (OSError, ValueError):
            raise ForgePilotError("repository_read", "Cannot read repository file") from None

    async def list_files(self, path: str = ".", limit: int = 200) -> str:
        files, truncated = await self._files(path)
        selected = []
        size = 0
        for file in files:
            size += len(json.dumps(file, ensure_ascii=False)) + 2
            if len(selected) >= limit or size > MAX_OUTPUT_CHARS:
                truncated = True
                break
            selected.append(file)
        return json.dumps({"files": selected, "truncated": truncated,
                           "note": "Policy-excluded paths and links are omitted; listing is bounded"}, ensure_ascii=False)

    async def read_file(self, path: str, start_line: int = 1, end_line: int = 200) -> str:
        return await self._read_file(path, start_line, end_line, record_version=True)

    async def _read_file(self, path: str, start_line: int = 1, end_line: int = 200, *, record_version=False) -> str:
        if end_line < start_line:
            raise ForgePilotError("invalid_arguments", "end_line must be >= start_line")
        await asyncio.sleep(0)
        key = path_key(self.normalize_plan_path(path)) if record_version else None
        text, limited, version = self._read_snapshot(path)
        # Only explicit reads establish a baseline; internal _read callers do not.
        if record_version:
            if version is None:
                self._read_versions.pop(key, None)
            else:
                self._read_versions[key] = version
        lines = text.splitlines()
        selected = "\n".join(f"{i}: {line}" for i, line in
                             enumerate(lines, 1) if start_line <= i <= end_line)
        reasons = []
        if limited:
            reasons.append("file_byte_limit")
        if start_line > 1 or end_line < len(lines):
            reasons.append("line_range")
        if len(selected) > MAX_OUTPUT_CHARS:
            selected = selected[:MAX_OUTPUT_CHARS]
            reasons.append("output_limit")
        # Keep structured JSON valid even when code contains many tabs/quotes.
        while len(json.dumps(selected, ensure_ascii=False)) > MAX_OUTPUT_CHARS:
            selected = selected[:len(selected) * 3 // 4]
            if "output_limit" not in reasons:
                reasons.append("output_limit")
        return json.dumps({"path": path, "content": selected, "sha256": version, "truncated": bool(reasons),
                           "limits": reasons}, ensure_ascii=False)

    async def search_code(self, query: str, path: str = ".", max_results: int = 50, *, file_suffixes=None) -> str:
        target = self._path(path)
        files, listing_limited = await self._files(path) if target.is_dir() else ([path], False)
        if file_suffixes is not None:
            files = [file for file in files if Path(file).suffix.casefold() in file_suffixes]
        reasons = {"file_scan_limit"} if listing_limited or len(files) > MAX_SEARCH_FILES else set()
        matches = []
        scanned_bytes = 0
        output_chars = 0
        skipped = 0
        for file in files[:MAX_SEARCH_FILES]:
            await asyncio.sleep(0)
            try:
                text, limited = self._read(file)
            except ForgePilotError as exc:
                if exc.code in {"repository_binary", "repository_encoding"}:
                    skipped += 1
                    continue
                raise
            data = text.encode("utf-8")
            if scanned_bytes + len(data) > MAX_SEARCH_BYTES:
                reasons.add("search_byte_limit")
                break
            scanned_bytes += len(data)
            if limited:
                reasons.add("file_byte_limit")
            # Search only the bounded, verified snapshot. LF defines source lines;
            # Unicode paragraph separators remain part of the source text.
            for number, content in enumerate(text.split("\n"), 1):
                if number % 256 == 0:
                    await asyncio.sleep(0)
                if content.endswith("\r"):
                    content = content[:-1]
                if query not in content:
                    continue
                match = {"path": file, "line": number, "text": content}
                cost = len(json.dumps(match, ensure_ascii=False)) + 2
                if len(matches) >= max_results or output_chars + cost > MAX_OUTPUT_CHARS:
                    reasons.add("match_or_output_limit")
                    return json.dumps({"matches": matches, "truncated": True, "limits": sorted(reasons),
                                       "skipped_non_utf8_or_binary": skipped}, ensure_ascii=False)
                output_chars += cost
                matches.append(match)
        return json.dumps({"matches": matches, "truncated": bool(reasons), "limits": sorted(reasons),
                           "skipped_non_utf8_or_binary": skipped}, ensure_ascii=False)
