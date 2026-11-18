"""Session task ownership, isolated from the HTTP loop; business stays in TaskService."""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from .host_runtime import HostRuntime
from .task_capacity import TaskCapacity
from .context import TaskContext, TaskState
from .path_identity import path_key
from .errors import ForgePilotError
from .sqlite_store import SqliteEventStore
from .recording import now, project_events, snapshot_context
from .trace import TraceEvent
from .mcp_service import MCPService

TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}


class _Trace:
    def __init__(self, callback):
        self.callback, self.sequence = callback, 0

    def emit(self, task_id, kind, data):
        self.sequence += 1
        self.callback(TraceEvent(self.sequence, task_id, kind, now(), data))


@dataclass
class _Task:
    initial_context: TaskContext
    service: object
    repository_key: str
    created_at: str
    updated_at: str
    control: asyncio.Lock
    admission: asyncio.Future
    running: asyncio.Task | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    closed: bool = False
    terminal_traces: list[TraceEvent] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    persistence_error: ForgePilotError | None = None


class TaskHost:
    def __init__(self, service_factory, *, store=None, max_tasks=32, mcp_disabled=False,
                 legacy_mcp_config=None, rag_coordinator=None, runtime=None, capacity=None):
        self.factory = service_factory
        self.store = store if store is not None else SqliteEventStore()
        self.runtime = runtime if runtime is not None else HostRuntime()
        self.capacity = capacity if capacity is not None else TaskCapacity(max_tasks, mission_count=self.store.missions.active_count)
        self._tasks: dict[str, _Task] = {}
        self._repositories: dict[str, str] = {}
        self.mcp_service = MCPService(self.store)  # No connection/decryption at startup.
        self.mcp_disabled = mcp_disabled
        self.legacy_mcp_config = legacy_mcp_config
        self.rag_coordinator = rag_coordinator
        self._mcp_active = set()  # Worker-loop owned; includes resource cleanup.

    @property
    def max_tasks(self):
        return self.capacity.max_tasks

    # Compatibility for embedded diagnostics; other hosts use the public runtime.
    @property
    def _loop(self):
        return self.runtime._loop

    @property
    def _thread(self):
        return self.runtime._thread

    @property
    def _stopping(self):
        return self.runtime.stopping

    @_stopping.setter
    def _stopping(self, value):
        if value:
            self.runtime.stop_accepting()

    def has_active_tasks(self):
        """Worker-loop view, including tasks still closing resources."""
        return bool(self._tasks)

    def has_active_task(self, task_id):
        return task_id in self._tasks

    def mark_cancel_for_deletion(self, task_id):
        """Worker-loop only: reject racing approvals before deletion yields."""
        entry = self._tasks.get(task_id)
        if entry is not None:
            entry.cancel_requested = True

    async def create_for_conversation(self, repository_path, prompt, **options):
        """Worker-loop admission linked to an already persisted conversation request."""
        return await self._create(repository_path, prompt, **options)

    async def start(self):
        await self.runtime.start()
        if self.legacy_mcp_config and not self.mcp_disabled:
            await self._submit(self._schedule_legacy_import())

    async def _schedule_legacy_import(self):
        async def import_owned():
            try:
                await self.mcp_service.import_legacy(self.legacy_mcp_config)
            except Exception as exc:
                # No config contents, paths, environment values or transport text.
                code=exc.code if isinstance(exc,ForgePilotError) else 'mcp_internal'
                logging.getLogger(__name__).warning('Legacy MCP import failed (%s); normal chat remains available',code)
            finally:
                self._mcp_active.discard(asyncio.current_task())
        operation=asyncio.create_task(import_owned())
        self._mcp_active.add(operation)  # Same admission accounting and shutdown join as management.

    async def _submit(self, operation):
        """Compatibility shim; shared runtime users call runtime.submit directly."""
        return await self.runtime.submit(operation)

    async def run_mcp(self, operation, *args, **kwargs):
        """Management remains request-scoped; cancellation joins worker cleanup."""
        async def execute():
            task = asyncio.current_task()
            if self._stopping:
                raise ForgePilotError("server_stopping", "Server is shutting down")
            if operation not in ("probe", "save", "set_enabled", "test", "delete"):
                raise ForgePilotError("mcp_config", "Unknown MCP management operation")
            network = operation in ("probe", "save", "test") or (operation == "set_enabled" and kwargs["enabled"])
            if network and self.mcp_disabled:
                raise ForgePilotError("mcp_disabled", "MCP is disabled by the backend startup option")
            if network and len(self._mcp_active) >= 2:
                raise ForgePilotError("mcp_capacity", "Two MCP connection checks are running; retry after one finishes")
            if network:
                self._mcp_active.add(task)
            try:
                method = getattr(self.mcp_service, operation)
                if operation == "delete":
                    return method(*args, **kwargs)
                return await method(*args, **kwargs)
            except asyncio.CancelledError:
                if self._stopping:
                    raise ForgePilotError("server_stopping", "Server is shutting down") from None
                raise
            finally:
                self._mcp_active.discard(task)

        return await self.runtime.run_request(execute())

    async def run_rag(self, operation, *args):
        if self.rag_coordinator is None:
            raise ForgePilotError("rag_unavailable", "Code indexing is not configured")
        if operation not in {"start", "status", "cancel", "clear"}:
            raise ForgePilotError("rag_unavailable", "Unknown code indexing operation")
        return await self._submit(getattr(self.rag_coordinator, operation)(*args))

    async def rag_snapshot(self, repository_id):
        if self.rag_coordinator is None:
            raise ForgePilotError("rag_unavailable", "Code indexing is not configured")

        async def read():
            return self.rag_coordinator.snapshot(repository_id)
        return await self._submit(read())

    def _snapshot(self, entry):
        context = entry.service.snapshot(persist_diff=True) or entry.initial_context
        return entry.service.redactor.data(snapshot_context(context, created_at=entry.created_at,
            updated_at=entry.updated_at, finished_at=entry.finished_at, cancel_requested=entry.cancel_requested))

    def _publish(self, entry, trace=None, *, closed=False, settled=False):
        if entry.persistence_error is not None:
            return  # Cleanup traces must not resume a failed persistence stream.
        state = entry.service.state or entry.initial_context.state
        # Terminal and review transitions can precede async resource cleanup.
        # Publish reviews only when the active segment has settled, so a visible
        # Approve action cannot race MCP shutdown and fail with task_busy.
        if (state in TERMINAL and not closed or
                state == TaskState.WAITING_APPROVAL and not settled):
            if trace is not None:
                entry.terminal_traces.append(trace)
            return
        entry.updated_at = now()
        snapshot = self._snapshot(entry)
        traces = entry.terminal_traces + ([trace] if trace is not None else [])
        traces = [replace(item, data=entry.service.redactor.data(item.data)) for item in traces]
        try:
            self.store.publish(entry.initial_context.task_id, snapshot, project_events(traces,snapshot), closed=closed, traces=traces)
        except (sqlite3.Error, ForgePilotError):
            raise self._persistence_failed(entry) from None
        entry.terminal_traces.clear()

    def _persistence_failed(self, entry):
        if entry.persistence_error is None:
            error = ForgePilotError("storage_unavailable", "Task persistence failed; durable status is unavailable")
            error.task_id = entry.initial_context.task_id
            entry.persistence_error = error
            self.store.mark_task_unavailable(error.task_id)
            if entry.service._runtime is not None:
                # A tool adapter may turn a trace exception into a ToolResult.
                # Stop the next tool/model step even when that exception is consumed.
                entry.service._runtime.cancel_requested = True
        return entry.persistence_error

    async def create(self, repository_path, prompt, *, use_rag=True):
        return await self._submit(self._create(repository_path, prompt, use_rag=use_rag))

    async def _create(self, repository_path, prompt, *, request_id=None, prepare_context=None, background=None,
                      use_rag=True):
        if self._stopping:
            raise ForgePilotError("server_stopping", "Server is shutting down")
        self.capacity.check_available("活动任务数量已达上限，请等待当前任务完成。")
        key = path_key(str(Path(repository_path).resolve()))
        if key in self._repositories:
            error = ForgePilotError("repository_busy", "Another task owns this repository")
            error.task_id = self._repositories[key]
            raise error
        context = TaskContext(repository_path, prompt, rag_requested=use_rag,
                              rag_status="unavailable" if use_rag else "not_requested")
        trace = _Trace(lambda event: self._publish(entry, event))
        service = self.factory(trace)
        if hasattr(service, "configure_mcp"):
            service.configure_mcp(() if self.mcp_disabled else self.mcp_service.snapshots("task"),
                                  connection_factory=self.mcp_service.connection_factory, disabled=self.mcp_disabled)
        stamp = now()
        entry = _Task(context, service, key, stamp, stamp, asyncio.Lock(), asyncio.get_running_loop().create_future())
        self.capacity.acquire("v1", context.task_id, message="活动任务数量已达上限，请等待当前任务完成。")
        self._tasks[context.task_id] = entry
        self._repositories[key] = context.task_id
        try:
            self.store.create(context.task_id, self._snapshot(entry), request_id=request_id)
        except BaseException as exc:
            entry.admission.cancel()
            try:
                try:
                    service.close()
                finally:
                    close=getattr(service.llm,'aclose',None)
                    if close: await close()
            except Exception:
                pass  # Preserve the creation error after attempting both resource cleanups.
            finally:
                self._tasks.pop(context.task_id,None); self._repositories.pop(key,None)
                self.capacity.release("v1", context.task_id)
            if isinstance(exc, sqlite3.Error):
                raise ForgePilotError("storage_unavailable", "Cannot persist task creation") from None
            raise
        entry.running = asyncio.create_task(self._drive(entry, initial=True, prepare_context=prepare_context,background=background))
        await asyncio.shield(entry.admission)
        return self.store.get(context.task_id)

    async def _drive(self, entry, *, initial=False, prepare_context=None, background=None):
        entry.started.set()
        def admitted():
            self._publish(entry)
            if not entry.admission.done():
                entry.admission.set_result(None)
        error = None
        try:
            try:
                if initial:
                    await entry.service.start(entry.initial_context.repository_path, entry.initial_context.user_request,
                                              task_id=entry.initial_context.task_id, on_admitted=admitted,
                                              prepare_context=prepare_context,background=background,
                                              use_rag=entry.initial_context.rag_requested)
                elif entry.service.state == TaskState.PLANNING:
                    await entry.service.replan(entry.initial_context.task_id)
                elif entry.service.state == TaskState.MODIFYING:
                    await entry.service.execute(entry.initial_context.task_id)
                self._publish(entry, settled=True)
            except asyncio.CancelledError:
                error = ForgePilotError("cancelled", "Task execution cancelled")
            except Exception as exc:
                error = exc if isinstance(exc, ForgePilotError) else ForgePilotError("task_internal", "Task execution failed")
        finally:
            try:
                if not entry.admission.done():
                    # No snapshot/report/storage or resource close can block admission settlement.
                    context = entry.service._context or entry.initial_context
                    error = entry.persistence_error or error or ForgePilotError(
                        (context.error or "task_internal:").split(":", 1)[0], context.error or "Task could not be admitted")
                    error.task_id = context.task_id
                    entry.admission.set_exception(error)
            finally:
                if error is not None or entry.persistence_error is not None:
                    await self._fail(entry, error)
                elif entry.service.state in TERMINAL:
                    await self._finalize(entry)

    async def _fail(self, entry, error):
        """Join execution before revoking authority; never acquire the control lock here."""
        entry.cancel_requested = True
        running = entry.running
        if running is not None and running is not asyncio.current_task() and not running.done():
            if not entry.closed and not running.cancelling():
                running.cancel()
            while not running.done():
                try:
                    await asyncio.shield(running)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        entry.service.fail_without_trace(entry.persistence_error or error or
            ForgePilotError("task_internal", "Task execution failed"))
        await self._finalize(entry)

    async def _finalize(self, entry):
        if entry.closed:
            return
        entry.closed = True
        try:
            try:
                try:
                    entry.service.close()
                finally:
                    close = getattr(entry.service.llm, "aclose", None)
                    if close is not None:
                        await close()
            except (asyncio.CancelledError, Exception):
                entry.service.fail_without_trace(entry.persistence_error or
                    ForgePilotError("task_cleanup", "Task resource cleanup failed"))
            entry.finished_at = now()
            try:
                self._publish(entry, closed=True)
            except Exception:
                entry.service.fail_without_trace(self._persistence_failed(entry))
        finally:
            if self._repositories.get(entry.repository_key) == entry.initial_context.task_id:
                del self._repositories[entry.repository_key]
            self._tasks.pop(entry.initial_context.task_id,None)
            self.capacity.release("v1", entry.initial_context.task_id)

    def _entry(self, task_id):
        self.store._ensure_task_available(task_id)
        if task_id not in self._tasks:
            self.store.get(task_id)  # Distinguish unknown IDs from persisted, read-only history.
            raise ForgePilotError("invalid_state", "Historical tasks are read-only; execution cannot be resumed")
        return self._tasks[task_id]

    async def approve(self, task_id, plan_version, *, approved_test_files=()):
        return await self._submit(self._approve(task_id, plan_version, approved_test_files=approved_test_files))

    async def _approve(self, task_id, plan_version, *, approved_test_files=()):
        entry = self._entry(task_id)
        if entry.cancel_requested:
            raise ForgePilotError("invalid_state", "任务正在停止或删除，不能批准方案。")
        async with entry.control:
            if self._stopping or entry.cancel_requested:
                raise ForgePilotError("invalid_state", "Task is stopping or cancellation was requested")
            if entry.running is not None and not entry.running.done():
                raise ForgePilotError("task_busy", "Task is still executing")
            try:
                self.store._ensure_task_available(task_id)
                if approved_test_files:
                    entry.service.approve(task_id, plan_version, approved_test_files=approved_test_files)
                else:
                    entry.service.approve(task_id, plan_version)
                self._publish(entry)
                entry.running = asyncio.create_task(self._drive(entry))
                return self.store.get(task_id)
            except Exception:
                if entry.persistence_error is not None:
                    await self._fail(entry, entry.persistence_error)
                    raise entry.persistence_error from None
                raise

    async def feedback(self, task_id, plan_version, feedback):
        return await self._submit(self._feedback(task_id, plan_version, feedback))

    async def _feedback(self, task_id, plan_version, feedback):
        entry = self._entry(task_id)
        async with entry.control:
            if self._stopping:
                raise ForgePilotError("server_stopping", "Server is shutting down")
            if entry.cancel_requested:
                raise ForgePilotError("invalid_state", "Task is stopping or being deleted")
            if entry.running is not None and not entry.running.done():
                raise ForgePilotError("task_busy", "Task is still executing or cleaning up")
            try:
                self.store._ensure_task_available(task_id)
                entry.service.begin_feedback(task_id, plan_version, feedback)
                self._publish(entry)
                snapshot = self.store.get(task_id)
                entry.running = asyncio.create_task(self._drive(entry))
                return snapshot
            except Exception as exc:
                if isinstance(exc, sqlite3.Error):
                    self._persistence_failed(entry)
                if entry.persistence_error is not None:
                    await self._fail(entry, entry.persistence_error)
                    raise entry.persistence_error from None
                raise

    async def cancel(self, task_id, *, reason="user", plan_version=None):
        return await self._submit(self._cancel(task_id, reason, plan_version))

    async def _cancel(self, task_id, reason, plan_version):
        if task_id not in self._tasks:
            saved=self.store.get(task_id)
            if reason=='user' and saved['state']=='cancelled' and not saved['read_only']:
                return saved
        entry = self._entry(task_id)
        async with entry.control:
            if entry.service.state is None and entry.running is not None:
                await entry.started.wait()
            state = (entry.service.state or entry.initial_context.state)
            if state == TaskState.CANCELLED and reason == "user":
                return self.store.get(task_id)  # Ordinary cancel is idempotent.
            if state in TERMINAL:
                raise ForgePilotError("invalid_state", "Terminal tasks cannot be cancelled or rejected")
            try:
                self.store._ensure_task_available(task_id)
                if reason == "reject":
                    entry.service.reject(task_id, plan_version)
                else:
                    entry.service.cancel()
                entry.cancel_requested = True
                self._publish(entry)
                if entry.running is not None and not entry.running.done():
                    await asyncio.shield(entry.running)
                else:
                    await self._finalize(entry)
                return self.store.get(task_id)
            except Exception:
                if entry.persistence_error is not None:
                    await self._fail(entry, entry.persistence_error)
                    raise entry.persistence_error from None
                raise

    async def close(self, close_resources=None):
        await self.runtime.close(self._drain_tasks,
            close_resources=lambda: self._close_resources(close_resources))

    async def stop_for_deletion(self, task_id):
        """Worker-loop only. Also join terminal tasks still closing their LLM/processes."""
        entry=self._tasks.get(task_id)
        if entry is None: return
        entry.cancel_requested=True  # Reject racing approvals before the first await.
        async with entry.control:
            if entry.service.state is None and entry.running is not None:
                await entry.started.wait()
            state=(entry.service.state or entry.initial_context.state)
            try:
                if state not in TERMINAL:
                    entry.service.cancel()
                    self._publish(entry)
                if entry.running is not None and not entry.running.done():
                    await asyncio.shield(entry.running)
                elif not entry.closed:
                    await self._finalize(entry)
                if entry.persistence_error is not None:
                    raise entry.persistence_error
            except Exception:
                if entry.persistence_error is not None:
                    await self._fail(entry, entry.persistence_error)
                    raise entry.persistence_error from None
                raise

    async def _shutdown(self, *, close_resources=None):
        """Compatibility entry for a worker-side legacy-only shutdown."""
        await self._drain_tasks()
        await self._close_resources(close_resources)

    async def _drain_tasks(self):
        self._stopping = True
        if self.rag_coordinator is not None:
            await self.rag_coordinator.close()
        checks = list(self._mcp_active)
        for check in checks:
            if not check.cancelling():
                check.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
        await asyncio.gather(*(self._cancel(task_id, "user", None) for task_id, entry in list(self._tasks.items())
                               if not entry.closed), return_exceptions=True)
        await asyncio.gather(*(entry.running for entry in self._tasks.values() if entry.running is not None),
                             return_exceptions=True)

    async def _close_resources(self, close_resources=None):
        if self.rag_coordinator is not None:
            # The coordinator first stops index runs and rejects new work. Shared
            # embedding/vector resources close only after Agent tasks have joined.
            await self.rag_coordinator.manager.aclose()
        if close_resources is not None:
            await close_resources()
