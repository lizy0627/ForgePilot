"""Incremental repository indexing over the established safe repository adapters."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
import fnmatch
import inspect
from pathlib import Path
import re

from ..errors import ForgePilotError
from ..repository import Repository
from .config import RagConfig
from .embedding import EmbeddingProvider
from .models import (CodeChunk, IndexFileState, IndexProgress, checked_relative_path,
                     checked_repository_id)
from .store import RagFileStore
from .vector_store import VectorStore


ProgressCallback = Callable[[IndexProgress], Awaitable[None] | None]
_UNINDEXED_HASH = "0" * 64
_CHUNK_LINES = 120
_CHUNK_OVERLAP = 20
_LANGUAGES = {
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".h": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".py": "python",
    ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".cs": "csharp",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".kt": "kotlin",
    ".kts": "kotlin", ".rb": "ruby", ".php": "php", ".lua": "lua",
    ".sql": "sql", ".sh": "shell", ".ps1": "powershell", ".cmake": "cmake",
    ".qml": "qml",
}
_DECLARATION = re.compile(
    r"^\s*(?:(async)\s+)?(class|def|function|func|fn|struct|enum|interface|namespace)\s+"
    r"([A-Za-z_$][\w$]*)",
)
_ASSIGNED_FUNCTION = re.compile(
    r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=.*(?:=>|function\b)")
_C_LIKE_FUNCTION = re.compile(
    r"^\s*(?:[A-Za-z_$][\w$:<>,*&\[\]]*\s+)+([A-Za-z_$][\w$]*)\s*"
    r"\([^;{}]*\)\s*(?:const\s*)?(?:\{|$)")
_CONTROL_WORDS = {"catch", "for", "if", "return", "sizeof", "switch", "while"}


def _language(path: str) -> str:
    candidate = Path(path)
    if candidate.name.casefold() == "cmakelists.txt":
        return "cmake"
    return _LANGUAGES.get(candidate.suffix.casefold(), "text")


def _symbol(lines: Sequence[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _DECLARATION.match(line)
        if match:
            kind = "function" if match.group(2) in {"def", "function", "func", "fn"} else match.group(2)
            return match.group(3), kind
        match = _ASSIGNED_FUNCTION.match(line)
        if match:
            return match.group(1), "function"
        match = _C_LIKE_FUNCTION.match(line)
        if match and match.group(1) not in _CONTROL_WORDS:
            return match.group(1), "function"
    return None, None


def _chunks(repository_id: str, path: str, text: str, content_hash: str) -> list[CodeChunk]:
    if not text:
        return []
    lines = text.split("\n")
    result = []
    step = _CHUNK_LINES - _CHUNK_OVERLAP
    for offset in range(0, len(lines), step):
        selected = lines[offset:offset + _CHUNK_LINES]
        if not selected:
            break
        content = "\n".join(selected)
        if not content and offset + len(selected) == len(lines):
            break
        symbol, symbol_type = _symbol(selected)
        result.append(CodeChunk(
            repository_id=repository_id,
            path=path,
            language=_language(path),
            symbol=symbol,
            symbol_type=symbol_type,
            start_line=offset + 1,
            end_line=offset + len(selected),
            content=content,
            file_hash=content_hash,
        ))
        if offset + len(selected) >= len(lines):
            break
    return result


def _embedding_text(chunk: CodeChunk) -> str:
    metadata = [f"Language: {chunk.language}", f"Path: {chunk.path}"]
    if chunk.symbol:
        suffix = f" ({chunk.symbol_type})" if chunk.symbol_type else ""
        metadata.append(f"Symbol: {chunk.symbol}{suffix}")
    return "\n".join(metadata) + "\n\n" + chunk.content


class RepositoryIndexer:
    """Incrementally index one repository without participating in Agent runtime startup."""

    def __init__(self, repository_id: str, repository: Repository, config: RagConfig,
                 embedding_provider: EmbeddingProvider | None, vector_store: VectorStore | None,
                 file_store: RagFileStore) -> None:
        self.repository_id = checked_repository_id(repository_id)
        self.repository = repository
        self.config = RagConfig.model_validate(config)
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.file_store = file_store
        self._run_lock = asyncio.Lock()
        states = self.file_store.list(self.repository_id)
        chunks = sum(state.chunk_count for state in states
                     if state.content_hash != _UNINDEXED_HASH)
        disabled = (not self.config.enabled or embedding_provider is None or vector_store is None)
        status = "disabled" if disabled else ("degraded" if any(
            state.status == "failed" for state in states) else "pending" if any(
                state.status == "pending" for state in states) else "ready" if states else "empty")
        self._status = IndexProgress(
            repository_id=self.repository_id, status=status, chunks=chunks)

    @property
    def available(self) -> bool:
        return self._status.status != "disabled"

    def get_status(self) -> IndexProgress:
        return self._status

    def _matches(self, path: str) -> bool:
        folded = path.casefold()
        name = Path(path).name.casefold()
        included = any(fnmatch.fnmatchcase(folded, pattern.casefold())
                       or fnmatch.fnmatchcase(name, pattern.casefold())
                       for pattern in self.config.include)
        excluded = any(fnmatch.fnmatchcase(folded, pattern.casefold())
                       or any(fnmatch.fnmatchcase(part.casefold(), pattern.casefold())
                              for part in Path(path).parts)
                       for pattern in self.config.exclude)
        return included and not excluded

    @staticmethod
    def _cancelled(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()

    async def _publish(self, callback: ProgressCallback | None) -> None:
        if callback is None:
            return
        try:
            pending = callback(self._status)
            if inspect.isawaitable(pending):
                await pending
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ForgePilotError("rag_progress", "RAG progress callback failed") from None

    def _set_status(self, **changes) -> None:
        values = self._status.model_dump()
        values.update(changes)
        self._status = IndexProgress.model_validate(values)

    def _stored_chunks(self) -> int:
        return sum(state.chunk_count for state in self.file_store.list(self.repository_id)
                   if state.content_hash != _UNINDEXED_HASH)

    def _state(self, path: str, status: str, previous: IndexFileState | None = None, *,
               content_hash: str | None = None, chunk_count: int | None = None) -> IndexFileState:
        return IndexFileState(
            repository_id=self.repository_id,
            path=path,
            content_hash=(content_hash if content_hash is not None else
                          previous.content_hash if previous is not None else _UNINDEXED_HASH),
            indexed_at=datetime.now(timezone.utc),
            chunk_count=(chunk_count if chunk_count is not None else
                         previous.chunk_count if previous is not None else 0),
            status=status,
        )

    def mark_dirty(self, path: str) -> IndexFileState:
        normalized = checked_relative_path(self.repository.normalize_plan_path(path))
        previous = self.file_store.get(self.repository_id, normalized)
        state = self.file_store.upsert(self._state(normalized, "pending", previous))
        self._status = IndexProgress(
            repository_id=self.repository_id, status="pending", chunks=self._stored_chunks())
        return state

    async def _embed(self, texts: list[str], cancel_event: asyncio.Event | None) -> list[list[float]]:
        self._cancelled(cancel_event)
        if cancel_event is None:
            return await self.embedding_provider.embed_documents(texts)
        embedding = asyncio.create_task(self.embedding_provider.embed_documents(texts))
        cancellation = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                (embedding, cancellation), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            embedding.cancel()
            cancellation.cancel()
            await asyncio.gather(embedding, cancellation, return_exceptions=True)
            raise
        if embedding in done:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
            return await embedding
        embedding.cancel()
        await asyncio.gather(embedding, return_exceptions=True)
        raise asyncio.CancelledError()

    async def _replace_vectors(self, path: str, chunks: list[CodeChunk],
                               vectors: list[list[float]]) -> tuple[int, bool]:
        # Finish a started mutation even if the outer task is cancelled. This
        # prevents a background Qdrant thread from racing the SQLite commit.
        replacement = asyncio.create_task(self.vector_store.replace_file(
            self.repository_id, path, chunks, vectors))
        try:
            return await asyncio.shield(replacement), False
        except asyncio.CancelledError:
            return await replacement, True

    async def _index_one(self, path: str, cancel_event: asyncio.Event | None) -> tuple[str, int]:
        previous = self.file_store.get(self.repository_id, path)
        try:
            self._cancelled(cancel_event)
            text, truncated, content_hash = self.repository._read_snapshot(path)
            if truncated or content_hash is None or len(text.encode("utf-8")) > self.config.max_file_bytes:
                raise ForgePilotError("rag_file_limit", "RAG source file exceeds the configured limit")
            if (previous is not None and previous.status == "indexed"
                    and previous.content_hash == content_hash):
                return "skipped", 0
            self.file_store.upsert(self._state(path, "pending", previous))
            chunks = _chunks(self.repository_id, path, text, content_hash)
            vectors = await self._embed(
                [_embedding_text(chunk) for chunk in chunks], cancel_event) if chunks else []
            written, task_cancelled = await self._replace_vectors(path, chunks, vectors)
            self.file_store.upsert(self._state(
                path, "indexed", content_hash=content_hash, chunk_count=written))
            if task_cancelled or (cancel_event is not None and cancel_event.is_set()):
                raise asyncio.CancelledError()
            return "indexed", written
        except asyncio.CancelledError:
            raise
        except Exception:
            # Preserve the last successfully indexed hash. A failed first attempt
            # uses a sentinel that can never equal a real SHA-256 digest in practice.
            try:
                self.file_store.upsert(self._state(path, "failed", previous))
            except Exception:
                pass
            return "failed", 0

    async def _remove_one(self, path: str) -> int:
        previous = self.file_store.get(self.repository_id, path)
        try:
            # Persist intent before deleting vectors. If the process stops after
            # the external mutation but before the row delete, restart reports a
            # retryable pending index instead of a false ready state.
            if previous is not None:
                self.file_store.upsert(self._state(path, "pending", previous))
            removed = await self.vector_store.delete_file(self.repository_id, path)
            self.file_store.delete(self.repository_id, path)
            return removed
        except asyncio.CancelledError:
            if previous is not None:
                try:
                    self.file_store.upsert(self._state(path, "failed", previous))
                except Exception:
                    pass
            raise
        except Exception:
            if previous is not None:
                try:
                    self.file_store.upsert(self._state(path, "failed", previous))
                except Exception:
                    pass
            raise

    async def _finish_item(self, outcome: str, callback: ProgressCallback | None) -> None:
        changes = {
            "processed": self._status.processed + 1,
            "skipped": self._status.skipped + (outcome == "skipped"),
            "failed": self._status.failed + (outcome == "failed"),
            "chunks": self._stored_chunks(),
        }
        self._set_status(**changes)
        await self._publish(callback)

    def _final_status(self) -> str:
        if self._status.failed:
            return "failed" if self._status.failed == self._status.processed else "degraded"
        return "ready" if self._status.chunks else "empty"

    async def index_repository(self, *, progress: ProgressCallback | None = None,
                               cancel_event: asyncio.Event | None = None) -> IndexProgress:
        if not self.available:
            await self._publish(progress)
            return self._status
        async with self._run_lock:
            try:
                self._cancelled(cancel_event)
                files, truncated = await self.repository._files(".")
                if truncated:
                    raise ForgePilotError("rag_scan_limit", "Repository file scan exceeded safe limits")
                selected = [path for path in files if self._matches(path)]
                known = {state.path for state in self.file_store.list(self.repository_id)}
                deleted = sorted(known - set(selected))
                self._status = IndexProgress(
                    repository_id=self.repository_id,
                    status="indexing",
                    total_files=len(selected) + len(deleted),
                    chunks=self._stored_chunks(),
                )
                await self._publish(progress)
                for path in selected:
                    self._cancelled(cancel_event)
                    outcome, _ = await self._index_one(path, cancel_event)
                    await self._finish_item(outcome, progress)
                for path in deleted:
                    self._cancelled(cancel_event)
                    try:
                        await self._remove_one(path)
                        self._cancelled(cancel_event)
                        outcome = "indexed"
                    except Exception:
                        outcome = "failed"
                    await self._finish_item(outcome, progress)
                self._set_status(status=self._final_status(), chunks=self._stored_chunks())
                await self._publish(progress)
                return self._status
            except asyncio.CancelledError:
                self._set_status(status="cancelled", chunks=self._stored_chunks())
                await asyncio.shield(self._publish(progress))
                raise

    async def index_file(self, path: str, *, progress: ProgressCallback | None = None,
                         cancel_event: asyncio.Event | None = None) -> IndexProgress:
        if not self.available:
            await self._publish(progress)
            return self._status
        async with self._run_lock:
            normalized = checked_relative_path(self.repository.normalize_plan_path(path))
            self._status = IndexProgress(
                repository_id=self.repository_id, status="indexing", total_files=1,
                chunks=self._stored_chunks())
            await self._publish(progress)
            try:
                outcome, _ = await self._index_one(normalized, cancel_event)
                await self._finish_item(outcome, progress)
                self._set_status(status=self._final_status(), chunks=self._stored_chunks())
                await self._publish(progress)
                return self._status
            except asyncio.CancelledError:
                self._set_status(status="cancelled", chunks=self._stored_chunks())
                await asyncio.shield(self._publish(progress))
                raise

    async def remove_file(self, path: str) -> int:
        if not self.available:
            return 0
        async with self._run_lock:
            normalized = checked_relative_path(self.repository.normalize_plan_path(path))
            return await self._remove_one(normalized)

    async def clear_repository(self) -> int:
        if not self.available:
            return 0
        async with self._run_lock:
            states = self.file_store.list(self.repository_id)
            for state in states:
                self.file_store.upsert(self._state(state.path, "pending", state))
            try:
                await self.vector_store.clear_repository(self.repository_id)
            except BaseException:
                for state in states:
                    try:
                        self.file_store.upsert(self._state(state.path, "failed", state))
                    except Exception:
                        pass
                raise
            removed = self.file_store.clear_repository(self.repository_id)
            self._status = IndexProgress(repository_id=self.repository_id, status="empty")
            return removed
