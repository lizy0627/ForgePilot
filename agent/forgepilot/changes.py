"""Task-local original bytes, conflict-checked writes, diff and explicit restoration."""

import asyncio
from dataclasses import dataclass
import difflib
import hashlib
import json
import os

from .errors import ForgePilotError
from .file_access import delete_open_file, pinned_file

MAX_SNAPSHOT_BYTES = 262_144
MAX_TOTAL_SNAPSHOT_BYTES = 8_388_608


@dataclass
class FileSnapshot:
    path: str
    original: bytes | None  # None means absent, b"" means an existing empty file.
    last_written: bytes | None = None
    restored: bool = False


def digest(value: bytes | None):
    return hashlib.sha256(value).hexdigest() if value is not None else None


def read_bytes(stream):
    if stream is None:
        return None
    stream.seek(0)
    data = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ForgePilotError("snapshot_limit", "File exceeds the 256 KiB complete-snapshot limit")
    return data


def write_bytes(stream, data):
    stream.seek(0)
    remaining = memoryview(data)
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise OSError("Incomplete write")
        remaining = remaining[count:]
    stream.truncate(len(data))
    stream.flush()
    os.fsync(stream.fileno())


class ChangeSet:
    def __init__(self, repository, ensure_write, trace, task_id):
        self.repository = repository
        self.ensure_write = ensure_write
        self.trace = trace
        self.task_id = task_id
        self.snapshots: dict[str, FileSnapshot] = {}
        self.content_guard = None

    async def write_file(self, path: str, content: str) -> str:
        await asyncio.sleep(0)  # Deliver cancellation before entering the bounded mutation.
        normalized = self.repository.normalize_plan_path(path)
        self.ensure_write(normalized)
        if "\x00" in content:
            raise ForgePilotError("repository_binary", "write_file only accepts UTF-8 text without NUL")
        data = content.encode("utf-8")
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ForgePilotError("write_limit", "Content exceeds the file limit")
        return json.dumps(self._mutate_file(normalized, lambda current: data, allow_create=True), ensure_ascii=False)

    async def edit_file(self, path: str, edits: list[dict]) -> str:
        await asyncio.sleep(0)
        normalized = self.repository.normalize_plan_path(path)
        self.ensure_write(normalized)
        # Detect missing parents without asking pinned_file to create anything.
        if not self.repository._path(normalized, allow_missing=True).exists():
            raise ForgePilotError("edit_file_missing", "edit_file requires an existing file")

        def make_bytes(current):
            bom = b"\xef\xbb\xbf" if current.startswith(b"\xef\xbb\xbf") else b""
            body = current[len(bom):]
            without_crlf = body.replace(b"\r\n", b"")
            if b"\r" in without_crlf or (b"\r\n" in body and b"\n" in without_crlf):
                raise ForgePilotError("edit_newline_unsupported", "File contains mixed newlines or standalone CR")
            newline = "\r\n" if b"\r\n" in body else "\n"
            ranges = []
            for number, edit in enumerate(edits, 1):
                old, new = (edit[field].replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
                            for field in ("old_text", "new_text"))
                start = body.find(old)
                if start < 0:
                    raise ForgePilotError("edit_not_found", f"Edit {number}: exact text was not found")
                if body.find(old, start + 1) >= 0:
                    raise ForgePilotError("edit_ambiguous", f"Edit {number}: text is not unique; include more context")
                ranges.append((start, start + len(old), new, number))
            ranges.sort(key=lambda item: item[0])
            for previous, following in zip(ranges, ranges[1:]):
                if following[0] < previous[1]:
                    raise ForgePilotError("edit_overlap", f"Edits {previous[3]} and {following[3]} overlap")
            for start, end, new, _ in reversed(ranges):
                body = body[:start] + new + body[end:]
            return bom + body

        result = self._mutate_file(normalized, make_bytes, allow_create=False)
        # An earlier write_file may have created this file, but edit_file never does.
        result.update(edits_applied=len(edits), created=False)
        return json.dumps(result, ensure_ascii=False)

    def _mutate_file(self, normalized, make_bytes, *, allow_create):
        """Mutate an authorized path; callers deliver cancellation before entering.

        make_bytes computes the complete target from the pinned current bytes. All
        computation and validation finish before a snapshot or write is attempted.
        """
        key = normalized.casefold()
        snapshot = self.snapshots.get(key)
        if snapshot is None and len(self.snapshots) >= 100:
            raise ForgePilotError("snapshot_limit", "Task has reached 100 file snapshots")
        # No await between permission check, snapshot, compare and write. The same
        # exclusive handle pins the content; directory handles pin its location.
        with pinned_file(self.repository, normalized, create=allow_create and snapshot is None, writable=True) as (stream, created):
            attempted = False
            try:
                self.ensure_write(normalized)
                if not allow_create and stream is None:
                    raise ForgePilotError("edit_file_missing", "edit_file requires an existing file")
                current = None if created else read_bytes(stream)
                if snapshot is not None and (snapshot.restored or current != snapshot.last_written):
                    raise ForgePilotError("write_conflict", "File changed since the last Agent write; user content preserved")
                if snapshot is None:
                    version = self.repository._read_versions.get(key)
                    if created and version is not None:
                        raise ForgePilotError("write_conflict", "File was deleted since read_file; user content preserved")
                    if current is not None:
                        if version is None:
                            raise ForgePilotError("write_requires_read", "Read the existing file with read_file before submitting complete replacement content")
                        if digest(current) != version:
                            raise ForgePilotError("write_conflict", "File changed since read_file; read it again and regenerate the change")
                if current is not None and (snapshot is None or not allow_create):
                    # A partial earlier I/O failure may have split a UTF-8 character.
                    try:
                        current.decode("utf-8-sig")
                        if b"\x00" in current:
                            raise UnicodeError()
                    except UnicodeError:
                        raise ForgePilotError("repository_encoding", "Only complete UTF-8 text files can be modified") from None
                data = make_bytes(current)
                if len(data) > MAX_SNAPSHOT_BYTES:
                    raise ForgePilotError("write_limit", "Content exceeds the file limit")
                if self.content_guard is not None:
                    self.content_guard(normalized, current, data.decode("utf-8"))
                if snapshot is None:
                    total = sum(len(s.original or b"") for s in self.snapshots.values()) + len(current or b"")
                    if total > MAX_TOTAL_SNAPSHOT_BYTES:
                        raise ForgePilotError("snapshot_limit", "Original snapshots exceed 8 MiB")
                    snapshot = FileSnapshot(normalized, current)
                    self.snapshots[key] = snapshot
                attempted = True
                try:
                    write_bytes(stream, data)
                finally:
                    # Retain even a partial I/O result for an explicit later recovery.
                    snapshot.last_written = read_bytes(stream)
                self.trace.emit(self.task_id, "file_changed", {
                    "path": snapshot.path, "created": snapshot.original is None,
                    "before_sha256": digest(snapshot.original), "after_sha256": digest(snapshot.last_written),
                })
                return {"path": snapshot.path, "bytes_written": len(data),
                        "created": snapshot.original is None}
            finally:
                if created and not attempted:
                    delete_open_file(stream)

    def diff(self, *, inspect_current=True, limit=12_000) -> dict:
        entries = []
        remaining = limit
        truncated = False
        for snapshot in self.snapshots.values():
            if snapshot.last_written is None:
                continue
            try:
                if inspect_current:
                    with pinned_file(self.repository, snapshot.path) as (stream, _):
                        current = read_bytes(stream)
                    conflict = not snapshot.restored and current != snapshot.last_written
                else:
                    conflict = None  # A trace may run while write_file still owns the exclusive handle.
                current_error = None
            except ForgePilotError as exc:
                conflict, current_error = True, exc.code
            old = (snapshot.original or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
            new = snapshot.last_written.decode("utf-8", errors="replace").splitlines(keepends=True)
            lines = difflib.unified_diff(old, new, fromfile=("before/" + snapshot.path) if snapshot.original is not None else "/dev/null",
                                         tofile="agent/" + snapshot.path)
            pieces = []
            for line in lines:
                rendered = line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                if remaining is None:
                    pieces.append(rendered)
                    continue
                if len(rendered) > remaining:
                    truncated = True
                pieces.append(rendered[:remaining])
                remaining -= min(remaining, len(rendered))
                if remaining <= 0:
                    truncated = True
                    break
            entries.append({"path": snapshot.path, "created": snapshot.original is None,
                            "original_sha256": digest(snapshot.original), "agent_sha256": digest(snapshot.last_written),
                            "diff": "".join(pieces), "conflict": conflict, "current_error": current_error,
                            "restored": snapshot.restored})
        return {"basis": "first-write snapshot -> last Agent bytes (not HEAD or current user edits)",
                "files": entries, "truncated": truncated}

    async def restore(self) -> dict:
        results = []
        for snapshot in self.snapshots.values():
            await asyncio.sleep(0)
            if snapshot.restored or snapshot.last_written is None:
                results.append({"path": snapshot.path, "status": "already_restored" if snapshot.restored else "unchanged"})
                continue
            try:
                with pinned_file(self.repository, snapshot.path, writable=True) as (stream, _):
                    current = read_bytes(stream)
                    if current != snapshot.last_written or stream is None:
                        raise ForgePilotError("restore_conflict", "File differs from the Agent version; preserved")
                    if snapshot.original is None:
                        delete_open_file(stream)
                    else:
                        write_bytes(stream, snapshot.original)
                    snapshot.restored = True
                result = {"path": snapshot.path, "status": "restored"}
            except ForgePilotError as exc:
                result = {"path": snapshot.path, "status": "conflict", "error": exc.code}
            results.append(result)
            self.trace.emit(self.task_id, "file_restore", result)
        return {"files": results, "conflicts": sum(r["status"] == "conflict" for r in results)}
