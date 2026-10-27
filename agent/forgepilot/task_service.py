"""Shared task lifecycle, approval, modification and restore for CLI/future adapters."""

import asyncio
from copy import deepcopy
from time import perf_counter

from .context import TaskContext, TaskState
from .changes import ChangeSet
from .path_identity import path_key
from .errors import ForgePilotError
from .file_access import RepositoryLock
from .git import GitRepository
from .repository import Repository
from .runtime import AgentRuntime, transition
from .security import Redactor
from .tools import ToolRegistry, register_repository_tools, register_retrieval_tool
from .tools.change_tools import register_change_tools
from .trace import TraceSink
from .build import BuildPipeline
from .presets import BuildConfig
from .tools.build_tools import register_build_tools
from .code_intelligence import CodeIntelligence, CodeIntelligenceConfig
from .tools.symbol_tools import register_symbol_tools
from .mcp_config import MCPConfig
from .tools.mcp_tools import MCPToolAdapter
from .llm import ModelClient
from .mcp_manager import MCPManager
from .integrity import IntegrityMonitor, IntegrityTaskSpec


class TaskService:
    def __init__(self, llm: ModelClient, *, rg_executable: str, git_executable: str,
                 trace: TraceSink, redactor: Redactor | None = None,
                 build_config: BuildConfig | None = None,
                 code_intelligence_config: CodeIntelligenceConfig | None = None,
                 mcp_config: MCPConfig | None = None, rag_manager=None, task_spec=None) -> None:
        self.llm = llm
        self.rg_executable = rg_executable
        self.git_executable = git_executable
        self.trace = trace
        self.build_config = build_config
        self.code_intelligence_config = code_intelligence_config
        self.mcp_config = mcp_config
        self.rag_manager = rag_manager
        self._mcp = None
        self.redactor = redactor or getattr(llm, "redactor", Redactor())
        self._active: asyncio.Task | None = None
        self._runtime: AgentRuntime | None = None
        self._context: TaskContext | None = None
        self._changes: ChangeSet | None = None
        self._lock: RepositoryLock | None = None
        self.mcp_manager = MCPManager(redactor=self.redactor)
        self.task_spec = task_spec or IntegrityTaskSpec()
        self._integrity = None
        self._extra_test_approval = ()

    def configure_mcp(self, snapshots, *, connection_factory, disabled=False):
        # Called once, synchronously at host admission, before any repository I/O.
        # Preserve explicit legacy TaskService callers; server.py uses only the
        # database import path. The startup disable switch overrides both paths.
        if disabled:
            self.mcp_config = None
        self.mcp_manager = MCPManager(snapshots, redactor=self.redactor, connection_factory=connection_factory,
            on_tool_event=lambda record: self.trace.emit(self._context.task_id, "mcp_tool_activity", record))
        self.redactor = self.mcp_manager.redactor

    def cancel(self) -> bool:
        if self._runtime is not None:
            self._runtime.cancel_requested = True
        if self._active is None or self._active.done():
            if self._context is not None and self._context.state in {TaskState.WAITING_APPROVAL, TaskState.MODIFYING, TaskState.PLANNING}:
                self._runtime.stop(self._context.task_id)
                self._release_terminal()
                return True
            return False
        self._active.cancel()
        return True

    async def analyze(self, repository_path: str, prompt: str) -> TaskContext:
        return await self._run(repository_path, prompt, planning=False)

    async def plan(self, repository_path: str, prompt: str) -> TaskContext:
        return await self._run(repository_path, prompt, planning=True)

    async def start(self, repository_path: str, prompt: str, *, task_id: str | None = None,
                    on_admitted=None, prepare_context=None, background=None,
                    use_rag: bool = True) -> TaskContext:
        """Start a modification task; return at the first human review boundary."""
        return await self._run(repository_path, prompt, planning=True, modify=True,
                               task_id=task_id, on_admitted=on_admitted, prepare_context=prepare_context,
                               background=background, use_rag=use_rag)

    async def _run(self, repository_path: str, prompt: str, *, planning: bool, modify: bool = False,
                   task_id: str | None = None, on_admitted=None, prepare_context=None, background=None,
                   use_rag: bool = True) -> TaskContext:
        if self._active is not None or (self._context is not None and
                                       self._context.state in {TaskState.WAITING_APPROVAL, TaskState.MODIFYING, TaskState.PLANNING}):
            raise ForgePilotError("task_busy", "This service instance already has an active task")
        self._active = asyncio.current_task()
        context = TaskContext(self.redactor.text(repository_path), self.redactor.text(prompt),
                              rag_requested=use_rag,
                              rag_status="unavailable" if use_rag else "not_requested")
        if task_id is not None:
            context.task_id = task_id  # Trusted adapter-generated identity; never an LLM field.
        self._context = context
        self._runtime = None
        self._changes = None
        self._integrity = None
        started = perf_counter()
        try:
            self.trace.emit(context.task_id, "task_created", {
                "repository_path": context.repository_path, "prompt": context.user_request,
            })
            if not prompt.strip() or len(prompt) > 16_384:
                raise ForgePilotError("invalid_prompt", "Prompt must contain 1–16384 characters")
            repository = await Repository.open(repository_path, rg_executable=self.rg_executable,
                                               git_executable=self.git_executable)
            registry = ToolRegistry(redactor=self.redactor)
            register_repository_tools(registry, repository)
            self.mcp_manager.register(registry)
            self._runtime = AgentRuntime(self.llm, registry, self.trace, redactor=self.redactor,
                                         repository=repository, mcp_manager=self.mcp_manager)
            runtime_for_symbols = self._runtime
            intelligence = CodeIntelligence(repository, self.code_intelligence_config,
                selected_directory=lambda: runtime_for_symbols.build_pipeline.binary_dir
                    if runtime_for_symbols.build_pipeline is not None else None)
            register_symbol_tools(registry, intelligence)
            rag_services = None
            if use_rag and self.rag_manager is not None and self.rag_manager.enabled:
                try:
                    rag_services = self.rag_manager.for_repository(
                        repository, code_intelligence=intelligence)
                    if rag_services is not None:
                        context.rag_status = "available"
                        self._runtime.retrieval_service = rag_services.retrieval
                        register_retrieval_tool(
                            registry, rag_services.retrieval,
                            planned_files=lambda: (
                                [file.path for file in self._context.plan.files_to_modify]
                                if self._context is not None and self._context.plan is not None else ()),
                        )
                except Exception:
                    context.rag_status = "degraded"
                    self.trace.emit(context.task_id, "rag_status", {
                        "status": "degraded", "error_code": "rag_initialization_failed",
                    })
            if modify:
                self._lock = RepositoryLock(repository.root)
                self._lock.acquire()
                if on_admitted is not None:
                    on_admitted()
                git = GitRepository(repository, self.git_executable)
                baseline = await git.capture()
                context.git_baseline = {"head": baseline.head, "head_state": baseline.head_state,
                    "status_bytes": len(baseline.status), "diff_bytes": len(baseline.diff),
                    "cached_diff_bytes": len(baseline.cached_diff)}
                self.trace.emit(context.task_id, "git_baseline", context.git_baseline)
                runtime, owned_lock = self._runtime, self._lock
                def ensure_write(path):
                    if owned_lock.handle is None:
                        raise ForgePilotError("repository_lock", "Modification task does not own the repository")
                    runtime.ensure_write(path)
                self._changes = ChangeSet(repository, ensure_write, self.trace, context.task_id)
                self._runtime.changes = self._changes
                monitor = IntegrityMonitor(repository, self.task_spec)
                self._integrity = monitor
                changes = self._changes
                changes.content_guard = monitor.guard
                def capture_approval(version):
                    extra = self._extra_test_approval
                    planned = {path_key(file.path) for file in runtime._review.plan.files_to_modify}
                    if any(path_key(repository.normalize_plan_path(path)) not in planned for path in extra):
                        raise ForgePilotError("validation_integrity", "Extra test approval must name files in this plan")
                    pipeline = runtime.build_pipeline
                    names = pipeline.report().get("initial_ctest_names") if pipeline is not None else None
                    metadata = monitor.capture(version, approved_test_files=extra, ctest_names=names)
                    changes.capture_scope()
                    context.test_integrity = metadata
                    self.trace.emit(context.task_id, "test_integrity_approved", metadata)
                def verify_integrity():
                    pipeline = runtime.build_pipeline
                    names = pipeline.report().get("test_names") if pipeline is not None else None
                    context.test_integrity = monitor.verify(ctest_names=names)
                    changes.verify_scope()
                    self.trace.emit(context.task_id, "test_integrity_verified", context.test_integrity)
                runtime.before_approve = capture_approval
                runtime.verify_integrity = verify_integrity
                def mark_rag_dirty(path):
                    if rag_services is None or not rag_services.indexer.available:
                        return
                    try:
                        state = rag_services.indexer.mark_dirty(path)
                        self.trace.emit(context.task_id, "rag_file_dirty", {
                            "path": state.path, "status": state.status,
                        })
                    except Exception:
                        self.trace.emit(context.task_id, "rag_status", {
                            "status": "degraded", "error_code": "rag_dirty_failed",
                        })
                register_change_tools(
                    registry, git, self._changes, on_changed=mark_rag_dirty)
                if self.build_config is not None:
                    def ensure_build(phase):
                        if owned_lock.handle is None:
                            raise ForgePilotError("repository_lock", "Build task does not own the repository")
                        runtime.ensure_build(phase)
                    pipeline = BuildPipeline(repository.root, self.build_config, ensure_build,
                                             self.trace, context.task_id, self.redactor)
                    runtime.build_pipeline = pipeline
                    await pipeline.prepare()
                    context.build_result = pipeline.report()
                    register_build_tools(registry, pipeline)
            if self.mcp_config is not None:
                self._mcp = MCPToolAdapter(self.mcp_config)
                try:
                    await self._mcp.register(registry)
                    self.mcp_manager.include_legacy_tool(self._mcp.name)
                    status = {"status": "ready", "name": self._mcp.name}
                except ForgePilotError as exc:
                    status = {"status": "unavailable", "error": self.redactor.text(str(exc))}
                self.trace.emit(context.task_id, "mcp_status", status)
            if rag_services is not None:
                begin_index = getattr(self.rag_manager, "begin_index", None)
                finish_index = getattr(self.rag_manager, "finish_index", None)
                claimed = (begin_index(rag_services.indexer.repository_id)
                           if begin_index is not None else True)
                try:
                    indexed = (await rag_services.indexer.index_repository() if claimed
                               else rag_services.indexer.get_status())
                    # Literal/clangd retrieval remains usable without a vector
                    # index, including embedding/Qdrant/index failures.
                    self._runtime.rag_index_available = getattr(
                        rag_services.retrieval, "available", True)
                    self.trace.emit(context.task_id, "rag_status", {
                        "status": indexed.status if claimed else "busy",
                        "total_files": indexed.total_files,
                        "processed": indexed.processed,
                        "skipped": indexed.skipped,
                        "failed": indexed.failed,
                        "chunks": indexed.chunks,
                    })
                    if indexed.status in {"degraded", "failed"}:
                        context.rag_status = "degraded"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    context.rag_status = "degraded"
                    self._runtime.rag_index_available = getattr(
                        rag_services.retrieval, "available", True)
                    self.trace.emit(context.task_id, "rag_status", {
                        "status": "degraded", "error_code": "rag_index_failed",
                    })
                finally:
                    if claimed and finish_index is not None:
                        finish_index(rag_services.indexer.repository_id)
            result = await self._runtime.run(context, planning=planning, prepare_context=prepare_context,background=background)
            # UI/adapters receive snapshots, never the mutable authorization state.
            return deepcopy(result)
        except asyncio.CancelledError:
            context.error = "cancelled: Task cancelled during repository validation"
            transition(context, TaskState.CANCELLED, self.trace)
        except ForgePilotError as exc:
            context.error = self.redactor.text(str(exc))
            transition(context, TaskState.FAILED, self.trace)
        except Exception:
            context.error = "task_internal: Unable to initialize analysis"
            transition(context, TaskState.FAILED, self.trace)
        finally:
            try:
                try:
                    await self._close_mcp()
                finally:
                    if self._runtime is not None and self._runtime.build_pipeline is not None:
                        context.build_result = self._runtime.build_pipeline.report()
            except BaseException:
                self._active = None
                self.fail_without_trace(ForgePilotError("task_cleanup", "Task resource cleanup failed"))
                raise
            finally:
                self._active = None
                self._release_terminal()
        context.duration_ms = int((perf_counter() - started) * 1_000)
        self.trace.emit(context.task_id, "task_finished", {
            "state": context.state.value, "error": context.error, "duration_ms": context.duration_ms,
        })
        return deepcopy(context)

    def _review_runtime(self, task_id: str) -> AgentRuntime:
        if self._context is None or self._context.task_id != task_id:
            raise ForgePilotError("unknown_task", "Task does not belong to this service")
        if self._runtime is None:
            raise ForgePilotError("invalid_state", "Task has no reviewable plan")
        return self._runtime

    @property
    def state(self) -> TaskState | None:
        """Read immutable lifecycle state without copying the task's accumulated data."""
        return self._context.state if self._context is not None else None

    def snapshot(self, *, persist_diff=False) -> TaskContext | None:
        """Read on the service's owning loop; callers receive data, never live authority."""
        result = deepcopy(self._context)
        if result is not None and self._runtime is not None and self._runtime.build_pipeline is not None:
            result.build_result = self._runtime.build_pipeline.report()
        if result is not None and persist_diff and self._changes is not None:
            result.task_diff = self.redactor.data(self._changes.diff(limit=None,
                inspect_current=result.state in {TaskState.COMPLETED,TaskState.FAILED,TaskState.CANCELLED}))
        return result

    def approve(self, task_id: str, plan_version: int, *, approved_test_files=()) -> TaskContext:
        """Additional exact test-file approval is a trusted human action, never plan data."""
        if type(approved_test_files) not in {tuple, list, set, frozenset}:
            raise ForgePilotError("invalid_arguments", "Extra test approval requires an exact file list")
        self._extra_test_approval = tuple(approved_test_files)
        try:
            return deepcopy(self._review_runtime(task_id).approve(task_id, plan_version))
        finally:
            self._extra_test_approval = ()

    def reject(self, task_id: str, plan_version: int) -> TaskContext:
        result = self._review_runtime(task_id).reject(task_id, plan_version)
        self._release_terminal()
        return deepcopy(result)

    def cancel_review(self, task_id: str) -> TaskContext:
        result = self._review_runtime(task_id).cancel_review(task_id)
        self._release_terminal()
        return deepcopy(result)

    def replace_plan(self, task_id: str, expected_version: int, proposal: str | dict) -> TaskContext:
        if self._active is not None:
            raise ForgePilotError("task_busy", "Cannot replace a plan during active execution")
        return deepcopy(self._review_runtime(task_id).replace_plan(task_id, expected_version, proposal))

    def begin_feedback(self, task_id: str, expected_version: int, feedback: str) -> TaskContext:
        if self._active is not None:
            raise ForgePilotError("task_busy", "Cannot accept feedback during execution or resource cleanup")
        # Runtime validation also protects direct Service callers, without an HTTP adapter.
        return deepcopy(self._review_runtime(task_id).begin_feedback(task_id, expected_version, feedback))

    async def replan(self, task_id: str) -> TaskContext:
        runtime = self._review_runtime(task_id)
        if self._active is not None:
            raise ForgePilotError("task_busy", "Task already executing")
        self._active = asyncio.current_task()
        try:
            await runtime.replan(task_id)
        finally:
            try:
                await self._close_mcp()
            except asyncio.CancelledError:
                # MCP close joins owned cleanup before propagating cancellation.
                # A plan produced just before close is still not an approved review.
                runtime._grant = runtime._review = None
                if self.state in {TaskState.PLANNING, TaskState.WAITING_APPROVAL}:
                    runtime.stop(task_id)
            except BaseException:
                self._active = None
                self.fail_without_trace(ForgePilotError("task_cleanup", "Task resource cleanup failed"))
                raise
            finally:
                self._active = None
                self._release_terminal()
        return deepcopy(self._context)

    def is_file_approved(self, task_id: str, plan_version: int, path: str) -> bool:
        return self._review_runtime(task_id).is_file_approved(task_id, plan_version, path)

    def _release_terminal(self):
        if self._context is not None and self._context.state in {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}:
            if self._lock is not None:
                self._lock.release()

    def fail_without_trace(self, error):
        """In-memory fallback only, after execution has exited or cancellation joined."""
        if self._active is not None and not self._active.done():
            raise ForgePilotError("task_busy", "Execution must settle before failure cleanup")
        if self._context is not None:
            self._context.state = TaskState.FAILED
            self._context.error = self.redactor.text(str(error))
            self._context.failure_kind = error.code
            self._context.outcome = error.code
            self._context.final_result = None
            self._context.approved_files.clear()
            self._context.approved_plan_version = None
        if self._runtime is not None:
            self._runtime._grant = None
            self._runtime._review = None
            self._runtime.cancel_requested = True
        self._release_terminal()

    async def execute(self, task_id: str) -> TaskContext:
        runtime = self._review_runtime(task_id)
        if self._active is not None:
            raise ForgePilotError("task_busy", "Task already executing")
        self._active = asyncio.current_task()
        try:
            return deepcopy(await runtime.execute(task_id))
        finally:
            try:
                await self._close_mcp()
            except BaseException:
                self._active = None
                self.fail_without_trace(ForgePilotError("task_cleanup", "Task resource cleanup failed"))
                raise
            finally:
                self._active = None
                self._release_terminal()

    async def _close_mcp(self):
        try:
            await self.mcp_manager.close()
        finally:
            if self._mcp is not None:
                await self._mcp.close()

    def task_diff(self, task_id: str) -> dict:
        self._review_runtime(task_id)
        if self._changes is None:
            raise ForgePilotError("invalid_state", "This is not a modification task")
        self._context.task_diff = self.redactor.data(self._changes.diff())
        return deepcopy(self._context.task_diff)

    async def restore(self, task_id: str) -> dict:
        self._review_runtime(task_id)
        if (self._active is not None or self._changes is None or
                self._context.state not in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}):
            raise ForgePilotError("invalid_state", "Explicit restore requires a stopped modification task")
        lock = RepositoryLock(self._changes.repository.root)
        lock.acquire()
        self._active = asyncio.current_task()
        try:
            result = await self._changes.restore()
            self._context.restore_result = self.redactor.data(result)
            return deepcopy(self._context.restore_result)
        finally:
            self._context.task_diff = self.redactor.data(self._changes.diff())
            self._active = None
            lock.release()

    def close(self):
        """Adapters must close abandoned reviews; no implicit rollback."""
        self.cancel()
        if self._active is None:
            self._release_terminal()
