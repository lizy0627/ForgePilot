"""Repository-scoped, durable RAG indexing coordination for management APIs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..errors import ForgePilotError
from .integration import RagManager, repository_id_for
from .models import IndexProgress, IndexRunRecord
from .store import RagRunStore


RepositoryOpener = Callable[[str], Awaitable[object]]
TERMINAL_RUN_STATES = frozenset({"ready", "empty", "degraded", "failed", "cancelled"})
RUN_STATE_TRANSITIONS = {
    "indexing": frozenset({"cancelling", "ready", "empty", "degraded", "failed", "cancelled"}),
    "cancelling": frozenset({"cancelled", "failed"}),
    "ready": frozenset(),
    "empty": frozenset(),
    "degraded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_UNCHANGED = object()


@dataclass(slots=True)
class _Run:
    record: IndexRunRecord
    cancel_event: asyncio.Event
    progress: IndexProgress
    task: asyncio.Task | None = None


class RagIndexCoordinator:
    """Own background runs and durably publish their latest progress snapshot.

    Revisions start at one and increase only when the public operation snapshot
    changes. SSE is deliberately snapshot/coalescing based: reconnecting clients
    receive the latest revision, not a replay of every intermediate counter.
    """

    def __init__(self, manager: RagManager, open_repository: RepositoryOpener) -> None:
        self.manager = manager
        self.open_repository = open_repository
        self.run_store = RagRunStore(manager.file_store.events)
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
        values = {
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
            "operation": None,
            "revision": 0,
            "error_code": self.manager.reason if not self.manager.index_available else None,
        }
        record = self.run_store.get(repository_id)
        # A terminal operation remains reconnectable after restart. A later
        # external dirty mark wins over an old successful terminal snapshot.
        latest_state = max((state.indexed_at for state in states), default=None)
        record_is_current = (latest_state is None or latest_state <= record.updated_at) if record else False
        if (self.manager.enabled and self.manager.index_available and record is not None
                and (record.status == status
                     or (record.status in {"failed", "cancelled"} and record_is_current))):
            values.update(
                status=record.status,
                total_files=record.total_files,
                processed=record.processed,
                skipped=record.skipped,
                failed=record.failed,
                chunks=record.chunks,
                operation_id=record.operation_id,
                operation=record.operation,
                revision=record.revision,
                error_code=record.error_code,
            )
        return values

    def snapshot(self, repository_id: str) -> dict:
        values = self._persisted(repository_id)
        run = self._runs.get(repository_id)
        if run is not None:
            record = run.record
            values.update(
                status=record.status,
                total_files=record.total_files,
                processed=record.processed,
                skipped=record.skipped,
                failed=record.failed,
                chunks=record.chunks,
                running=record.status not in TERMINAL_RUN_STATES,
                operation_id=record.operation_id,
                operation=record.operation,
                revision=record.revision,
                error_code=record.error_code,
            )
        return {"repository_id": repository_id, **values}

    def _advance(self, run: _Run, *, progress: IndexProgress | None = None,
                 status: str | None = None, error_code=_UNCHANGED) -> None:
        old = run.record
        if progress is None:
            progress = run.progress
        next_status = status or progress.status
        if status is None and old.status == "cancelling" and next_status == "indexing":
            next_status = "cancelling"
        if next_status != old.status and next_status not in RUN_STATE_TRANSITIONS[old.status]:
            raise ForgePilotError("rag_index_state", "RAG operation state transition is invalid")
        next_error = old.error_code if error_code is _UNCHANGED else error_code
        public = (next_status, progress.total_files, progress.processed, progress.skipped,
                  progress.failed, progress.chunks, next_error)
        previous = (old.status, old.total_files, old.processed, old.skipped,
                    old.failed, old.chunks, old.error_code)
        run.progress = progress
        if public == previous:
            return
        now = datetime.now(timezone.utc)
        updated = old.model_copy(update={
            "status": next_status,
            "revision": old.revision + 1,
            "total_files": progress.total_files,
            "processed": progress.processed,
            "skipped": progress.skipped,
            "failed": progress.failed,
            "chunks": progress.chunks,
            "error_code": next_error,
            "updated_at": now,
            "finished_at": now if next_status in TERMINAL_RUN_STATES else None,
        })
        updated = IndexRunRecord.model_validate(updated)
        self.run_store.update(updated, previous_revision=old.revision)
        run.record = updated

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
        if current is not None and current.record.status not in TERMINAL_RUN_STATES:
            raise ForgePilotError("rag_index_busy", "This repository is already being indexed")
        services = self.manager.for_repository(repository)
        if services is None or not services.indexer.available:
            raise ForgePilotError("rag_unavailable", "Code indexing is unavailable")
        if not self.manager.begin_index(repository_id):
            raise ForgePilotError("rag_index_busy", "This repository is already being indexed")
        try:
            record = self.run_store.begin(repository_id, "index")
        except BaseException:
            self.manager.finish_index(repository_id)
            raise
        progress = IndexProgress(repository_id=repository_id, status="indexing")
        run = _Run(record, asyncio.Event(), progress)
        self._runs[repository_id] = run
        try:
            run.task = asyncio.create_task(self._drive(repository_id, run, services.indexer))
        except BaseException:
            failed = IndexProgress(repository_id=repository_id, status="failed",
                                   total_files=1, processed=1, failed=1)
            self._advance(run, progress=failed, error_code="rag_index_failed")
            self.manager.finish_index(repository_id)
            raise
        return self.snapshot(repository_id)

    async def _drive(self, repository_id: str, run: _Run, indexer) -> None:
        async def publish(progress: IndexProgress) -> None:
            if self._runs.get(repository_id) is not run:
                return
            error = ("rag_index_file_failed"
                     if progress.status in {"failed", "degraded"} else None)
            self._advance(run, progress=progress, error_code=error)

        try:
            final = await indexer.index_repository(
                progress=publish, cancel_event=run.cancel_event)
            self._advance(run, progress=final,
                          error_code="rag_index_file_failed"
                          if final.status in {"failed", "degraded"} else None)
        except asyncio.CancelledError:
            current = run.progress
            cancelled = IndexProgress(
                repository_id=repository_id, status="cancelled",
                total_files=current.total_files, processed=current.processed,
                skipped=current.skipped, failed=current.failed, chunks=current.chunks,
            )
            self._advance(run, progress=cancelled, error_code=None)
        except Exception as exc:
            code = exc.code if isinstance(exc, ForgePilotError) else "rag_index_failed"
            current = run.progress
            next_failed = max(current.failed, 1)
            processed = max(current.processed, current.skipped + next_failed, 1)
            total = max(current.total_files, processed)
            failed = IndexProgress(
                repository_id=repository_id, status="failed", total_files=total,
                processed=processed, skipped=current.skipped,
                failed=next_failed, chunks=current.chunks,
            )
            self._advance(run, progress=failed, error_code=code)
        finally:
            self.manager.finish_index(repository_id)

    async def cancel(self, repository_path: str) -> dict:
        _, repository_id = await self._resolve(repository_path)
        run = self._runs.get(repository_id)
        if run is None:
            persisted = self.run_store.get(repository_id)
            if persisted is not None and persisted.status == "cancelled":
                return self.snapshot(repository_id)
            raise ForgePilotError("rag_index_not_running", "This repository is not being indexed")
        if run.record.status == "cancelled":
            return self.snapshot(repository_id)
        if run.record.status in TERMINAL_RUN_STATES or run.task is None or run.task.done():
            raise ForgePilotError("rag_index_not_running", "This repository is not being indexed")
        if run.record.status != "cancelling":
            self._advance(run, status="cancelling")
            run.cancel_event.set()
        await asyncio.shield(run.task)
        return self.snapshot(repository_id)

    async def clear(self, repository_path: str) -> dict:
        repository, repository_id = await self._resolve(repository_path)
        current = self._runs.get(repository_id)
        if current is not None and current.record.status not in TERMINAL_RUN_STATES:
            raise ForgePilotError("rag_index_busy", "Cancel indexing before deleting this index")
        if not self.manager.index_available:
            raise ForgePilotError("rag_unavailable", "Code index storage is unavailable")
        services = self.manager.for_repository(repository)
        if services is None:
            raise ForgePilotError("rag_unavailable", "Code indexing is unavailable")
        if not self.manager.begin_index(repository_id):
            raise ForgePilotError("rag_index_busy", "This repository index is busy")
        try:
            record = self.run_store.begin(repository_id, "clear")
        except BaseException:
            self.manager.finish_index(repository_id)
            raise
        run = _Run(record, asyncio.Event(),
                   IndexProgress(repository_id=repository_id, status="indexing"))
        self._runs[repository_id] = run
        try:
            removed = await services.indexer.clear_repository()
            empty = IndexProgress(repository_id=repository_id, status="empty")
            self._advance(run, progress=empty, error_code=None)
            return {**self.snapshot(repository_id), "deleted_files": removed}
        except asyncio.CancelledError:
            values = self._persisted(repository_id)
            total = max(values["file_count"], 1)
            failed = IndexProgress(repository_id=repository_id, status="failed",
                total_files=total, processed=total, failed=total, chunks=values["chunks"])
            self._advance(run, progress=failed, error_code="rag_index_interrupted")
            raise
        except Exception as exc:
            values = self._persisted(repository_id)
            total = max(values["file_count"], 1)
            failed = IndexProgress(repository_id=repository_id, status="failed",
                total_files=total, processed=total, failed=total, chunks=values["chunks"])
            code = exc.code if isinstance(exc, ForgePilotError) else "rag_index_clear_failed"
            self._advance(run, progress=failed, error_code=code)
            raise ForgePilotError(code, "Code index could not be cleared") from None
        finally:
            self.manager.finish_index(repository_id)

    async def close(self) -> None:
        self._closed = True
        pending = []
        for run in self._runs.values():
            if run.task is not None and not run.task.done():
                run.cancel_event.set()
                pending.append(run.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
