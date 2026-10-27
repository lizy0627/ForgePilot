"""Task-local original bytes, conflict-checked writes, diff and explicit restoration."""

import asyncio
import ast
from dataclasses import dataclass
import difflib
import hashlib
import json

from .errors import ForgePilotError
from .path_identity import path_key
from .file_access import CommittedPatchError, atomic_replace, delete_open_file, pinned_file

MAX_SNAPSHOT_BYTES = 262_144
MAX_TOTAL_SNAPSHOT_BYTES = 8_388_608


@dataclass
class FileSnapshot:
    path: str
    original: bytes | None  # None means absent, b"" means an existing empty file.
    last_written: bytes | None = None
    restored: bool = False
    deleted: bool = False


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


class ChangeSet:
    def __init__(self, repository, ensure_write, trace, task_id):
        self.repository = repository
        self.ensure_write = ensure_write
        self.trace = trace
        self.task_id = task_id
        self.snapshots: dict[str, FileSnapshot] = {}
        self.content_guard = None
        self.scope_guard = None
        self._scope_baseline = None

    def capture_scope(self):
        if self._scope_baseline is None:
            self._scope_baseline = self.repository.audit_manifest()
        else:
            self.verify_scope()
        self.scope_guard = self.verify_scope

    def verify_scope(self, ignore_path=None):
        if self._scope_baseline is None:
            raise ForgePilotError("scope_unapproved", "Capture the approved workspace before modifying")
        expected = dict(self._scope_baseline)
        for key, snapshot in self.snapshots.items():
            if snapshot.restored:
                continue
            if snapshot.deleted:
                expected.pop(key, None)
            elif snapshot.last_written is not None:
                expected[key] = digest(snapshot.last_written)
        actual = self.repository.audit_manifest()
        if ignore_path is not None:
            expected.pop(path_key(ignore_path), None)
            actual.pop(path_key(ignore_path), None)
        if actual != expected:
            raise ForgePilotError("scope_violation", "Workspace changed outside the recorded approved patches")

    async def apply_patch(self, path: str, expected_hash: str, edits: list[dict]) -> str:
        """Apply exact edits only to the explicit SHA256 baseline supplied by caller."""
        self._validate_hash(expected_hash)
        return await self.edit_file(path, edits, expected_hash=expected_hash)

    async def create_file(self, path: str, content: str) -> str:
        return await self.write_file(path, content, require_new=True)

    async def delete_new_file(self, path: str, expected_hash: str) -> str:
        await asyncio.sleep(0)
        normalized = self.repository.normalize_plan_path(path)
        self.ensure_write(normalized)
        self._validate_hash(expected_hash)
        snapshot = self.snapshots.get(path_key(normalized))
        if snapshot is None or snapshot.original is not None or snapshot.deleted or snapshot.restored:
            raise ForgePilotError("delete_denied", "Only this task's newly created files can be deleted")
        if self.scope_guard is not None:
            self.scope_guard(normalized)
        with pinned_file(self.repository, normalized, writable=True) as (stream, _):
            self.ensure_write(normalized)
            current = read_bytes(stream)
            if current != snapshot.last_written or digest(current) != expected_hash or stream is None:
                raise ForgePilotError("write_conflict", "Delete baseline changed; user bytes preserved")
            delete_open_file(stream)
            snapshot.deleted = True
        if self.scope_guard is not None:
            self.scope_guard()
        self.trace.emit(self.task_id, "file_changed", {"path": normalized, "deleted": True,
            "before_sha256": expected_hash, "after_sha256": None})
        return json.dumps({"path": normalized, "deleted": True})

    @staticmethod
    def _validate_hash(value):
        if (not isinstance(value, str) or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)):
            raise ForgePilotError("invalid_arguments", "expected_hash must be a lowercase SHA256")

    async def write_file(self, path: str, content: str, *, require_new=False) -> str:
        await asyncio.sleep(0)  # Deliver cancellation before entering the bounded mutation.
        normalized = self.repository.normalize_plan_path(path)
        self.ensure_write(normalized)
        if "\x00" in content:
            raise ForgePilotError("repository_binary", "write_file only accepts UTF-8 text without NUL")
        data = content.encode("utf-8")
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ForgePilotError("write_limit", "Content exceeds the file limit")
        return json.dumps(self._mutate_file(normalized, lambda current: data, allow_create=True,
                                           require_new=require_new), ensure_ascii=False)

    async def edit_file(self, path: str, edits: list[dict], *, expected_hash=None) -> str:
        await asyncio.sleep(0)
        normalized = self.repository.normalize_plan_path(path)
        self.ensure_write(normalized)
        if expected_hash is not None:
            self._validate_hash(expected_hash)
        if (not isinstance(edits, list) or not 1 <= len(edits) <= 8
                or any(not isinstance(e, dict) or set(e) != {"old_text", "new_text"}
                       or not isinstance(e["old_text"], str) or not e["old_text"]
                       or not isinstance(e["new_text"], str)
                       or "\x00" in e["old_text"] + e["new_text"] for e in edits)):
            raise ForgePilotError("invalid_arguments", "Supply 1-8 nonempty exact UTF-8 text edits without NUL")
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

        result = self._mutate_file(normalized, make_bytes, allow_create=False, expected_hash=expected_hash)
        # An earlier write_file may have created this file, but edit_file never does.
        result.update(edits_applied=len(edits), created=False)
        return json.dumps(result, ensure_ascii=False)

    def _mutate_file(self, normalized, make_bytes, *, allow_create, expected_hash=None, require_new=False):
        """Mutate an authorized path; callers deliver cancellation before entering.

        make_bytes computes the complete target from the pinned current bytes. All
        computation and validation finish before a snapshot or write is attempted.
        """
        key = path_key(normalized)
        snapshot = self.snapshots.get(key)
        if snapshot is None and len(self.snapshots) >= 100:
            raise ForgePilotError("snapshot_limit", "Task has reached 100 file snapshots")
        if self.scope_guard is not None:
            self.scope_guard(normalized)
        # No await between permission check, snapshot, compare and write. The same
        # exclusive handle pins the content; directory handles pin its location.
        with pinned_file(self.repository, normalized, create=allow_create and snapshot is None,
                         writable=True, replaceable=True) as (stream, created):
            attempted = False
            try:
                self.ensure_write(normalized)
                if not allow_create and stream is None:
                    raise ForgePilotError("edit_file_missing", "edit_file requires an existing file")
                current = None if created else read_bytes(stream)
                if require_new and not created:
                    raise ForgePilotError("create_conflict", "repository_create_file never overwrites an existing file")
                if expected_hash is not None and digest(current) != expected_hash:
                    raise ForgePilotError("write_conflict", "Patch SHA256 does not match the current bytes")
                if snapshot is not None and (snapshot.restored or snapshot.deleted or current != snapshot.last_written):
                    raise ForgePilotError("write_conflict", "File changed since the last Agent write; user content preserved")
                if snapshot is None:
                    version = self.repository._read_versions.get(key)
                    if created and version is not None:
                        raise ForgePilotError("write_conflict", "File was deleted since read_file; user content preserved")
                    if current is not None:
                        if version is None and expected_hash is None:
                            raise ForgePilotError("write_requires_read", "Read the existing file with read_file before submitting complete replacement content")
                        if expected_hash is None and digest(current) != version:
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
                try:
                    data.decode("utf-8-sig")
                    if b"\x00" in data:
                        raise UnicodeError()
                except UnicodeError:
                    raise ForgePilotError("repository_encoding", "Patch must produce complete UTF-8 without NUL") from None
                if normalized.casefold().endswith(".py"):
                    try:
                        ast.parse(data.decode("utf-8-sig"), filename=normalized)
                    except (SyntaxError, ValueError):
                        raise ForgePilotError("patch_syntax", "Patch produces invalid Python syntax") from None
                if normalized.casefold().endswith(".json"):
                    try:
                        json.loads(data.decode("utf-8-sig"))
                    except ValueError:
                        raise ForgePilotError("patch_syntax", "Patch produces invalid JSON syntax") from None
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
                    after_hash = atomic_replace(self.repository, normalized, stream, data,
                                                expected_sha256=digest(current or b""))
                    snapshot.last_written = data
                except BaseException as exc:
                    if isinstance(exc, CommittedPatchError):
                        snapshot.last_written = data
                    elif created:
                        delete_open_file(stream)
                    if snapshot.last_written is None:
                        self.snapshots.pop(key, None)
                    raise
                self.trace.emit(self.task_id, "file_changed", {
                    "path": snapshot.path, "created": snapshot.original is None,
                    "before_sha256": digest(snapshot.original), "after_sha256": digest(snapshot.last_written),
                })
                result = {"path": snapshot.path, "bytes_written": len(data),
                          "created": snapshot.original is None, "before_sha256": digest(current),
                          "after_sha256": after_hash}
            finally:
                if created and not attempted:
                    delete_open_file(stream)
        if self.scope_guard is not None:
            self.scope_guard()
        return result

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
                    conflict = not snapshot.restored and current != (None if snapshot.deleted else snapshot.last_written)
                else:
                    conflict = None  # A trace may run while write_file still owns the exclusive handle.
                current_error = None
            except ForgePilotError as exc:
                conflict, current_error = True, exc.code
            old = (snapshot.original or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
            new = (b"" if snapshot.deleted else snapshot.last_written).decode("utf-8", errors="replace").splitlines(keepends=True)
            lines = difflib.unified_diff(old, new, fromfile=("before/" + snapshot.path) if snapshot.original is not None else "/dev/null",
                                         tofile="/dev/null" if snapshot.deleted else "agent/" + snapshot.path)
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
                            "original_sha256": digest(snapshot.original), "agent_sha256": None if snapshot.deleted else digest(snapshot.last_written),
                            "deleted": snapshot.deleted,
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
                with pinned_file(self.repository, snapshot.path, writable=True,
                                 replaceable=snapshot.original is not None) as (stream, _):
                    current = read_bytes(stream)
                    if snapshot.deleted and current is None:
                        snapshot.restored = True
                        results.append({"path": snapshot.path, "status": "restored"})
                        continue
                    if current != snapshot.last_written or stream is None:
                        raise ForgePilotError("restore_conflict", "File differs from the Agent version; preserved")
                    if snapshot.original is None:
                        delete_open_file(stream)
                    else:
                        # Restore also stages complete bytes before replacement.
                        # This path's handle is exclusive; replaceable is set below.
                        atomic_replace(self.repository, snapshot.path, stream, snapshot.original,
                                       expected_sha256=digest(current))
                    snapshot.restored = True
                result = {"path": snapshot.path, "status": "restored"}
            except ForgePilotError as exc:
                result = {"path": snapshot.path, "status": "conflict", "error": exc.code}
            results.append(result)
            self.trace.emit(self.task_id, "file_restore", result)
        return {"files": results, "conflicts": sum(r["status"] == "conflict" for r in results)}
