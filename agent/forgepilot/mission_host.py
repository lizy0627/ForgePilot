"""Mission application boundary sharing the existing host, Graph and services.

HTTP never supplies model configuration, tools, graph definitions or capability
grants. Work runs on the shared runtime loop and survives request disconnects.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from inspect import signature
import json
import logging
from pathlib import Path
import re
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from .agents.analysis import AnalysisRequest
from .artifacts.models import DataPolicy, RepositorySpec
from .errors import ForgePilotError
from .mission_events import safe_event_data
from .orchestration.graph import validate_graph
from .orchestration.models import GraphDefinition, NodeResult, NodeStatus, TERMINAL_NODE_STATUSES
from .orchestration.recovery import recovery_role
from .orchestration.store import GraphStore

if TYPE_CHECKING:
    from .mission_factory import MissionDependencies
    from .presets import BuildConfig, MissionBuildSelection
    from .workspace_manager import WorkspaceManager


def _revision_conflict():
    return ForgePilotError("mission_revision_conflict",
        "Mission revision changed; refresh the mission before retrying")


def _approval_conflict():
    return ForgePilotError("mission_approval_conflict",
        "TaskSpec, Plan or Scope changed; review the current approval package")


@dataclass(frozen=True)
class _SourcePreview:
    task_id: str
    workspace: WorkspaceManager
    fields: dict[str, object]
    source_head: str
    base_commit: str
    file_hashes: dict[str, str | None]
    patch_sha256: str | None
    selection: MissionBuildSelection
    build_config: BuildConfig
    expires_at: float


class MissionHost:
    def __init__(self, task_host=None, workflow_factory=None, *, runtime=None, capacity=None,
                 store=None, mcp_service=None, mcp_disabled=None, dependencies: MissionDependencies | None = None,
                 development_config=None, locked_approval_authority=None):
        # Positional TaskHost remains an in-process compatibility adapter only.
        # Production composition injects these shared components explicitly.
        self.runtime = runtime if runtime is not None else task_host.runtime
        self.capacity = capacity if capacity is not None else task_host.capacity
        self.store = store if store is not None else task_host.store
        self.mcp_service = mcp_service if mcp_service is not None else task_host.mcp_service
        self.mcp_disabled = task_host.mcp_disabled if mcp_disabled is None else mcp_disabled
        self.factory = workflow_factory
        if dependencies is not None and dependencies.events is not self.store:
            raise ValueError("Mission dependencies must use the host event store")
        self.dependencies = dependencies
        self._development_config = development_config or (lambda: dependencies)
        self.locked_approval_authority = locked_approval_authority
        self.graphs = GraphStore(self.store)
        self._services = {}
        self._previews: dict[str, _SourcePreview] = {}
        self._source_lock = asyncio.Lock()
        self._running = {}
        self._admissions = set()
        self._operations = set()
        self._controls = WeakValueDictionary()
        self._stopping = False

    def _selected_mcp_snapshots(self, server_ids, expected_revisions=None):
        """Resolve exact user-selected task servers before allocating a Worktree."""
        if not isinstance(server_ids, (list, tuple)) or len(server_ids) > 4:
            raise ForgePilotError("mcp_selection", "Select at most four MCP servers")
        try:
            selected = tuple(str(UUID(value)) for value in server_ids)
        except (ValueError, TypeError, AttributeError):
            raise ForgePilotError("mcp_selection", "MCP selection requires server UUIDs") from None
        if len(selected) != len(set(selected)):
            raise ForgePilotError("mcp_selection", "Select distinct MCP servers")
        if selected and self.mcp_disabled:
            raise ForgePilotError("policy_denied", "Work-mode MCP is disabled by application policy")
        available = {item.server_id: item for item in self.mcp_service.snapshots("task")} if selected else {}
        if any(server_id not in available for server_id in selected):
            raise ForgePilotError("mcp_selection", "Selected MCP server is not enabled for tasks")
        snapshots = tuple(available[server_id] for server_id in selected)
        if any(item.secret_error or not item.config.selected_tools or
               any(name not in {entry.get("name") for entry in item.catalog
                                if entry.get("compatible") is True and
                                entry.get("annotations", {}).get("readOnlyHint") is True}
                   for name in item.config.selected_tools)
               for item in snapshots):
            raise ForgePilotError("mcp_selection", "Selected MCP server is unavailable or not read-only")
        revisions = tuple(item.revision for item in snapshots)
        if expected_revisions is not None and tuple(expected_revisions) != revisions:
            raise ForgePilotError("mcp_catalog_changed", "Mission MCP selection changed; start a new mission")
        return snapshots

    def _workflow(self, task_id, *, mcp_server_ids=None, single_agent=None):
        restoring = mcp_server_ids is None
        if task_id not in self._services:
            metadata = None
            if mcp_server_ids is None:
                metadata = self._metadata(task_id)
                if (metadata.get("source_baseline") is None and self.graphs.snapshot(task_id).graph_id in {
                        "task-analysis-delivery.v2", "task-analysis-delivery.v3", "task-analysis-delivery.v4",
                        "validated-delivery.v2", "validated-delivery.v3", "validated-delivery.v4"}):
                    raise ForgePilotError("mission_frozen_config_missing",
                        "This historical task has no frozen source/build selection; it remains readable, but must be recreated before execution or recovery")
                if self.graphs.snapshot(task_id).graph_version >= 4 and metadata.get("tool_selection") is None:
                    raise ForgePilotError("mission_frozen_config_missing", "Frozen Mission tool selection is missing")
                mcp_server_ids = metadata.get("mcp_server_ids", ())
                expected_revisions = metadata.get("mcp_server_revisions")
                if mcp_server_ids and expected_revisions is None:
                    raise ForgePilotError("mcp_catalog_changed", "Mission MCP selection cannot be restored")
            else:
                expected_revisions = None
            if single_agent is None:
                single_agent = (metadata or {}).get("orchestration_mode") == "single_agent"
            snapshots = self._selected_mcp_snapshots(mcp_server_ids, expected_revisions)
            if self.factory is None:
                from .mission_factory import build_mission_workflow
                if self.dependencies is None:
                    raise ForgePilotError("mission_unavailable", "Missions require the configured shared ModelGateway")
                workflow = build_mission_workflow(self.dependencies, mcp_snapshots=snapshots,
                                                  single_agent=single_agent)
                workflow.mission_mcp_revisions = tuple(item.revision for item in snapshots)
            else:
                kwargs = {}
                if mcp_server_ids:
                    kwargs["mcp_snapshots"] = snapshots
                if single_agent:
                    kwargs["single_agent"] = True
                if kwargs:
                    try:
                        signature(self.factory).bind(self.store, **kwargs)
                    except (TypeError, ValueError):
                        if mcp_server_ids:
                            raise ForgePilotError("mcp_selection", "Mission factory does not support selected MCP servers") from None
                        raise ForgePilotError("mission_unavailable", "Mission factory does not support the requested topology") from None
                    workflow = self.factory(self.store, **kwargs)
                    if mcp_server_ids:
                        workflow.mission_mcp_revisions = tuple(item.revision for item in kwargs["mcp_snapshots"])
                else:
                    workflow = self.factory(self.store)
            workflow.mission_mcp_snapshots = snapshots
            if bool(getattr(workflow, "single_agent", False)) != bool(single_agent):
                raise ForgePilotError("mission_unavailable", "Mission workflow topology differs from the locked request")
            if restoring and metadata.get("source_baseline") is not None:
                from .presets import MissionBuildSelection
                config = self._build_config(metadata)
                if not isinstance(metadata.get("build_selection"), dict):
                    raise ForgePilotError("build_selection_changed", "Frozen build selection is missing; recreate this task")
                selection = MissionBuildSelection.from_dict(metadata["build_selection"])
                current, frozen = asdict(config), metadata.get("frozen_build_config")
                # Earlier hosts froze an implicit Debug as an explicit value.
                # Presets can resolve an unspecified configuration differently.
                if (selection.configuration == "Debug" and current["configuration"] in (None, "Debug")
                        and isinstance(frozen, dict) and frozen.get("configuration") in (None, "Debug")):
                    current["configuration"] = frozen.get("configuration")
                if current != frozen:
                    raise ForgePilotError("build_selection_changed", "Trusted build configuration changed; recreate this task")
                workflow.mission_source_baseline = metadata["source_baseline"]
                workflow.mission_build_selection = metadata["build_selection"]
                if hasattr(workflow.validator, "bind_build_selection"):
                    workflow.validator.bind_build_selection(task_id, selection)
            if restoring and metadata.get("tool_selection") is not None:
                from .mission_tools import MissionToolSelection, bind_mission_tools
                selection = MissionToolSelection.from_dict(metadata["tool_selection"])
                if ([s["server_id"] for s in selection.mcp_servers] != list(mcp_server_ids)
                        or selection.use_rag != metadata.get("use_rag", False)):
                    raise ForgePilotError("mission_frozen_config_missing", "Mission tool selection changed")
                bind_mission_tools(workflow, task_id, selection)
            self._services[task_id] = workflow
        return self._services[task_id]

    def _metadata(self, task_id):
        return self.store.missions.metadata(task_id)

    def _check_revision(self, task_id, revision):
        self._metadata(task_id)
        snapshot = self.graphs.snapshot(task_id)
        if snapshot.revision != revision:
            raise _revision_conflict()
        return snapshot

    def _control(self, task_id):
        return self._controls.setdefault(task_id, asyncio.Lock())

    def _idle(self, task_id):
        operation = self._running.get(task_id)
        if operation is not None and not operation.done():
            raise ForgePilotError("task_busy", "Mission operation is still running")

    def list_page(self, limit: int = 20, before: str | None = None, *,
                  repository_path: str | None = None, status: str | None = None):
        page = self.store.missions.list_page(limit, before, repository_path=repository_path, status=status)
        page["items"] = safe_event_data(self.store.redactor, page["items"])
        return page

    def get(self, task_id):
        self._reconcile_delivery(task_id)
        metadata, run, sequence, review = self.store.missions.mission_view(task_id)
        review = asdict(review) if review is not None else None
        result = {key: run[key] for key in ("task_id", "status", "current_revision", "graph_id",
            "graph_version", "created_at", "updated_at", "finished_at")}
        result.update({key: metadata[key] for key in ("repository_path", "request", "task_type",
            "orchestration_mode", "base_ref", "toolchain_profile")})
        result.update(review=review, last_event_id=f"{task_id}:{sequence}" if sequence else None,
                      recovery_required=bool(run["recovery_required"]))
        result.update(source_baseline=metadata.get("source_baseline"), build_selection=metadata.get("build_selection"))
        result.update(use_rag=metadata.get("use_rag", False), mcp_server_ids=metadata.get("mcp_server_ids", []),
                      tool_context=self.store.missions.tool_context(task_id, metadata))
        return safe_event_data(self.store.redactor, result)

    def observability(self, task_id):
        self._metadata(task_id)
        return self.store.missions.observability(task_id)

    def _reconcile_delivery(self, task_id):
        """Complete an outbox intent from WorkspaceManager's durable outcome.

        Git and SQLite cannot share a transaction. The intent is committed before
        Git runs; after a crash the manager's verified journal remains authority.
        Reconciliation publishes an outcome and never repeats the Git operation.
        """
        control = self._controls.get(task_id)
        if control is not None and control.locked():
            return
        with self.store.missions.delivery_reconciliation(task_id) as row:
            if row is None or row["event_type"] != "workspace.delivery_requested":
                return
            intent = json.loads(row["payload_json"])
            try:
                workflow = self._services.get(task_id)
                if workflow is not None:
                    workspace = workflow.workspace
                else:
                    from .workspace_manager import WorkspaceManager
                    record = self.graphs.workspace_record(task_id)
                    workspace = WorkspaceManager(Path(record["worktree_path"]).parents[2], artifacts=self.store.artifacts)
                state = workspace._load(task_id)
            except (ForgePilotError, OSError):
                # Inspection remains available if execution tools/configuration
                # are temporarily unavailable. The durable intent is retained.
                return
            if state.get("delivery_operation_id") != intent.get("operation_id"):
                return
            result = state.get("delivery_result")
            if state["status"] == "abandoned":
                result = {"status": "abandoned", "worktree_path": state["worktree_path"], "branch": state["branch"]}
            if result is None or (state["status"] != "abandoned" and result.get("mode") != intent["mode"]):
                return
            self.store.missions.record_delivery_outcome(task_id, "workspace.delivered",
                intent["mode"], result=result, recovered=True)

    def graph(self, task_id):
        snapshot, definition = self.store.missions.graph_view(task_id)
        result = dict(task_id=task_id, graph_id=snapshot.graph_id, graph_version=snapshot.graph_version,
            definition_hash=snapshot.definition_hash, status=snapshot.status.value,
            current_revision=snapshot.revision, definition=definition, nodes=[{
                "node_id": n.node_id, "node_type": n.node_type.value, "status": n.status.value,
                "attempt": n.attempt, "result": n.result.to_dict() if n.result else None,
            } for n in snapshot.nodes])
        return safe_event_data(self.store.redactor, result)

    def artifacts(self, task_id):
        self._metadata(task_id)
        return {"task_id": task_id, "items": [self._artifact(item)
            for item in self.store.artifacts.list_for_task(task_id)]}

    def _artifact(self, item):
        return safe_event_data(self.store.redactor, item.model_dump(mode="json"))

    def artifact(self, task_id, artifact_id):
        self._metadata(task_id)
        return self._artifact(self.store.missions.owned_artifact(task_id, artifact_id))

    def diff(self, task_id):
        values = [item for item in self.artifacts(task_id)["items"] if item["artifact_type"] == "ChangeSet"]
        # Delivery emits the cumulative verified ChangeSet last. Earlier entries
        # remain available as immutable provenance for implementation/repair.
        patch = "".join(file["unified_diff"] for file in values[-1]["content"]["files"]) if values else ""
        return {"task_id": task_id, "patch": patch, "change_sets": values}

    def validation(self, task_id):
        values = [item for item in self.artifacts(task_id)["items"] if item["artifact_type"] == "ValidationReport"]
        return {"task_id": task_id, "items": values, "latest": values[-1] if values else None}

    async def _execute_control(self, operation, *args, **kwargs):
        """Join accepted controls during shutdown, including work offloaded to threads."""
        if self._stopping:
            raise ForgePilotError("server_stopping", "Runtime is stopping")
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            return await operation(*args, **kwargs)
        finally:
            self._operations.discard(task)

    def _build_config(self, fields):
        from .presets import BuildConfig
        from .process import executable
        development = self._development_config()
        config = development.build_config if development else None
        config = config or BuildConfig(executable(None, "cmake"), executable(None, "ctest"), configuration="Debug")
        names = {key: fields.get(key) or getattr(config, key)
                 for key in ("configure_preset", "build_preset", "test_preset")}
        if any(names.values()):
            # Explicit preset semantics replace the host's non-preset fallback flags.
            return replace(config, **names, generator=None, architecture=None, make_program=None)
        return config

    def _preview_workspace(self):
        from .workspace_manager import WorkspaceManager
        configured = getattr(self.store, "workspace_backend", None)
        if isinstance(configured, WorkspaceManager):
            return configured
        development = self._development_config()
        return WorkspaceManager(self.store.artifacts.root / "mission-workspaces", artifacts=self.store.artifacts,
            git_executable=development.git_executable if development else None)

    @staticmethod
    def _preview_fields(fields):
        return {"repository_path": fields["repository_path"], "base_ref": fields.get("base_ref", "HEAD"),
            "baseline_paths": sorted(fields.get("baseline_paths", [])),
            **{key: fields.get(key) for key in ("configure_preset", "build_preset", "test_preset")}}

    async def preview(self, **fields):
        return await self.runtime.submit(self._execute_control(self._preview, fields))

    async def _preview(self, fields):
        async with self._source_lock:
            for token, preview in tuple(self._previews.items()):
                if monotonic() >= preview.expires_at:
                    await self._discard_pending_preview(token)
            return await self._preview_locked(fields)

    async def _preview_locked(self, fields):
        from .presets import Presets
        from .build_process import build_environment
        if len(self._previews) >= 8:
            raise ForgePilotError("baseline_capacity", "Discard an unused source preview before creating another")
        fields = self._preview_fields(fields)
        workspace, task_id = self._preview_workspace(), str(uuid4())
        config = self._build_config(fields)
        try:
            await self._create_workspace(workspace, fields, task_id)
            source = await asyncio.to_thread(workspace.source_baseline_view, task_id, fields["baseline_paths"])
            record = workspace.record(task_id)
            patch, patch_sha = "", None
            if fields["baseline_paths"]:
                if record["base_ref"] != source["source_head"]:
                    raise ForgePilotError("baseline_changed", "Selected working changes require the current HEAD baseline")
                prepared = await asyncio.to_thread(workspace.prepare_baseline, task_id, fields["baseline_paths"])
                if prepared["source_head"] != source["source_head"] or prepared["file_hashes"] != source["file_hashes"]:
                    raise ForgePilotError("baseline_changed", "Selected source changed; preview again")
                patch, patch_sha = prepared["patch_text"], prepared["sha256"]
            selection = await asyncio.to_thread(lambda: Presets(Path(record["worktree_path"]), build_environment(),
                overrides=source["files"]).resolve_mission(config))
            token = uuid4().hex + uuid4().hex
            self._previews[token] = _SourcePreview(task_id, workspace, fields, source["source_head"], record["base_ref"],
                source["file_hashes"], patch_sha, selection, config, monotonic() + 900)
            return {"preview_token": token, "base_commit": record["base_ref"],
                "source_head": source["source_head"], "baseline_paths": fields["baseline_paths"],
                "changed_files": source["changed_files"], "patch": self.store.redactor.text(patch),
                "build": selection.to_dict()}
        except BaseException as creation_error:
            try:
                expected = await asyncio.to_thread(workspace.inspect_task, task_id)
            except FileNotFoundError:
                raise creation_error
            await asyncio.to_thread(workspace.cleanup, task_id, approved=True, expected=expected)
            raise

    async def discard_preview(self, token):
        return await self.runtime.submit(self._execute_control(self._discard_preview, token))

    async def _discard_preview(self, token):
        async with self._source_lock:
            await self._discard_pending_preview(token)

    async def _discard_pending_preview(self, token):
        preview = self._previews.get(token)
        if preview is not None:
            expected = await asyncio.to_thread(preview.workspace.inspect_task, preview.task_id)
            await asyncio.to_thread(preview.workspace.cleanup, preview.task_id, approved=True, expected=expected)
            self._previews.pop(token)

    async def create(self, **fields):
        return await self.runtime.submit(self._execute_control(self._admit, fields))

    async def create_locked(self, task_spec, *, allow_locked_test_scope=False,
                            locked_authorization_ref=None, **fields):
        """Trusted in-process entry for a pre-registered TaskSpec.

        The HTTP CreateMission schema has no such field. A caller must bind the
        exact frozen content to the repository, toolchain, and task request.
        Editable named tests still require a separate plan-bound approval.
        """
        return await self.runtime.submit(self._execute_control(self._admit, fields, locked_task_spec=task_spec,
            allow_locked_test_scope=allow_locked_test_scope,
            locked_authorization_ref=locked_authorization_ref))

    async def _admit(self, fields, *, locked_task_spec=None, allow_locked_test_scope=False,
                     locked_authorization_ref=None):
        with self.capacity.admission("Mission capacity reached"):
            operation = asyncio.current_task()
            self._admissions.add(operation)
            try:
                if fields.get("preview_token"):
                    async with self._source_lock:
                        return await self._create(fields, locked_task_spec=locked_task_spec,
                            allow_locked_test_scope=allow_locked_test_scope,
                            locked_authorization_ref=locked_authorization_ref)
                return await self._create(fields, locked_task_spec=locked_task_spec,
                    allow_locked_test_scope=allow_locked_test_scope,
                    locked_authorization_ref=locked_authorization_ref)
            finally:
                self._admissions.discard(operation)

    @staticmethod
    async def _create_workspace(workspace, fields, task_id):
        worker = asyncio.create_task(asyncio.to_thread(workspace.create,
            fields["repository_path"], task_id, base_ref=fields["base_ref"]))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A worktree process must settle before identity-checked cleanup.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise

    async def _create(self, fields, *, locked_task_spec=None, allow_locked_test_scope=False,
                      locked_authorization_ref=None):
        if self._stopping or self.runtime.stopping:
            raise ForgePilotError("server_stopping", "Runtime is stopping")
        if fields["toolchain_profile"] not in {"auto", "cmake", "cmake-ctest-v1"}:
            raise ForgePilotError("invalid_toolchain_profile", "Unknown trusted toolchain profile")
        if fields["orchestration_mode"] not in {"multi_agent", "single_agent"}:
            raise ForgePilotError("invalid_mode", "Unknown mission topology")
        if fields["orchestration_mode"] == "single_agent" and locked_task_spec is None:
            raise ForgePilotError("policy_denied", "Single-Agent baseline requires a trusted locked TaskSpec")
        if allow_locked_test_scope and locked_task_spec is None:
            raise ForgePilotError("policy_denied", "Editable test scope requires a locked TaskSpec")
        if allow_locked_test_scope and (not isinstance(locked_authorization_ref, str)
                or re.fullmatch(r"[0-9a-f]{64}", locked_authorization_ref) is None):
            raise ForgePilotError("policy_denied", "Locked editable test scope requires an auditable manifest hash")
        if not allow_locked_test_scope and locked_authorization_ref is not None:
            raise ForgePilotError("policy_denied", "Manifest authorization cannot be attached to a normal mission")
        if locked_task_spec is not None:
            from .agents.contracts import validate_task_spec
            locked_task_spec = validate_task_spec(locked_task_spec)
            if (fields["request"] != locked_task_spec.objective
                    or fields["task_type"] not in {"auto", locked_task_spec.task_type}
                    or fields["base_ref"] != locked_task_spec.repository.base_ref):
                raise ForgePilotError("policy_denied", "Locked TaskSpec does not match the mission request or base")
        token = fields.get("preview_token")
        preview = self._previews.get(token) if token else None
        if token and (preview is None or monotonic() >= preview.expires_at or self._preview_fields(fields) != preview.fields):
            raise ForgePilotError("baseline_changed", "Source preview expired or selection changed; preview and confirm again")
        config = self._build_config(fields)
        config_requires_preview = any(getattr(config, key)
            for key in ("configure_preset", "build_preset", "test_preset"))
        if config_requires_preview and preview is None:
            raise ForgePilotError("baseline_approval", "Preview the resolved preset configuration before creating the task")
        if fields.get("baseline_paths") and (preview is None or fields.get("baseline_confirmed") is not True):
            raise ForgePilotError("baseline_approval", "Preview and explicitly confirm selected source changes")
        if preview is not None:
            if not preview.selection.matches_config(config):
                raise ForgePilotError("build_selection_changed", "Trusted build configuration changed; preview and confirm again")
            preview.selection.verify_tools(preview.build_config.cmake, preview.build_config.ctest)
            source = await asyncio.to_thread(preview.workspace.source_baseline_view, preview.task_id,
                                             fields.get("baseline_paths", ()))
            if source["source_head"] != preview.source_head or source["file_hashes"] != preview.file_hashes:
                raise ForgePilotError("baseline_changed", "Source HEAD or selected files changed; preview and confirm again")
        task_id = preview.task_id if preview else str(uuid4())
        workflow = self._workflow(task_id, mcp_server_ids=fields.get("mcp_server_ids", ()),
                                  single_agent=fields["orchestration_mode"] == "single_agent")
        try:
            # Trusted profile selection never accepts executable paths or shell text.
            profile = workflow.validator.profile.profile_id
            if locked_task_spec is not None and (
                    locked_task_spec.repository.toolchain_profile != profile
                    or locked_task_spec.data_policy != getattr(workflow, "mission_data_policy", DataPolicy())):
                raise ForgePilotError("policy_denied", "Locked TaskSpec changed trusted toolchain or data policy")
            from .presets import Presets
            from .build_process import build_environment
            if preview is None:
                await self._create_workspace(workflow.workspace, fields, task_id)
            else:
                if workflow.workspace.root != preview.workspace.root:
                    raise ForgePilotError("workspace_unavailable", "Preview and task must share the same WorkspaceManager root")
                self._previews.pop(token)
                if preview.patch_sha256 is not None:
                    prepared = await asyncio.to_thread(workflow.workspace.prepare_baseline, task_id, fields["baseline_paths"])
                    if (prepared["sha256"] != preview.patch_sha256 or prepared["source_head"] != preview.source_head
                            or prepared["file_hashes"] != preview.file_hashes):
                        raise ForgePilotError("baseline_changed", "Selected patch changed; preview and confirm again")
                    expected = await asyncio.to_thread(workflow.workspace.inspect_task, task_id)
                    applied = await asyncio.to_thread(workflow.workspace.apply_baseline, task_id,
                        preview.patch_sha256, approved=True, expected=expected)
                    await asyncio.to_thread(workflow.workspace.freeze_source_baseline, task_id,
                        preview.patch_sha256, approved=True, expected=applied)
            record = workflow.workspace.record(task_id)
            selection = await asyncio.to_thread(lambda: Presets(Path(record["worktree_path"]),
                build_environment()).resolve_mission(config))
            if preview and selection.to_dict() != preview.selection.to_dict():
                raise ForgePilotError("build_selection_changed", "Resolved preset selection changed; preview and confirm again")
            if locked_task_spec is None:
                workflow.mission_source_baseline = {"version": 1,
                    "source_commit": preview.base_commit if preview else record["base_ref"],
                    "task_commit": record["base_ref"], "paths": fields.get("baseline_paths", []),
                    "file_hashes": preview.file_hashes if preview else {},
                    "patch_sha256": preview.patch_sha256 if preview else None}
                workflow.mission_build_selection = selection.to_dict()
                if hasattr(workflow.validator, "bind_build_selection"):
                    workflow.validator.bind_build_selection(task_id, selection)
                fields = {**fields, "source_baseline": workflow.mission_source_baseline,
                          "build_selection": selection.to_dict(), "frozen_build_config": asdict(config)}
            from .mission_tools import MissionToolSelection, bind_mission_tools
            selected_servers = tuple({"server_id": item.server_id, "name": item.config.name,
                "tools": list(item.config.selected_tools)} for item in workflow.mission_mcp_snapshots)
            tools = MissionToolSelection(fields.get("use_rag", False),
                getattr(workflow, "mission_data_policy", DataPolicy()), selected_servers)
            tool_context = bind_mission_tools(workflow, task_id, tools)
            fields = {**fields, "use_rag": tools.use_rag, "tool_selection": tools.to_dict(),
                      "tool_context": tool_context}
            request_text = self.store.redactor.text(fields["request"])
            if fields["task_type"] != "auto" and locked_task_spec is None:
                request_text += "\nRequested task type: " + fields["task_type"]
            if len(request_text) > 8192:
                request_text = self.store.redactor.text(fields["request"])
            request = AnalysisRequest(request=request_text, repository=RepositorySpec(
                base_ref=record["base_ref"] if locked_task_spec is None else fields["base_ref"], toolchain_profile=profile),
                data_policy=getattr(workflow, "mission_data_policy", DataPolicy()),
                language=fields.get("language", "zh-CN"))
            if locked_task_spec is None:
                definition = await asyncio.to_thread(workflow._analysis_graph, task_id, request)
            else:
                definition = await asyncio.to_thread(workflow._analysis_graph, task_id, request,
                                                     locked_task_spec=locked_task_spec,
                                                     allow_locked_test_scope=allow_locked_test_scope)
            self.graphs.create_run(task_id, validate_graph(definition),
                                  policy_hash=workflow.runner.policy.definition_hash)
            record = workflow.workspace.record(task_id)
            self.store.missions.admit_mission(task_id, record,
                {**fields, "locked_authorization_ref": locked_authorization_ref, "mcp_server_revisions": list(getattr(workflow, "mission_mcp_revisions", ()))})
            self._launch(task_id, workflow.run(task_id))
            return self.get(task_id)
        except BaseException:
            try:
                self._metadata(task_id)
            except ForgePilotError:
                # Only a newly allocated, app-owned worktree is eligible. The
                # manager proves identity and refuses unknown user bytes.
                try:
                    expected = await asyncio.to_thread(workflow.workspace.inspect_task, task_id)
                    await asyncio.to_thread(workflow.workspace.cleanup, task_id, approved=True, expected=expected)
                except (ForgePilotError, OSError) as cleanup_error:
                    if preview is not None:
                        raise ForgePilotError("baseline_cleanup",
                            "The source baseline could not be finalized or safely removed; inspect its controlled workspace before retrying") from cleanup_error
                self._services.pop(task_id, None)
                candidate = getattr(workflow, "mission_candidate_runner", None)
                if candidate is not None:
                    try:
                        await asyncio.to_thread(candidate.close)
                    except Exception:
                        pass
                self.store.missions.discard_unadmitted_graph(task_id)
            raise

    def _launch(self, task_id, operation, *, admission=None):
        async def drive():
            try:
                await operation
            except asyncio.CancelledError:
                if admission is not None and not admission.done():
                    admission.cancel()
                raise
            except BaseException as exc:
                if admission is not None and not admission.done():
                    admission.set_exception(exc)
                    return
                code = exc.code if isinstance(exc, ForgePilotError) else "mission_internal"
                # Persist a stable failure, never exception strings, model text or credentials.
                self.store.missions.fail_background(task_id, code)
            finally:
                workflow = self._services.get(task_id)
                manager = getattr(workflow, "mission_mcp_manager", None)
                if manager is not None:
                    try:
                        await manager.close()
                    except (Exception, asyncio.CancelledError):
                        # Closing a selected server cannot mask the durable
                        # mission result or retain its workflow reference.
                        pass
                try:
                    if self.graphs.snapshot(task_id).status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                        workflow = self._services.pop(task_id, None)
                        candidate = getattr(workflow, "mission_candidate_runner", None)
                        if candidate is not None:
                            try:
                                await asyncio.to_thread(candidate.close)
                            except Exception:
                                pass
                except ForgePilotError:
                    pass
        running = asyncio.create_task(drive())
        self._running[task_id] = running
        def settled(completed):
            if self._running.get(task_id) is completed:
                self._running.pop(task_id, None)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    code = error.code if isinstance(error, ForgePilotError) else "mission_internal"
                    logging.getLogger(__name__).warning("Mission background cleanup failed (%s)", code)
        running.add_done_callback(settled)
        return running

    async def approve(self, task_id, **fields):
        return await self.runtime.submit(self._execute_control(self._approve, task_id, fields))

    async def approve_existing_tests(self, task_id, *, expected_revision,
                                     task_spec_hash, plan_hash, scope_hash,
                                     exact_paths, authorization_source,
                                     authorization_ref, approved_by):
        """Separate trusted approval of named existing tests before execution.

        This is deliberately absent from HTTP mission creation and execution
        approval. A benchmark adapter must explicitly bind the frozen manifest's
        exact paths and review hashes; a TaskSpec alone grants no test edits.
        """
        return await self.runtime.submit(self._execute_control(self._approve_existing_tests, task_id,
            expected_revision=expected_revision, task_spec_hash=task_spec_hash,
            plan_hash=plan_hash, scope_hash=scope_hash, exact_paths=exact_paths,
            authorization_source=authorization_source,
            authorization_ref=authorization_ref, approved_by=approved_by))

    async def _approve_existing_tests(self, task_id, *, expected_revision,
                                      task_spec_hash, plan_hash, scope_hash,
                                      exact_paths, authorization_source,
                                      authorization_ref, approved_by):
        async with self._control(task_id):
            self._check_revision(task_id, expected_revision)
            self._idle(task_id)
            workflow = self._workflow(task_id)
            workflow._stored_graph(task_id)
            with self.store.missions.consistent_view():
                review = workflow.review(task_id)
                if (task_spec_hash != review.task_spec_hash or plan_hash != review.plan_hash
                        or scope_hash != review.scope_hash):
                    raise _approval_conflict()
                spec = self.store.artifacts.get(review.task_spec_artifact_id).content
                named = tuple(spec.test_policy.modifiable_existing_test_patterns)
                if (not isinstance(exact_paths, (tuple, list)) or tuple(exact_paths) != named
                        or not named or any(any(char in path for char in "*?[") for path in named)):
                    raise ForgePilotError("test_approval_required", "Exact named test paths must match the frozen TaskSpec")
                locked_ref = self._metadata(task_id).get("locked_authorization_ref")
                if (authorization_source != "locked_benchmark_manifest"
                        or authorization_ref != locked_ref or not locked_ref
                        or approved_by != "benchmark-evaluator/" + locked_ref):
                    raise ForgePilotError("test_approval_required", "Test approval lacks the locked manifest authorization")
                if (self.locked_approval_authority is None or
                        self.locked_approval_authority(spec, authorization_source,
                            authorization_ref, approved_by) is not True):
                    raise ForgePilotError("test_approval_required", "No trusted authority verified the locked test grant")
                approval = workflow.approve_existing_tests(review, approved_by=approved_by,
                    audit_event={"event_type": "approval.existing_tests", "data": {
                        "paths": list(named), "review_hash": review.review_hash,
                        "authorization_source": authorization_source,
                        "authorization_ref": authorization_ref, "approved_by": approved_by},
                        "actor": "benchmark-evaluator"})
            return safe_event_data(self.store.redactor, approval)

    async def _approve(self, task_id, fields):
        async with self._control(task_id):
            self._check_revision(task_id, fields["expected_revision"])
            self._idle(task_id)
            workflow = self._workflow(task_id)
            definition = workflow._stored_graph(task_id)
            with self.store.missions.consistent_view():
                review = workflow.review(task_id)
                if any(fields[name] != getattr(review, name) for name in ("task_spec_hash", "plan_hash", "scope_hash")):
                    self.store.missions.record_security_event(event_id=str(uuid4()), task_id=task_id,
                        event_type="approval_denied", severity="warning",
                        attributes={"code": "mission_approval_conflict", "boundary": "execution_approval",
                                    "node_id": "execution_approval"}, publish_event=False)
                    raise _approval_conflict()
                workflow._check_review(review)
                requested = self._metadata(task_id)["task_type"]
                spec = self.store.artifacts.get(review.task_spec_artifact_id)
                if requested != "auto" and spec.content.task_type != requested:
                    self.store.missions.record_security_event(event_id=str(uuid4()), task_id=task_id,
                        event_type="approval_denied", severity="warning",
                        attributes={"code": "mission_approval_conflict", "boundary": "execution_approval",
                                    "node_id": "execution_approval"}, publish_event=False)
                    raise ForgePilotError("mission_approval_conflict", "TaskSpec does not match the requested task type")
            if fields["decision"] == "rejected":
                self.graphs.cancel(task_id, expected_revision=review.revision,
                    rejected_approval={**asdict(review), "approval_id": str(uuid4())})
                return self.get(task_id)
            loop = asyncio.get_running_loop()
            admission = loop.create_future()
            def admitted():
                loop.call_soon_threadsafe(lambda: admission.set_result(None) if not admission.done() else None)
            approval = {name: fields[name] for name in ("task_spec_hash", "plan_hash", "scope_hash")}
            approval["approval_id"] = str(uuid4())
            operation = workflow._run_worker(task_id, workflow.runner.approve_human, task_id,
                definition, "execution_approval", expected_revision=review.revision,
                approval=approval, on_approved=admitted)
            self._launch(task_id, operation, admission=admission)
            await admission
            return self.get(task_id)

    async def cancel(self, task_id, *, expected_revision):
        return await self.runtime.submit(self._execute_control(self._cancel, task_id, expected_revision))

    async def _cancel(self, task_id, expected_revision):
        async with self._control(task_id):
            self._check_revision(task_id, expected_revision)
            # An unloaded mission can be cancelled without restoring its frozen tools.
            self._services.get(task_id, self.graphs).cancel(task_id, expected_revision=expected_revision)
            running = self._running.get(task_id)
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            return self.get(task_id)

    async def retry(self, task_id, *, expected_revision, node_id=None):
        return await self.runtime.submit(self._execute_control(self._retry, task_id, expected_revision, node_id))

    async def _retry(self, task_id, revision, node_id):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            self._idle(task_id)
            workflow = self._workflow(task_id)
            definition = workflow._stored_graph(task_id)
            candidates = [n for n in snapshot.nodes if n.status is NodeStatus.RETRYABLE]
            if node_id is None and len(candidates) == 1:
                node_id = candidates[0].node_id
            if not any(n.node_id == node_id for n in candidates):
                raise ForgePilotError("invalid_state", "Select a retryable mission node")
            workflow.runner._assert_definition(snapshot, validate_graph(definition))
            selected = next(node for node in definition.nodes if node.node_id == node_id)
            if recovery_role(selected) in {"writer", "delivery"}:
                raise ForgePilotError("recovery_required", "Writer and delivery nodes require recovery confirmation")
            if self.store.missions.get_graph_run(task_id)["recovery_required"]:
                raise ForgePilotError("recovery_required", "Inspect and confirm mission recovery first")
            # Commit the retry admission before returning 202. Worker execution
            # may then proceed without keeping the HTTP request alive.
            self.graphs.transition(task_id, node_id, expected_revision=revision,
                source=NodeStatus.RETRYABLE, target=NodeStatus.READY,
                event_type="node_retry_started", payload={"reason": "explicit_retry"})
            self._launch(task_id, workflow.run(task_id))
            return self.get(task_id)

    async def resume(self, task_id, *, expected_revision, recovery_token=None):
        return await self.runtime.submit(self._execute_control(self._resume, task_id, expected_revision, recovery_token))

    async def _resume(self, task_id, revision, token):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            self._idle(task_id)
            workflow = self._workflow(task_id)
            graph = workflow._stored_graph(task_id)
            if token is None:
                recover_storage = getattr(workflow, "recover_storage_failure", None)
                if snapshot.status is NodeStatus.FAILED and recover_storage is not None:
                    recovered = await asyncio.to_thread(recover_storage, task_id, expected_revision=revision)
                    if recovered:
                        snapshot = self.graphs.snapshot(task_id)
                        revision = snapshot.revision
                if snapshot.status not in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                    self.store.missions.request_recovery(task_id, revision)
                plan = await asyncio.to_thread(workflow.runner.prepare_recovery, task_id, graph)
                return {"mission": self.get(task_id), "recovery": plan.to_dict()}
            await asyncio.to_thread(workflow.runner.confirm_recovery, task_id, graph,
                                    token=token, expected_revision=revision)
            self._launch(task_id, workflow.run(task_id))
            return {"mission": self.get(task_id), "recovery": None}

    async def delivery(self, task_id, *, expected_revision, mode="preview", token=None, target_branch=None):
        return await self.runtime.submit(self._execute_control(self._delivery, task_id, expected_revision, mode, token, target_branch))

    async def _delivery(self, task_id, revision, mode, token, target_branch):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            if mode == "abandon" and snapshot.status not in TERMINAL_NODE_STATUSES:
                raise ForgePilotError("mission_cancel_required", "Cancel the mission before abandoning its workspace")
            self._idle(task_id)
            if mode != "abandon" and snapshot.status is not NodeStatus.SUCCEEDED:
                raise ForgePilotError("invalid_state", "Delivery requires a successfully validated mission")
            workflow = self._workflow(task_id)
            if mode == "preview" or token is None:
                result = await asyncio.to_thread(workflow.workspace.delivery_preview, task_id, target_branch)
            else:
                bundles = [item for item in self.store.artifacts.list_for_task(task_id) if item.artifact_type == "DeliveryBundle"]
                operation_id = uuid4().hex
                self.store.missions.request_delivery(task_id, revision, mode, operation_id)
                try:
                    result = await asyncio.to_thread(workflow.workspace.deliver, task_id, mode,
                        approved=True, token=token, bundle=bundles[-1].content if bundles else None,
                        operation_id=operation_id)
                except ForgePilotError as exc:
                    self.store.missions.record_delivery_outcome(task_id, "workspace.delivery_failed",
                        mode, error_code=exc.code)
                    raise
                self.store.missions.record_delivery_outcome(task_id, "workspace.delivered", mode, result=result)
            delivery = safe_event_data(self.store.redactor, result)
            if isinstance(result, dict) and "token" in result:
                # This freshly generated one-time approval capability must reach
                # the user for the next request. It is never persisted or emitted
                # in mission events; all other response fields remain sanitized.
                delivery["token"] = result["token"]
            return {"mission": self.get(task_id), "delivery": delivery}

    async def close(self):
        if self.runtime.available:
            await self.runtime.drain(self._close())

    async def _close(self):
        self._stopping = True
        running = [operation for operation in (*self._admissions, *self._running.values()) if not operation.done()]
        for operation in running:
            operation.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        # A recovery or delivery control may still be completing to_thread work.
        # Join it before closing workflows, then drain any execution it launched.
        await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        late = [operation for operation in self._running.values() if not operation.done()]
        for operation in late:
            if not operation.cancelling():
                operation.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        for token in tuple(self._previews):
            await self._discard_preview(token)
        for workflow in self._services.values():
            candidate = getattr(workflow, "mission_candidate_runner", None)
            if candidate is not None:
                await asyncio.to_thread(candidate.close)


__all__ = ["MissionHost"]
