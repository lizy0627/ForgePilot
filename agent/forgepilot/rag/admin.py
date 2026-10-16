"""Repository-scoped RAG indexing coordination for local management APIs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from uuid import uuid4

from ..errors import ForgePilotError
from .integration import RagManager, repository_id_for
from .models import IndexProgress


RepositoryOpener = Callable[[str], Awaitable[object]]


@dataclass(slots=True)
class _Run:
    operation_id: str
    cancel_event: asyncio.Event
    progress: IndexProgress
    task: asyncio.Task | None = None
    revision: int = 0
    cancelling: bool = False
    error_code: str | None = None


class RagIndexCoordinator:
    """Own background index runs on TaskHost's existing worker loop."""

    def __init__(self, manager: RagManager, open_repository: RepositoryOpener) -> None:
        self.manager = manager
        self.open_repository = open_repository
        self._runs: dict[str, _Run] = {}
        self._closed = False

    async def _resolve(self, repository_path: str):
        if self._closed:
            raise ForgePilotError("server_stopping", "Server is shutting down")
        # Repository.open performs the existing Git-root, reparse-point and
        # canonical-path checks. Never derive authority from the request path.
        repository = await self.open_repository(repository_path)
        return repository, repository_id_for(repository.root)

    def _persisted(self, repository_id: str) -> dict:
        states = self.manager.file_store.list(repository_id)
        indexed = [state for state in states if state.status == "indexed"]
        failed = sum(state.status == "failed" for state in states)
        dirty = sum(state.status == "pending" for state in states)
        chunks = sum(state.chunk_count for state in states)
        if not self.manager.enabled:
            status = "disabled"
        elif not self.manager.index_available:
            status = "unavailable"
        elif dirty:
            status = "pending"
        elif failed and failed == len(states):
            status = "failed"
        elif failed:
            status = "degraded"
        else:
            status = "ready" if states else "empty"
        last = max((state.indexed_at for state in indexed), default=None)
        return {
            "enabled": self.manager.enabled,
            "available": self.manager.index_available,
            "status": status,
            "total_files": len(states),
            "processed": len(states),
            "skipped": 0,
            "failed": failed,
            "chunks": chunks,
            "file_count": len(states),
            "dirty_file_count": dirty,
            "failed_file_count": failed,
            "last_indexed_at": last.isoformat() if isinstance(last, datetime) else None,
            "running": False,
            "operation_id": None,
            "revision": 0,
            "error_code": self.manager.reason if not self.manager.index_available else None,
        }

    def snapshot(self, repository_id: str) -> dict:
        values = self._persisted(repository_id)
        run = self._runs.get(repository_id)
        if run is None:
            return {"repository_id": repository_id, **values}
        progress = run.progress
        running = run.task is None or not run.task.done()
        values.update(
            status="cancelling" if run.cancelling and running else progress.status,
            total_files=progress.total_files,
            processed=progress.processed,
            skipped=progress.skipped,
            failed=progress.failed,
            chunks=progress.chunks,
            running=running,
            operation_id=run.operation_id,
            revision=run.revision,
            error_code=run.error_code,
        )
        return {"repository_id": repository_id, **values}

    async def status(self, repository_path: str) -> dict:
        _, repository_id = await self._resolve(repository_path)
        return self.snapshot(repository_id)

    async def start(self, repository_path: str) -> dict:
        repository, repository_id = await self._resolve(repository_path)
        if not self.manager.enabled:
            raise ForgePilotError("rag_disabled", "Code indexing is disabled")
        if not self.manager.index_available:
            raise ForgePilotError("rag_unavailable", "Code indexing is unavailable")
        current = self._runs.get(repository_id)
        if current is not None and current.task is not None and not current.task.done():
            raise ForgePilotError("rag_index_busy", "This repository is already being indexed")
        services = self.manager.for_repository(repository)
        if services is None or not services.indexer.available:
            raise ForgePilotError("rag_unavailable", "Code indexing is unavailable")
        if not self.manager.begin_index(repository_id):
            raise ForgePilotError("rag_index_busy", "This repository is already being indexed")
        progress = IndexProgress(repository_id=repository_id, status="indexing")
        run = _Run(uuid4().hex, asyncio.Event(), progress, revision=1)
        self._runs[repository_id] = run
        try:
            run.task = asyncio.create_task(self._drive(repository_id, run, services.indexer))
        except BaseException:
            self.manager.finish_index(repository_id)
            self._runs.pop(repository_id, None)
            raise
        return self.snapshot(repository_id)

    async def _drive(self, repository_id: str, run: _Run, indexer) -> None:
        async def publish(progress: IndexProgress) -> None:
            if self._runs.get(repository_id) is not run:
                return
            run.progress = progress
            run.revision += 1

        try:
            run.progress = await indexer.index_repository(
                progress=publish, cancel_event=run.cancel_event)
        except asyncio.CancelledError:
            # RepositoryIndexer has already published a retryable cancelled state.
            if run.progress.status != "cancelled":
                run.progress = IndexProgress(
                    repository_id=repository_id, status="cancelled",
                    total_files=run.progress.total_files,
                    processed=run.progress.processed,
                    skipped=run.progress.skipped,
                    failed=run.progress.failed,
                    chunks=run.progress.chunks,
                )
        except Exception as exc:
            run.error_code = exc.code if isinstance(exc, ForgePilotError) else "rag_index_failed"
            total = max(run.progress.total_files, 1)
            processed = max(run.progress.processed, 1)
            run.progress = IndexProgress(
                repository_id=repository_id, status="failed",
                total_files=total,
                processed=processed,
                skipped=run.progress.skipped,
                failed=max(run.progress.failed, 1),
                chunks=run.progress.chunks,
            )
        finally:
            run.cancelling = False
            run.revision += 1
            self.manager.finish_index(repository_id)

    async def cancel(self, repository_path: str) -> dict:
        _, repository_id = await self._resolve(repository_path)
        run = self._runs.get(repository_id)
        if run is None or run.task is None or run.task.done():
            raise ForgePilotError("rag_index_not_running", "This repository is not being indexed")
        run.cancelling = True
        run.revision += 1
        run.cancel_event.set()
        await asyncio.shield(run.task)
        return self.snapshot(repository_id)

    async def clear(self, repository_path: str) -> dict:
        repository, repository_id = await self._resolve(repository_path)
        run = self._runs.get(repository_id)
        if run is not None and run.task is not None and not run.task.done():
            raise ForgePilotError("rag_index_busy", "Cancel indexing before deleting this index")
        if not self.manager.index_available:
            raise ForgePilotError("rag_unavailable", "Code index storage is unavailable")
        services = self.manager.for_repository(repository)
        if services is None:
            raise ForgePilotError("rag_unavailable", "Code indexing is unavailable")
        if not self.manager.begin_index(repository_id):
            raise ForgePilotError("rag_index_busy", "This repository index is busy")
        try:
            removed = await services.indexer.clear_repository()
        finally:
            self.manager.finish_index(repository_id)
        self._runs.pop(repository_id, None)
        return {**self.snapshot(repository_id), "deleted_files": removed}

    async def close(self) -> None:
        self._closed = True
        pending = []
        for run in self._runs.values():
            if run.task is not None and not run.task.done():
                run.cancel_event.set()
                pending.append(run.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
