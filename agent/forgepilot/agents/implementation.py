"""Approval-bound implementation, using the durable Graph writer boundary.

Only the host can approve the immutable package or separately approve named
existing tests. Model output describes completion; filesystem observations,
never model-provided diffs or hashes, produce the ChangeSet.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import json

from pydantic import Field
from typing import Literal

from ..artifacts.canonical import content_sha256
from ..artifacts.models import ArtifactModel, TaskSpec, ExecutionPlan, EvidenceBundle
from ..artifacts.sqlite_store import ArtifactStorageError
from ..artifacts.validation import create_artifact, validate_plan_coverage
from ..errors import ForgePilotError
from ..models.accounting import ModelBudget
from ..orchestration.analysis_review import AnalysisReviewStore, scope_content
from ..orchestration.graph import validate_graph
from ..orchestration.models import GraphDefinition, NodeDefinition, NodeMetrics, NodeResult, NodeStatus, NodeType, FailureKind
from ..orchestration.node_handlers import HandlerRegistry
from ..orchestration.runner import GraphRunner
from ..repository import Repository
from ..workspace_manager import WorkspaceManager
from .analysis_tools import validate_evidence
from .implementation_tools import ImplementationTools
from .prompts import get_prompt
from .registry import AgentRegistry


class ImplementationOutcome(ArtifactModel):
    status: Literal["completed", "awaiting_human"]
    summary: str = Field(min_length=1, max_length=2048)


_NEEDS_HUMAN = frozenset({
    "scope_approval_required", "test_approval_required", "file_limit",
    "write_conflict", "edit_not_found", "edit_ambiguous", "approval_stale",
    "implementation_no_changes", "evidence_stale",
})


class ImplementationService:
    """Explicit backend entry point; no Validator, Repair, Reviewer or delivery.

    The host creates the task's WorkspaceManager worktree, stores its TaskSpec
    and ExecutionPlan, calls prepare(), displays review(), then calls approve().
    Repeated run() calls reuse Graph's durable result, including across restarts.
    """

    def __init__(self, events, workspace_manager, gateway, *, limits=None,
                 rg_executable="rg", retrieval_factory=None):
        if not isinstance(workspace_manager, WorkspaceManager):
            raise ForgePilotError("workspace_unavailable", "Implementation requires WorkspaceManager")
        self.events, self.workspace, self.gateway = events, workspace_manager, gateway
        self.agents = AgentRegistry()
        self.definition = self.agents.get("implementer.v1")
        self.limits = self.definition.limits if limits is None else self.agents.validate_limits(
            self.definition.agent_id, limits)
        gateway.router.validate_agent(self.definition)
        self.rg_executable, self.retrieval_factory = rg_executable, retrieval_factory
        self.reviews = AnalysisReviewStore(events)
        self.runner = GraphRunner(events, HandlerRegistry({
            "execution_approval": self._gate, "implementer.v1": self._handler,
        }), workspace_backend=workspace_manager)
        # Optional composition is declared before graph creation, never appended
        # to an already-approved graph with a different definition hash.
        from .analysis import STAGES
        for stage in STAGES:
            self.runner.handlers.register(stage, self._analysis_handler)
        self.runner.handlers.register("join_evidence", self._analysis_join)
        self._analysis_services = {}
        self._loop = None
        self._active = set()
        self._cancel_requested = set()
        self._budgets = {}

    def graph(self):
        return GraphDefinition("controlled-implementation.v1", 1, (
            NodeDefinition("execution_approval", NodeType.HUMAN_GATE, "execution_approval",
                           settings={"checkpoint_type": "plan_approved"}),
            NodeDefinition("implementer", NodeType.AGENT, "implementer.v1",
                           dependencies=("execution_approval",),
                           agent_id=self.definition.agent_id,
                           agent_definition_hash=self.definition.definition_hash,
                           settings={"limits": self.limits.model_dump(mode="json")}),
        ))

    def _analysis_for(self, task_id):
        with self.events._lock:
            if task_id not in self._analysis_services:
                from .analysis import AnalysisService
                self._analysis_services[task_id] = AnalysisService(
                    self.events, self._repository(task_id), self.gateway)
            analysis = self._analysis_services[task_id]
        analysis._loop = self._loop
        return analysis

    def _analysis_handler(self, context):
        return self._analysis_for(context.task_id)._agent_handler(context)

    def _analysis_join(self, context):
        return self._analysis_for(context.task_id)._join(context)

    def _stored_graph(self, task_id):
        run = self.events.missions.get_graph_run(task_id)
        graph = GraphDefinition.from_dict(json.loads(run["definition_json"]))
        if graph.graph_id == "controlled-implementation.v1":
            expected = self.graph()
        elif graph.graph_id == "task-analysis-implementation.v1":
            from .analysis import AnalysisRequest
            draft = next(node for node in graph.nodes if node.node_id == "draft_task_spec")
            from ..orchestration.models import _plain_json
            expected = self._analysis_graph(task_id, AnalysisRequest.model_validate(_plain_json(draft.settings["request"])))
        else:
            raise ForgePilotError("graph_definition", "Task was not created as an implementation graph")
        if graph.definition_hash != expected.definition_hash:
            raise ForgePilotError("graph_definition", "Persisted implementation definition or limits changed")
        return expected

    def _analysis_graph(self, task_id, request):
        analysis = self._analysis_for(task_id).graph(request)
        gate, implementer = self.graph().nodes
        from dataclasses import replace
        from ..orchestration.models import _plain_json
        original_gate = analysis.nodes[-1]
        return GraphDefinition("task-analysis-implementation.v1", 1, (
            *analysis.nodes[:-1], replace(gate, dependencies=original_gate.dependencies,
                                          settings=_plain_json(gate.settings)),
            replace(implementer, dependencies=("execution_approval", "join_evidence"),
                    settings=_plain_json(implementer.settings)),
        ))

    async def prepare_analysis(self, task_id, request):
        """Analyze inside the managed Worktree, then stop at execution approval.

        This composes the existing read-only analysis workflow at graph creation;
        approval runs only Implementer afterward and ends at its checkpoint.
        """
        graph = self._analysis_graph(task_id, request)
        self._create_run(task_id, graph, ())
        return await self._run_worker(task_id, self.runner.run, task_id, graph)

    def _create_run(self, task_id, graph, ids):
        with self.events._lock:
            try:
                current = self.runner.store.snapshot(task_id)
            except ForgePilotError as exc:
                if exc.code != "unknown_graph":
                    raise
                current = None
            record = self.workspace.record(task_id)
            self.workspace.inspect(record)
            if current is None:
                current = self.runner.store.create_run(task_id, validate_graph(graph),
                    inputs=tuple((item, self.events.artifacts.get(item).content_sha256) for item in ids),
                    policy_hash=self.runner.policy.definition_hash)
            if current.definition_hash != graph.definition_hash:
                raise ForgePilotError("graph_definition", "Existing task has a different graph definition")
            run = self.events.missions.get_graph_run(task_id)
            if run["workspace_id"] is None:
                self.events.missions.save_workspace(**record)
                self.events.missions.update_graph_run(task_id, previous_revision=current.revision,
                                                     workspace_id=record["workspace_id"])

    def _package(self, task_id, artifact_ids):
        artifacts = tuple(self.events.artifacts.get(item) for item in artifact_ids)
        self.agents.validate_artifacts(self.definition.agent_id, artifacts)
        if any(item.task_id != task_id for item in artifacts):
            raise ForgePilotError("artifact_scope", "Implementation inputs belong to another task")
        specs = [item for item in artifacts if isinstance(item.content, TaskSpec)]
        plans = [item for item in artifacts if isinstance(item.content, ExecutionPlan)]
        evidence = tuple(item for item in artifacts if isinstance(item.content, EvidenceBundle))
        if len(specs) != 1 or len(plans) != 1 or len(evidence) > 4:
            raise ForgePilotError("artifact_scope", "Expected one TaskSpec, one Plan and bounded Evidence")
        spec, plan = specs[0], plans[0]
        if plan.content.task_spec_artifact_id != spec.artifact_id:
            raise ForgePilotError("artifact_scope", "ExecutionPlan does not reference the frozen TaskSpec")
        validate_plan_coverage(spec.content, plan.content)
        return spec, plan, evidence

    def _repository(self, task_id):
        record = self.workspace.record(task_id)
        try:
            registered = self.runner.store.workspace_record(task_id)
        except ForgePilotError as exc:
            if exc.code != "unknown_graph":
                raise
            registered = None
        if registered is not None and any(registered[key] != record[key] for key in (
                "workspace_id", "task_id", "worktree_path", "repository_path", "base_ref")):
            raise ForgePilotError("workspace_corrupted", "Graph and manager workspace identities differ")
        self.workspace.inspect(record)
        return Repository(Path(record["worktree_path"]), self.rg_executable)

    async def prepare(self, task_id, task_spec_artifact_id, plan_artifact_id, *, evidence_artifact_ids=()):
        ids = (task_spec_artifact_id, plan_artifact_id, *evidence_artifact_ids)
        if len(ids) != len(set(ids)):
            raise ForgePilotError("artifact_scope", "Duplicate implementation inputs")
        self._package(task_id, ids)
        graph = self.graph()
        self._create_run(task_id, graph, ids)
        # GraphRunner rejects a different graph, input set or budget on repeat.
        return await self._run_worker(task_id, self.runner.run, task_id, graph, input_artifact_ids=ids)

    async def run(self, task_id):
        return await self._run_worker(task_id, self.runner.run, task_id, self._stored_graph(task_id))

    def review(self, task_id):
        return self.reviews.get(task_id)

    def _check_review(self, review):
        if self.reviews.get(review.task_id) != review:
            raise ForgePilotError("approval_stale", "Frozen TaskSpec, Plan, Scope or revision changed")
        snapshot = self.runner.store.snapshot(review.task_id)
        gate = next(node for node in snapshot.nodes if node.node_id == "execution_approval")
        if (snapshot.status is not NodeStatus.AWAITING_HUMAN
                or gate.status is not NodeStatus.AWAITING_HUMAN
                or self.runner.store.recovery_required(review.task_id)):
            raise ForgePilotError("invalid_state", "Execution approval gate is not awaiting review")

    def _save_approval(self, review, approved_by, kind):
        return self.events.missions.save_approval(approval_id=str(uuid4()), task_id=review.task_id,
            task_spec_artifact_id=review.task_spec_artifact_id, plan_artifact_id=review.plan_artifact_id,
            scope_hash=review.scope_hash, approved_by=approved_by, approval_type=kind)

    def approve_existing_tests(self, review, *, approved_by="user"):
        """Independent host approval of the exact test paths listed in TaskSpec."""
        with self.events._lock:
            self._check_review(review)
            spec = self.events.artifacts.get(review.task_spec_artifact_id)
            paths = spec.content.test_policy.modifiable_existing_test_patterns
            if not paths or any(any(c in path for c in "*?[") for path in paths):
                raise ForgePilotError("test_approval_required", "Test approval requires concrete named files")
            repository = self._repository(review.task_id)
            for path in paths:
                if repository.normalize_plan_path(path) != path or not repository._path(path).is_file():
                    raise ForgePilotError("test_approval_required", "Test approval requires existing canonical files")
            return self._save_approval(review, approved_by, "existing_tests")

    async def approve(self, review, *, approved_by="user"):
        def approve_and_run():
            with self.events._lock:
                self._check_review(review)
                self._save_approval(review, approved_by, "execution")
            return self.runner.approve_human(review.task_id, self._stored_graph(review.task_id), "execution_approval",
                                             expected_revision=review.revision)
        return await self._run_worker(review.task_id, approve_and_run)

    def cancel(self, task_id, *, expected_revision):
        return self.runner.cancel(task_id, expected_revision=expected_revision)

    async def _run_worker(self, task_id, operation, *args, **kwargs):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ForgePilotError("implementation_loop", "Service must use one event loop")
        if task_id in self._active:
            raise ForgePilotError("graph_conflict", "Implementation is already running")
        self._loop = loop
        self._active.add(task_id)
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self._cancel_requested.add(task_id)
            with self.events._lock:
                snapshot = self.runner.store.snapshot(task_id)
                if snapshot.status not in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                    self.runner.cancel(task_id, expected_revision=snapshot.revision)
            # The writer lease must outlive all asynchronous model/tool activity.
            drain = asyncio.gather(worker, return_exceptions=True)
            while not drain.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(drain)
            raise
        finally:
            analysis = self._analysis_services.get(task_id)
            if analysis is not None:
                await analysis._drain(task_id)
            self._active.discard(task_id)

    def _gate(self, context):
        spec, plan, _ = self._package(context.task_id, context.input_artifact_ids)
        self.reviews.bind(context.task_id, spec, plan)
        return NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=context.input_artifact_ids,
                          decision_summary="Approve frozen TaskSpec, ExecutionPlan and Scope")

    def _granted(self, task_id, spec, plan, kind):
        with self.events._lock:
            ids = self.events._db.execute(
                "SELECT approval_id FROM approvals WHERE task_id=? AND approval_type=? AND status='approved'",
                (task_id, kind)).fetchall()
            for row in ids:
                grant = self.events.missions.get_approval(row[0])
                if (grant["valid"] and grant["task_spec_artifact_id"] == spec.artifact_id
                        and grant["task_spec_hash"] == spec.content_sha256
                        and grant["plan_artifact_id"] == plan.artifact_id
                        and grant["plan_hash"] == plan.content_sha256
                        and grant["scope_hash"] == content_sha256(scope_content(spec.content))):
                    return True
        return False

    def _handler(self, context):
        # Worker-thread writer ownership is retained until the coroutine drains.
        future = asyncio.run_coroutine_threadsafe(self._tracked(context), self._loop)
        try:
            return future.result()
        except ArtifactStorageError:
            raise
        except ForgePilotError as exc:
            return self._error_result(context, exc.code)
        except (ValueError, TypeError):
            return self._error_result(context, "implementation_contract")

    def _metrics(self, context):
        budget = self._budgets.get((context.task_id, context.node.node_id))
        return NodeMetrics() if budget is None else NodeMetrics(model_calls=budget.model_calls,
            tool_calls=budget.tool_calls, total_tokens=budget.input_tokens + budget.output_tokens)

    def _error_result(self, context, code):
        human = code in _NEEDS_HUMAN
        return NodeResult(NodeStatus.AWAITING_HUMAN if human else NodeStatus.FAILED,
            decision_summary=code, error_summary=code, metrics=self._metrics(context),
            failure_kind=None if human else FailureKind.TOOL_POLICY_VIOLATION)

    def _cancelled(self, context):
        return context.task_id in self._cancel_requested or context.is_cancelled()

    async def _tracked(self, context):
        async def cancelled():
            while not self._cancelled(context):
                await asyncio.sleep(0.02)
        work = asyncio.create_task(self._agent(context))
        monitor = asyncio.create_task(cancelled())
        try:
            await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
            if self._cancelled(context):
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work
                return NodeResult(NodeStatus.CANCELLED, metrics=self._metrics(context))
            return await work
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor

    async def _agent(self, context):
        definition = self.agents.get(context.node.agent_id or self.definition.agent_id)
        from ..orchestration.models import _plain_json
        limits = self.agents.validate_limits(definition.agent_id,
            _plain_json(context.node.settings["limits"]) if "limits" in context.node.settings else self.limits)
        spec, plan, evidence = self._package(context.task_id, context.input_artifact_ids)
        repository = self._repository(context.task_id)
        approved_tests = spec.content.test_policy.modifiable_existing_test_patterns if self._granted(
            context.task_id, spec, plan, "existing_tests") else ()

        def check_approval():
            if self._cancelled(context):
                raise asyncio.CancelledError()
            active_budget = self._budgets.get((context.task_id, context.node.node_id))
            if active_budget is not None:
                active_budget.remaining_seconds()
            if not self._granted(context.task_id, spec, plan, "execution"):
                raise ForgePilotError("approval_stale", "Frozen implementation package is not approved")
            if approved_tests and not self._granted(context.task_id, spec, plan, "existing_tests"):
                raise ForgePilotError("test_approval_required", "Existing test approval is no longer valid")

        check_approval()
        # Validate only repository evidence; external claims remain untrusted
        # context and cannot authorize any tool or path.
        for artifact in evidence if definition.role == "implementer" else ():
            local = tuple(item for item in artifact.content.items if item.source in {"local_repository", "rag"})
            if local:
                validate_evidence(EvidenceBundle(items=local, summary=artifact.content.summary), repository)
        if not spec.content.data_policy.cloud_model_allowed:
            route = self.gateway.router.validate_agent(definition)
            if any(profile and urlsplit(profile.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
                   for profile in (route.primary, route.fallback)):
                raise ForgePilotError("policy_denied", "Data policy forbids the configured cloud route")
        key = (context.task_id, context.node.node_id)
        if key not in self._budgets:
            if context.attempt > 1:
                raise ForgePilotError("llm_budget", "Restart cannot replenish an unknown implementation budget")
            self._budgets[key] = ModelBudget.from_limits(limits)
        budget = self._budgets[key]
        # The factory is host-owned and receives the task Worktree, never a main
        # checkout. Cloud-disabled tasks do not invoke an unknown RAG adapter.
        retrieval = (self.retrieval_factory(repository) if self.retrieval_factory
                     and spec.content.data_policy.cloud_model_allowed else None)
        controlled = ImplementationTools(repository, spec, plan,
            capabilities=definition.capabilities, approved_test_paths=approved_tests,
            retrieval=retrieval, redactor=self.gateway.redactor,
            is_cancelled=lambda: self._cancelled(context), approval_check=check_approval)
        registry = controlled.registry
        payload = {"task_spec": spec.content.model_dump(mode="json"),
                   "execution_plan": plan.content.model_dump(mode="json"),
                   "evidence": [item.content.model_dump(mode="json") for item in evidence] if definition.role == "implementer" else [],
                   "allowed_paths": sorted({path for step in plan.content.steps
                                            if step.owner in {"implementer", "implementer.v1"} for path in step.expected_files}),
                   "approved_existing_tests": list(approved_tests)}
        payload.update(self._extra_agent_payload(context))
        messages = [{"role": "system", "content": get_prompt(definition.agent_id, definition.role)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        client = self.gateway.for_agent(definition, budget=budget)
        with registry.as_capabilities(definition.capabilities):
            tools = registry.get_specs()
            while True:
                check_approval()
                response = await client.chat(messages, tools, output_schema=ImplementationOutcome.model_json_schema())
                check_approval()
                if not response.message.tool_calls:
                    break
                # Reject malformed or forbidden calls anywhere in a batch before
                # dispatching its first write. Writes themselves stay serial.
                for call in response.message.tool_calls:
                    registry.validate_call(call.function.name, call.function.arguments,
                                           capabilities=definition.capabilities)
                budget.consume_tools(len(response.message.tool_calls))
                messages.append(response.message.model_dump(exclude_none=True))
                for call in response.message.tool_calls:
                    check_approval()
                    try:
                        async with asyncio.timeout(budget.remaining_seconds()):
                            result = await registry.execute(call.function.name, call.function.arguments)
                    except TimeoutError:
                        raise ForgePilotError("llm_budget", "Implementation tool time budget exhausted") from None
                    if not result.success:
                        code = (result.error or "implementation_tool_failed").split(":", 1)[0]
                        raise ForgePilotError(code, "Controlled implementation tool was blocked")
                    messages.append({"role": "tool", "tool_call_id": call.id,
                        "content": json.dumps({"content_is_untrusted": True, "output": result.output}, ensure_ascii=False)})
        outcome = ImplementationOutcome.model_validate_json(response.message.content)
        controlled.audit()
        check_approval()
        if outcome.status == "awaiting_human":
            return NodeResult(NodeStatus.AWAITING_HUMAN, decision_summary=self.gateway.redactor.text(outcome.summary),
                              metrics=self._metrics(context))
        changes = controlled.change_set(summary=self.gateway.redactor.text(outcome.summary))
        artifact = create_artifact(task_id=context.task_id, producer=definition.agent_id,
            producer_node=context.node.node_id, parent_artifact_ids=context.input_artifact_ids, content=changes)
        with self.events._lock:
            check_approval()
            self.events.artifacts.put(artifact)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                          decision_summary="ChangeSet verified against approved plan", metrics=self._metrics(context))

    def _extra_agent_payload(self, context):
        return {}


__all__ = ["ImplementationService", "ImplementationOutcome"]
