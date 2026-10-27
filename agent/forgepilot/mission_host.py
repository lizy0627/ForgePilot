"""Mission application boundary sharing the existing host, Graph and services.

HTTP never supplies model configuration, tools, graph definitions or capability
grants. Work runs on TaskHost's owning loop and survives request disconnects.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from .agents.analysis import AnalysisRequest
from .artifacts.models import DataPolicy, RepositorySpec
from .errors import ForgePilotError
from .mission_events import append_mission_event, safe_event_data
from .orchestration.analysis_review import AnalysisReviewStore
from .orchestration.graph import validate_graph
from .orchestration.models import GraphDefinition, NodeResult, NodeStatus
from .orchestration.recovery import recovery_role
from .orchestration.store import GraphStore, _now


def _revision_conflict():
    return ForgePilotError("mission_revision_conflict",
        "Mission revision changed; refresh the mission before retrying")


def _approval_conflict():
    return ForgePilotError("mission_approval_conflict",
        "TaskSpec, Plan or Scope changed; review the current approval package")


class MissionHost:
    def __init__(self, task_host, workflow_factory=None):
        self.host, self.store = task_host, task_host.store
        self.factory = workflow_factory
        self.graphs = GraphStore(self.store)
        self._services = {}
        self._running = {}
        self._admissions = set()
        self._controls = WeakValueDictionary()
        self._stopping = False
        previous_extra = task_host.extra_active
        task_host.extra_active = lambda: previous_extra() + self._active_count()

    def _active_count(self):
        with self.store._lock:
            return self.store._db.execute("""SELECT count(*) FROM task_graph_runs g
                WHERE status NOT IN ('succeeded','failed','cancelled','completed','skipped')
                AND EXISTS(SELECT 1 FROM graph_events e WHERE e.task_id=g.task_id AND e.event_type='task.created')""").fetchone()[0] + len(self._admissions)

    def _workflow(self, task_id):
        if task_id not in self._services:
            if self.factory is None:
                from .mission_factory import build_mission_workflow
                workflow = build_mission_workflow(self.host)
            else:
                workflow = self.factory(self.store)
            self._services[task_id] = workflow
        return self._services[task_id]

    def _metadata(self, task_id):
        with self.store._lock:
            row = self.store._db.execute("SELECT payload_json FROM graph_events WHERE task_id=? "
                "AND event_type='task.created' ORDER BY sequence LIMIT 1", (task_id,)).fetchone()
            if row is None:
                raise ForgePilotError("unknown_mission", "Mission does not exist")
            return json.loads(row[0])

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

    def get(self, task_id):
        self._reconcile_delivery(task_id)
        with self.store._lock:
            metadata = self._metadata(task_id)
            run = self.store.missions.get_graph_run(task_id)
            sequence = self.store._db.execute("SELECT MAX(sequence) FROM graph_events WHERE task_id=?", (task_id,)).fetchone()[0]
            review = None
            if run["task_spec_artifact_id"] and run["plan_artifact_id"]:
                review = asdict(AnalysisReviewStore(self.store).get(task_id))
            result = {key: run[key] for key in ("task_id", "status", "current_revision", "graph_id",
                "graph_version", "created_at", "updated_at", "finished_at")}
            result.update({key: metadata[key] for key in ("repository_path", "request", "task_type",
                "orchestration_mode", "base_ref", "toolchain_profile")})
            result.update(review=review, last_event_id=f"{task_id}:{sequence}" if sequence else None,
                          recovery_required=bool(run["recovery_required"]))
            return safe_event_data(self.store.redactor, result)

    def _reconcile_delivery(self, task_id):
        """Complete an outbox intent from WorkspaceManager's durable outcome.

        Git and SQLite cannot share a transaction. The intent is committed before
        Git runs; after a crash the manager's verified journal remains authority.
        Reconciliation publishes an outcome and never repeats the Git operation.
        """
        control = self._controls.get(task_id)
        if control is not None and control.locked():
            return
        with self.store._lock:
            row = self.store._db.execute("""SELECT sequence,event_type,payload_json FROM graph_events
                WHERE task_id=? AND event_type IN ('workspace.delivery_requested','workspace.delivered',
                'workspace.delivery_failed') ORDER BY sequence DESC LIMIT 1""", (task_id,)).fetchone()
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
            with self.store._transaction():
                append_mission_event(self.store, task_id, "workspace.delivered", {
                    "mode": intent["mode"], "result": result, "recovered": True}, actor="user")

    def graph(self, task_id):
        with self.store._lock:
            self._metadata(task_id)
            snapshot = self.graphs.snapshot(task_id)
            definition = json.loads(self.store.missions.get_graph_run(task_id)["definition_json"])
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
        with self.store._lock:
            owner = self.store._db.execute("SELECT task_id FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if owner is None or owner[0] != task_id:
                raise ForgePilotError("unknown_artifact", "Artifact does not belong to this mission")
            return self._artifact(self.store.artifacts.get(artifact_id))

    def diff(self, task_id):
        values = [item for item in self.artifacts(task_id)["items"] if item["artifact_type"] == "ChangeSet"]
        # Delivery emits the cumulative verified ChangeSet last. Earlier entries
        # remain available as immutable provenance for implementation/repair.
        patch = "".join(file["unified_diff"] for file in values[-1]["content"]["files"]) if values else ""
        return {"task_id": task_id, "patch": patch, "change_sets": values}

    def validation(self, task_id):
        values = [item for item in self.artifacts(task_id)["items"] if item["artifact_type"] == "ValidationReport"]
        return {"task_id": task_id, "items": values, "latest": values[-1] if values else None}

    async def create(self, **fields):
        return await self.host._submit(self._admit(fields))

    async def _admit(self, fields):
        if len(self.host._tasks) + self.host.extra_active() >= self.host.max_tasks:
            raise ForgePilotError("task_capacity", "Mission capacity reached")
        operation = asyncio.current_task()
        self._admissions.add(operation)
        try:
            return await self._create(fields)
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

    async def _create(self, fields):
        if self._stopping or self.host._stopping:
            raise ForgePilotError("server_stopping", "Runtime is stopping")
        if fields["toolchain_profile"] not in {"auto", "cmake", "cmake-ctest-v1"}:
            raise ForgePilotError("invalid_toolchain_profile", "Unknown trusted toolchain profile")
        if fields["orchestration_mode"] != "multi_agent":
            raise ForgePilotError("invalid_mode", "Mission mode must be multi_agent")
        task_id = str(uuid4())
        workflow = self._workflow(task_id)
        try:
            await self._create_workspace(workflow.workspace, fields, task_id)
            # Trusted profile selection never accepts executable paths or shell text.
            profile = workflow.validator.profile.profile_id
            request_text = self.store.redactor.text(fields["request"])
            if fields["task_type"] != "auto":
                request_text += "\nRequested task type: " + fields["task_type"]
            if len(request_text) > 8192:
                request_text = self.store.redactor.text(fields["request"])
            request = AnalysisRequest(request=request_text, repository=RepositorySpec(
                base_ref=fields["base_ref"], toolchain_profile=profile),
                data_policy=getattr(workflow, "mission_data_policy", DataPolicy()))
            definition = await asyncio.to_thread(workflow._analysis_graph, task_id, request)
            self.graphs.create_run(task_id, validate_graph(definition),
                                  policy_hash=workflow.runner.policy.definition_hash)
            record = workflow.workspace.record(task_id)
            with self.store._transaction():
                stamp = _now()
                self.store._db.execute("""INSERT INTO workspace_records(workspace_id,task_id,
                    repository_path,worktree_path,base_ref,head_commit,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", tuple(record[key] for key in (
                    "workspace_id", "task_id", "repository_path", "worktree_path", "base_ref", "head_commit", "status")) + (stamp, stamp))
                self.store._db.execute("UPDATE task_graph_runs SET workspace_id=? WHERE task_id=?",
                                       (record["workspace_id"], task_id))
                append_mission_event(self.store, task_id, "task.created", fields, actor="user")
                append_mission_event(self.store, task_id, "workspace.created", {
                    "workspace_id": record["workspace_id"], "base_ref": record["base_ref"],
                    "head_commit": record["head_commit"]}, actor="workspace-manager")
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
                except (ForgePilotError, OSError):
                    pass
                self._services.pop(task_id, None)
                with self.store._transaction():
                    self.store._db.execute("DELETE FROM task_graph_runs WHERE task_id=?", (task_id,))
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
                with self.store._transaction():
                    row = self.store._db.execute("SELECT status FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone()
                    if row is not None and row[0] not in {"cancelled", "failed", "succeeded"}:
                        stamp = _now()
                        self.store._db.execute("UPDATE task_graph_runs SET status='failed',finished_at=? WHERE task_id=?", (stamp, task_id))
                        append_mission_event(self.store, task_id, "task.failed", {"error_code": code})
            finally:
                try:
                    if self.graphs.snapshot(task_id).status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                        self._services.pop(task_id, None)
                except ForgePilotError:
                    pass
        running = asyncio.create_task(drive())
        self._running[task_id] = running
        running.add_done_callback(lambda completed: self._running.pop(task_id, None)
            if self._running.get(task_id) is completed else None)
        return running

    async def approve(self, task_id, **fields):
        return await self.host._submit(self._approve(task_id, fields))

    async def _approve(self, task_id, fields):
        async with self._control(task_id):
            self._check_revision(task_id, fields["expected_revision"])
            self._idle(task_id)
            workflow = self._workflow(task_id)
            with self.store._lock:
                review = workflow.review(task_id)
                if any(fields[name] != getattr(review, name) for name in ("task_spec_hash", "plan_hash", "scope_hash")):
                    raise _approval_conflict()
                workflow._check_review(review)
                requested = self._metadata(task_id)["task_type"]
                spec = self.store.artifacts.get(review.task_spec_artifact_id)
                if requested != "auto" and spec.content.task_type != requested:
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
                workflow._stored_graph(task_id), "execution_approval", expected_revision=review.revision,
                approval=approval, on_approved=admitted)
            self._launch(task_id, operation, admission=admission)
            await admission
            return self.get(task_id)

    async def cancel(self, task_id, *, expected_revision):
        return await self.host._submit(self._cancel(task_id, expected_revision))

    async def _cancel(self, task_id, expected_revision):
        async with self._control(task_id):
            self._check_revision(task_id, expected_revision)
            workflow = self._workflow(task_id)
            workflow.cancel(task_id, expected_revision=expected_revision)
            running = self._running.get(task_id)
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            return self.get(task_id)

    async def retry(self, task_id, *, expected_revision, node_id=None):
        return await self.host._submit(self._retry(task_id, expected_revision, node_id))

    async def _retry(self, task_id, revision, node_id):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            self._idle(task_id)
            candidates = [n for n in snapshot.nodes if n.status is NodeStatus.RETRYABLE]
            if node_id is None and len(candidates) == 1:
                node_id = candidates[0].node_id
            if not any(n.node_id == node_id for n in candidates):
                raise ForgePilotError("invalid_state", "Select a retryable mission node")
            workflow = self._workflow(task_id)
            definition = workflow._stored_graph(task_id)
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
        return await self.host._submit(self._resume(task_id, expected_revision, recovery_token))

    async def _resume(self, task_id, revision, token):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            self._idle(task_id)
            workflow = self._workflow(task_id)
            graph = workflow._stored_graph(task_id)
            if token is None:
                if snapshot.status not in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                    with self.store._transaction():
                        self._check_revision(task_id, revision)
                        self.store._db.execute("UPDATE task_graph_runs SET recovery_required=1 WHERE task_id=?", (task_id,))
                        append_mission_event(self.store, task_id, "task.recovery_requested", {}, actor="user")
                plan = await asyncio.to_thread(workflow.runner.prepare_recovery, task_id, graph)
                return {"mission": self.get(task_id), "recovery": plan.to_dict()}
            await asyncio.to_thread(workflow.runner.confirm_recovery, task_id, graph,
                                    token=token, expected_revision=revision)
            self._launch(task_id, workflow.run(task_id))
            return {"mission": self.get(task_id), "recovery": None}

    async def delivery(self, task_id, *, expected_revision, mode="preview", token=None, target_branch=None):
        return await self.host._submit(self._delivery(task_id, expected_revision, mode, token, target_branch))

    async def _delivery(self, task_id, revision, mode, token, target_branch):
        async with self._control(task_id):
            snapshot = self._check_revision(task_id, revision)
            self._idle(task_id)
            if mode != "abandon" and snapshot.status is not NodeStatus.SUCCEEDED:
                raise ForgePilotError("invalid_state", "Delivery requires a successfully validated mission")
            workflow = self._workflow(task_id)
            if mode == "preview" or token is None:
                result = await asyncio.to_thread(workflow.workspace.delivery_preview, task_id, target_branch)
            else:
                bundles = [item for item in self.store.artifacts.list_for_task(task_id) if item.artifact_type == "DeliveryBundle"]
                operation_id = uuid4().hex
                with self.store._transaction():
                    self._check_revision(task_id, revision)
                    append_mission_event(self.store, task_id, "workspace.delivery_requested", {
                        "mode": mode, "operation_id": operation_id}, actor="user")
                try:
                    result = await asyncio.to_thread(workflow.workspace.deliver, task_id, mode,
                        approved=True, token=token, bundle=bundles[-1] if bundles else None,
                        operation_id=operation_id)
                except ForgePilotError as exc:
                    with self.store._transaction():
                        append_mission_event(self.store, task_id, "workspace.delivery_failed", {
                            "mode": mode, "error_code": exc.code}, actor="user")
                    raise
                with self.store._transaction():
                    append_mission_event(self.store, task_id, "workspace.delivered", {"mode": mode,
                        "result": result}, actor="user")
            return {"mission": self.get(task_id), "delivery": safe_event_data(self.store.redactor, result)}

    async def close(self):
        if self.host._loop is not None and not self.host._loop.is_closed():
            await self.host._submit(self._close())

    async def _close(self):
        self._stopping = True
        running = [operation for operation in (*self._admissions, *self._running.values()) if not operation.done()]
        for operation in running:
            operation.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


__all__ = ["MissionHost"]
