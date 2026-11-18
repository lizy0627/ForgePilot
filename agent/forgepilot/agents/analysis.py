"""Read-only TaskSpec -> two Analysts -> joined evidence -> reviewed plan.

The synchronous durable graph runs on worker threads; all model and tool I/O
stays on the caller's one asyncio loop. No chat history or memory is accepted.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
import json
from urllib.parse import urlsplit

from pydantic import Field

from ..artifacts.models import ArtifactModel, DataPolicy, EvidenceBundle, ExecutionPlan, RepositorySpec, TaskSpec
from ..artifacts.store import ArtifactStorageError
from ..artifacts.validation import create_artifact
from ..errors import ForgePilotError
from ..mission_observability import model_call, tool_call, validate_tool
from ..models.accounting import ModelBudget
from ..models.errors import category_of
from ..orchestration.analysis_review import AnalysisReviewStore
from ..orchestration.models import GraphDefinition, NodeDefinition, NodeMetrics, NodeResult, NodeStatus, NodeType, FailureKind, _plain_json
from ..orchestration.node_handlers import HandlerRegistry, NodeExecutionError
from ..orchestration.runner import GraphRunner
from ..orchestration.execution import coordinator_for_storage
from .registry import AgentRegistry
from .prompts import get_prompt
from .contracts import output_schema, validate_task_spec, validate_evidence_bundle, validate_execution_plan
from .analysis_tools import build_analysis_registry, validate_evidence


class AnalysisRequest(ArtifactModel):
    request: str = Field(min_length=1, max_length=8192)
    repository: RepositorySpec
    data_policy: DataPolicy = DataPolicy()
    language: str = Field(default="zh-CN", min_length=1, max_length=32)


STAGES = {
    "draft_task_spec": ("orchestrator.v1", TaskSpec),
    "root_cause_analysis": ("root-cause-analyst.v1", EvidenceBundle),
    "impact_test_analysis": ("impact-test-analyst.v1", EvidenceBundle),
    "generate_plan": ("orchestrator.v1", ExecutionPlan),
    "generalist_analysis": ("generalist.v1", EvidenceBundle),
    "generalist_plan": ("generalist.v1", ExecutionPlan),
}


class AnalysisService:
    """Backend entry point; stops at the single execution approval gate.

    gateway must be the shared ModelGateway (inject fake providers in tests).
    approved_mcp / approved_server_ids are trusted user-selection state, never
    parsed from a model, repository text, chat memory, or server descriptions.
    """

    def __init__(self, event_store, repository, gateway, *, retrieval=None,
                 intelligence=None, approved_mcp=(), approved_server_ids=(), policy=None,
                 single_agent=False):
        self.events, self.repository, self.gateway = event_store, repository, gateway
        self.retrieval, self.intelligence = retrieval, intelligence
        self.approved_mcp = tuple(approved_mcp)
        self.approved_server_ids = tuple(approved_server_ids)
        self.single_agent = single_agent
        self.agents = AgentRegistry(include_generalist=single_agent)
        stages = ("generalist_analysis", "generalist_plan") if single_agent else (
            "draft_task_spec", "root_cause_analysis", "impact_test_analysis", "generate_plan")
        for agent_id in {STAGES[stage][0] for stage in stages}:
            gateway.router.validate_agent(self.agents.get(agent_id))
        self.reviews = AnalysisReviewStore(event_store)
        handlers = {stage: self._agent_handler for stage in STAGES}
        handlers["locked_task_spec"] = self._locked_task_spec
        handlers.update(join_evidence=self._join, execution_approval=self._gate)
        self.runner = GraphRunner(event_store, HandlerRegistry(handlers), policy=policy,
                                  coordinator=coordinator_for_storage(event_store))
        self._loop = None
        self._budgets = {}
        self._receipts = {}
        self._active = set()
        self._pending = {}
        self._cancel_requests = set()

    def graph(self, request, *, locked_task_spec=None, allow_locked_test_scope=False):
        request = AnalysisRequest.model_validate(request)
        if allow_locked_test_scope and locked_task_spec is None:
            raise ForgePilotError("policy_denied", "Editable test scope requires a locked TaskSpec")
        if locked_task_spec is not None:
            locked_task_spec = validate_task_spec(locked_task_spec)
            if (locked_task_spec.repository != request.repository
                    or locked_task_spec.data_policy != request.data_policy):
                raise ForgePilotError("policy_denied", "Locked TaskSpec changed trusted repository or data policy")
            self._validate_test_policy(locked_task_spec, allow_locked_test_scope=allow_locked_test_scope)
        def agent(stage, dependencies):
            definition = self.agents.get(STAGES[stage][0])
            settings = {"request": request.model_dump(mode="json")} if stage == "draft_task_spec" else {}
            return NodeDefinition(stage, NodeType.AGENT, stage, dependencies=dependencies,
                agent_id=definition.agent_id, agent_definition_hash=definition.definition_hash, settings=settings)
        draft = (agent("draft_task_spec", ()) if locked_task_spec is None else
                 NodeDefinition("draft_task_spec", NodeType.DETERMINISTIC, "locked_task_spec",
                     settings={"request": request.model_dump(mode="json"),
                               "task_spec": locked_task_spec.model_dump(mode="json"),
                               "allow_locked_test_scope": allow_locked_test_scope}))
        if self.single_agent:
            if locked_task_spec is None:
                raise ForgePilotError("policy_denied", "Single-Agent baseline requires a locked TaskSpec")
            return GraphDefinition("task-analysis-single-locked.v1", 1, (
                draft,
                agent("generalist_analysis", ("draft_task_spec",)),
                NodeDefinition("join_evidence", NodeType.JOIN, "join_evidence",
                    dependencies=("generalist_analysis",)),
                agent("generalist_plan", ("draft_task_spec", "join_evidence")),
                NodeDefinition("execution_approval", NodeType.HUMAN_GATE, "execution_approval",
                    dependencies=("draft_task_spec", "join_evidence", "generalist_plan")),
            ))
        return GraphDefinition("task-analysis-locked.v1" if locked_task_spec is not None else "task-analysis.v1", 1, (
            draft,
            NodeDefinition("analyst_fanout", NodeType.PARALLEL_FANOUT, "parallel_fanout", dependencies=("draft_task_spec",)),
            agent("root_cause_analysis", ("analyst_fanout",)),
            agent("impact_test_analysis", ("analyst_fanout",)),
            NodeDefinition("join_evidence", NodeType.JOIN, "join_evidence", dependencies=("root_cause_analysis", "impact_test_analysis")),
            agent("generate_plan", ("draft_task_spec", "join_evidence")),
            NodeDefinition("execution_approval", NodeType.HUMAN_GATE, "execution_approval",
                dependencies=("draft_task_spec", "join_evidence", "generate_plan")),
        ))

    async def run(self, task_id, request, *, locked_task_spec=None):
        return await self._run_worker(task_id, self.runner.run, task_id,
                                      self.graph(request, locked_task_spec=locked_task_spec))

    def _locked_task_spec(self, context):
        """Publish a caller-supplied frozen TaskSpec without a model draft call.

        This handler is reachable only from an internally constructed graph. The
        persisted settings and Graph definition hash bind the exact input across
        restart, while normal missions still use the model-drafted path.
        """
        request = AnalysisRequest.model_validate(_plain_json(context.node.settings["request"]))
        spec = validate_task_spec(_plain_json(context.node.settings["task_spec"]))
        if spec.repository != request.repository or spec.data_policy != request.data_policy:
            raise ForgePilotError("policy_denied", "Locked TaskSpec changed trusted repository or data policy")
        self._validate_test_policy(spec, allow_locked_test_scope=bool(
            context.node.settings.get("allow_locked_test_scope", False)))
        artifact = create_artifact(task_id=context.task_id, producer="orchestrator.v1",
            producer_node="draft_task_spec", content=spec)
        with self.events.missions.consistent_view():
            if context.is_cancelled():
                return NodeResult(NodeStatus.CANCELLED)
            self.events.artifacts.put(artifact)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                          decision_summary="Frozen TaskSpec validated")

    async def retry(self, task_id, request, node_id, *, expected_revision, locked_task_spec=None):
        return await self._run_worker(task_id, self.runner.retry, task_id,
                                      self.graph(request, locked_task_spec=locked_task_spec),
                                      node_id, expected_revision=expected_revision)

    async def _run_worker(self, task_id, operation, *args, **kwargs):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ForgePilotError("analysis_loop", "AnalysisService must use one event loop")
        if task_id in self._active:
            raise ForgePilotError("graph_conflict", "Analysis is already running")
        self._loop = loop
        self._active.add(task_id)
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            result = await asyncio.shield(worker)
            await self._drain(task_id)
            return result
        except asyncio.CancelledError:
            # Publish cancellation before draining workers; no late Artifact may
            # be published and no join/approval runs after cancellation.
            self._cancel_requests.add(task_id)
            with self.events.missions.consistent_view():
                try:
                    snapshot = self.runner.store.snapshot(task_id)
                    self.runner.cancel(task_id, expected_revision=snapshot.revision)
                except ForgePilotError as exc:
                    if exc.code != "unknown_graph":
                        raise
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            await self._drain(task_id)
            raise
        finally:
            self._active.discard(task_id)

    def cancel(self, task_id, *, expected_revision):
        return self.runner.cancel(task_id, expected_revision=expected_revision)

    async def _drain(self, task_id):
        pending = tuple(self._pending.get(task_id, ()))
        if pending:
            drain = asyncio.gather(*pending, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue

    async def _tracked_agent(self, context):
        task = asyncio.current_task()
        self._pending.setdefault(context.task_id, set()).add(task)
        try:
            return await self._agent(context)
        finally:
            self._pending[context.task_id].discard(task)

    def _agent_handler(self, context):
        original_cancelled = context.is_cancelled
        context = replace(context, is_cancelled=lambda: context.task_id in self._cancel_requests or original_cancelled())
        previous = self._budgets.get((context.task_id, context.node.node_id))
        before = (previous.model_calls, previous.tool_calls, previous.input_tokens + previous.output_tokens) if previous else (0, 0, 0)
        future = asyncio.run_coroutine_threadsafe(self._tracked_agent(context), self._loop)
        try:
            while True:
                try:
                    return future.result(timeout=0.05)
                except FutureTimeout:
                    if context.is_cancelled():
                        future.cancel()
                        return NodeResult(NodeStatus.CANCELLED, decision_summary="Analysis cancelled")
        except ArtifactStorageError:
            raise
        except ForgePilotError as exc:
            category = category_of(exc)
            kind = (FailureKind.REPOSITORY_CONFLICT if exc.code in {"evidence_stale", "stale_evidence"}
                    else FailureKind.MODEL_TRANSIENT if category in {"network", "server"}
                    else FailureKind.MODEL_RATE_LIMIT if category == "rate_limit"
                    else FailureKind.TOOL_POLICY_VIOLATION if category in {"policy", "permission", "authentication"}
                    else FailureKind.MODEL_CONTRACT_VIOLATION)
            return self._failed(context, kind, exc.code, before)
        except (ValueError, TypeError):
            return self._failed(context, FailureKind.MODEL_CONTRACT_VIOLATION,
                                "Agent output violates the analysis contract", before)

    def _failed(self, context, kind, summary, before):
        budget = self._budgets.get((context.task_id, context.node.node_id))
        metrics = NodeMetrics() if budget is None else NodeMetrics(
            model_calls=budget.model_calls-before[0], tool_calls=budget.tool_calls-before[1],
            total_tokens=budget.input_tokens+budget.output_tokens-before[2])
        return NodeResult(NodeStatus.FAILED, failure_kind=kind, error_summary=summary, metrics=metrics)

    def _budget_for(self, context, definition):
        key = (context.task_id, context.node.node_id)
        if key not in self._budgets:
            if context.attempt > 1:
                # Restart never silently replenishes a spent node budget. An
                # unfinished stream has unknown usage, so a fresh analysis task
                # is required instead of guessing a safe remaining allowance.
                raise ForgePilotError("llm_budget", "Restarted analysis requires a new task budget")
            self._budgets[key] = ModelBudget.from_limits(definition.limits)
        return self._budgets[key]

    def _inputs(self, context, content_type):
        artifacts = [self.events.artifacts.get(item) for item in context.input_artifact_ids]
        if any(item.task_id != context.task_id for item in artifacts):
            raise ForgePilotError("artifact_scope", "Analysis inputs belong to another task")
        return [item for item in artifacts if isinstance(item.content, content_type)]

    async def _agent(self, context):
        stage = context.node.node_id
        definition = self.agents.get(context.node.agent_id or STAGES[stage][0])
        schema = output_schema(stage)
        spec_artifact = None
        if stage == "draft_task_spec":
            request = AnalysisRequest.model_validate(_plain_json(context.node.settings["request"]))
            payload = request.model_dump(mode="json")
            data_policy = request.data_policy
            # A model may propose scope; it cannot grant itself data authority or
            # select a different repository/toolchain than the caller supplied.
            schema["properties"]["data_policy"] = {"const": data_policy.model_dump(mode="json")}
            schema["properties"]["repository"] = {"const": request.repository.model_dump(mode="json")}
        else:
            specs = self._inputs(context, TaskSpec)
            if len(specs) != 1:
                raise ValueError("Expected exactly one TaskSpec")
            spec_artifact = specs[0]
            data_policy = spec_artifact.content.data_policy
            payload = {"task_spec": spec_artifact.model_dump(mode="json")}
            if stage in {"generate_plan", "generalist_plan"}:
                schema = output_schema(stage, task_spec=spec_artifact.content)
                evidence = self._inputs(context, EvidenceBundle)
                if len(evidence) != 1:
                    raise ValueError("Expected joined evidence")
                self._verify_evidence(context.task_id, evidence[0].content)
                payload["evidence"] = evidence[0].model_dump(mode="json")
                schema["properties"]["task_spec_artifact_id"] = {"const": spec_artifact.artifact_id}
        self._check_data_route(definition, data_policy)
        key = (context.task_id, stage)
        # Retries spend the same cumulative node budget. Restart recovery is
        # explicit and never resumes an unaccounted model stream.
        budget = self._budget_for(context, definition)
        client = self.gateway.for_agent(definition, budget=budget)
        mcp_enabled = stage in {"root_cause_analysis", "impact_test_analysis", "generalist_analysis"} and data_policy.external_mcp_allowed
        registry = build_analysis_registry(self.repository, role=definition.role,
            retrieval=self._retrieval_for(data_policy), intelligence=self.intelligence,
            approved_mcp=self.approved_mcp if mcp_enabled else (),
            approved_server_ids=self.approved_server_ids if mcp_enabled else (),
            external_mcp_allowed=data_policy.external_mcp_allowed,
            redactor=self.gateway.redactor)
        messages = [{"role": "system", "content": get_prompt(definition.agent_id, stage)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        if stage in {"generate_plan", "generalist_plan"}:
            criteria = spec_artifact.content.acceptance_criteria
            messages.append({"role": "user", "content": (
                "Return only one JSON object with exactly task_spec_artifact_id, steps, summary. "
                f"task_spec_artifact_id must be {spec_artifact.artifact_id}. "
                "Each step must have exactly step_id (STEP-1 etc.), owner (implementer), "
                "depends_on (array), expected_files (array of concrete repository-relative files "
                "to edit), action, verification (array), risk, adds_tests (boolean), and "
                "acceptance_criteria_ids (array). A single step is sufficient for a small fix. "
                "Cover every criterion with its exact verification method; the required pairs are "
                + json.dumps([(item.id, item.verification) for item in criteria]) + ". "
                "Do not plan changes to read-only existing tests or forbidden files. No Markdown."
            )})
        before = (budget.model_calls, budget.tool_calls, budget.input_tokens + budget.output_tokens)
        # A generalist never receives writer tools during analysis, nor the
        # analyst-only extras during planning. The same reviewed stage tools
        # are available to the corresponding multi-Agent nodes.
        capabilities = (() if stage == "draft_task_spec" else
                        self.agents.get("root-cause-analyst.v1").capabilities
                        if stage == "generalist_analysis" else
                        self.agents.get("orchestrator.v1").capabilities
                        if stage == "generalist_plan" else definition.capabilities)
        with registry.as_capabilities(capabilities):
            tools = registry.get_specs()
            while True:
                if context.is_cancelled():
                    return NodeResult(NodeStatus.CANCELLED)
                response = await model_call(self.events, context, definition, client,
                                            messages, tools, output_schema=schema)
                if not response.message.tool_calls:
                    break
                # Validate the whole batch before dispatch. Even if a provider
                # returns a forbidden call, it never reaches an adapter.
                try:
                    for call in response.message.tool_calls:
                        validate_tool(self.events, context, registry, call.function.name,
                                      call.function.arguments, capabilities=capabilities)
                except ForgePilotError as exc:
                    if exc.code not in {"repository_path", "repository_boundary"}:
                        raise
                    # No tool in this batch has run. Return a bounded, generic
                    # correction to the model without relaxing path validation.
                    messages.append(response.message.model_dump(exclude_none=True))
                    for call in response.message.tool_calls:
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "Invalid path. Use an existing repository-relative path from the task tree."})
                    continue
                budget.consume_tools(len(response.message.tool_calls))
                messages.append(response.message.model_dump(exclude_none=True))
                for call in response.message.tool_calls:
                    if context.is_cancelled():
                        return NodeResult(NodeStatus.CANCELLED)
                    try:
                        async with asyncio.timeout(budget.remaining_seconds()):
                            result = await tool_call(self.events, context, registry,
                                                     call.function.name, call.function.arguments)
                    except TimeoutError:
                        raise ForgePilotError("llm_budget", "Agent time budget exhausted during tools") from None
                    if not result.success:
                        code = result.error.split(":", 1)[0] if result.error else "tool_error"
                        if call.function.name.startswith("mcp_") and code in {
                                "mcp_unavailable", "mcp_timeout", "mcp_disconnected", "mcp_transient",
                                "mcp_session_expired", "mcp_capacity", "mcp_transport"}:
                            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({
                                "error_code": code, "available": False,
                                "next_step": "Use the task repository read/search tools for this optional source."})})
                            continue
                        raise ForgePilotError("analysis_tool_failed", "Read-only analysis tool failed")
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result.output})
        if stage in {"generate_plan", "generalist_plan"}:
            def checked_plan(message):
                plan = ExecutionPlan.model_validate_json(message.content)
                validate_execution_plan(spec_artifact.content, plan)
                self._validate_plan_paths(spec_artifact.content, plan)
                return plan

            try:
                content = checked_plan(response.message)
            except ValueError:
                # JSON Schema cannot express every dependency, Artifact, file
                # scope, or test-policy invariant. One correction is permitted;
                # Repository/permission ForgePilotError denials are not caught.
                if budget.model_calls >= budget.max_model_calls:
                    raise
                if context.is_cancelled():
                    return NodeResult(NodeStatus.CANCELLED)
                messages.append({"role": "user", "content": (
                    "The ExecutionPlan failed a dependency, acceptance-criterion, file-scope, "
                    "or test-policy check. Regenerate the complete ExecutionPlan from the "
                    "supplied TaskSpec and evidence. Keep steps acyclic, use exact concrete "
                    "file paths within the approved scope and file budget, and honor the "
                    "existing/new test policy. Do not change task policy or request tools.")})
                response = await model_call(self.events, context, definition, client,
                                            messages, [], output_schema=schema)
                if response.message.tool_calls:
                    raise ForgePilotError("policy_denied", "Plan correction cannot request tools")
                content = checked_plan(response.message)
        else:
            content = STAGES[stage][1].model_validate_json(response.message.content)
        if isinstance(content, TaskSpec):
            validate_task_spec(content)
            if content.data_policy != request.data_policy or content.repository != request.repository:
                raise ForgePilotError("policy_denied", "Model changed trusted task policy")
            self._validate_test_policy(content)
        elif isinstance(content, EvidenceBundle):
            validate_evidence_bundle(content)
            try:
                registry.validate_evidence(content)
            except ForgePilotError as exc:
                # A syntactically valid bundle can still cite a missing file,
                # stale hash, or invalid line span. Give the Analyst one chance
                # to correct its own citations without relaxing the evidence
                # boundary or granting another tool call.
                if (exc.code not in {"repository_path", "evidence_location", "evidence_stale"}
                        or budget.model_calls >= budget.max_model_calls):
                    raise
                if context.is_cancelled():
                    return NodeResult(NodeStatus.CANCELLED)
                messages.append({"role": "user", "content": (
                    "The EvidenceBundle contains a missing or stale repository evidence location. "
                    "Regenerate the complete EvidenceBundle using only repository files and "
                    "hashes already verified by the read-only tool results. Include exact "
                    "repository-relative paths, line spans, symbols, and full current file hashes. "
                    "Do not request tools or change the task policy.")})
                response = await model_call(self.events, context, definition, client,
                                            messages, [], output_schema=schema)
                if response.message.tool_calls:
                    raise ForgePilotError("policy_denied", "Evidence correction cannot request tools")
                content = EvidenceBundle.model_validate_json(response.message.content)
                validate_evidence_bundle(content)
                registry.validate_evidence(content)
            self._receipts[key] = registry.external_receipts
        artifact = create_artifact(task_id=context.task_id, producer=definition.agent_id,
            producer_node=stage, parent_artifact_ids=context.input_artifact_ids, content=content)
        with self.events.missions.consistent_view():
            if context.is_cancelled():
                return NodeResult(NodeStatus.CANCELLED)
            self.events.artifacts.put(artifact)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
            decision_summary=f"{artifact.artifact_type} validated",
            metrics=NodeMetrics(model_calls=budget.model_calls-before[0], tool_calls=budget.tool_calls-before[1],
                total_tokens=budget.input_tokens+budget.output_tokens-before[2]))

    def _check_data_route(self, definition, policy):
        if policy.cloud_model_allowed:
            return
        route = self.gateway.router.validate_agent(definition)
        for profile in (route.primary, route.fallback):
            if profile and urlsplit(profile.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ForgePilotError("policy_denied", "Data policy forbids the configured cloud model route")

    def _retrieval_for(self, policy):
        from ..rag.mission import MissionRetrievalService
        if isinstance(self.retrieval, MissionRetrievalService):
            if self.retrieval.data_policy != policy:
                raise ForgePilotError("policy_denied", "Retrieval differs from frozen task data policy")
            return self.retrieval
        if policy.cloud_model_allowed or self.retrieval is None:
            return self.retrieval
        from ..rag.retrieval import RetrievalService
        # Unknown adapters cannot prove that they do not send task text away.
        # Clone the built-in service without *any* network/vector/refresh path;
        # never mutate shared chat/index services to enforce a task's policy.
        if type(self.retrieval) is not RetrievalService:
            return None
        return RetrievalService(self.retrieval.repository_id, self.repository,
            self.retrieval.config, None, None, code_intelligence=self.intelligence,
            rrf_k=self.retrieval.rrf_k)

    def _verify_evidence(self, task_id, bundle):
        receipts = tuple(receipt for (task, _), values in self._receipts.items()
                         if task == task_id for receipt in values)
        validate_evidence(bundle, self.repository, external_receipts=receipts)

    def _join(self, context):
        bundles = self._inputs(context, EvidenceBundle)
        expected = {"generalist.v1"} if self.single_agent else {
            "root-cause-analyst.v1", "impact-test-analyst.v1"}
        if len(bundles) != len(expected) or {a.producer for a in bundles} != expected:
            raise NodeExecutionError(FailureKind.MODEL_CONTRACT_VIOLATION, "Expected Analyst artifacts are required")
        items = []
        try:
            for bundle in bundles:
                self._verify_evidence(context.task_id, bundle.content)
                for item in bundle.content.items:
                    items.append(item.model_copy(update={"evidence_id": f"EV-{len(items)+1}"}))
            summary = ("Verified single-Agent analysis evidence joined; original provenance retained in parent artifact."
                       if self.single_agent else
                       "Root Cause and Impact/Test evidence joined; original provenance retained in parent artifacts.")
            merged = EvidenceBundle(items=tuple(items), summary=summary)
            artifact = create_artifact(task_id=context.task_id, producer="orchestrator.v1",
                producer_node="join_evidence", parent_artifact_ids=tuple(a.artifact_id for a in bundles), content=merged)
            with self.events.missions.consistent_view():
                if context.is_cancelled():
                    return NodeResult(NodeStatus.CANCELLED)
                self.events.artifacts.put(artifact)
            return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                              decision_summary=f"Joined {len(bundles)} Analyst artifacts and {len(items)} verified evidence items")
        except ForgePilotError as exc:
            raise NodeExecutionError(FailureKind.REPOSITORY_CONFLICT, exc.code) from None

    def _gate(self, context):
        specs, plans = self._inputs(context, TaskSpec), self._inputs(context, ExecutionPlan)
        if len(specs) != 1 or len(plans) != 1:
            raise NodeExecutionError(FailureKind.MODEL_CONTRACT_VIOLATION, "Incomplete execution review")
        for evidence in self._inputs(context, EvidenceBundle):
            self._verify_evidence(context.task_id, evidence.content)
        self.reviews.bind(context.task_id, specs[0], plans[0])
        return NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=(specs[0].artifact_id, plans[0].artifact_id),
                          decision_summary="Approve TaskSpec, ExecutionPlan and Scope together")

    def review(self, task_id):
        return self.reviews.get(task_id)

    def approve(self, review, *, approved_by="user"):
        self._validate_review_evidence(review.task_id)
        return self.reviews.approve(review, approved_by=approved_by)

    def _validate_review_evidence(self, task_id):
        snapshot = self.runner.store.snapshot(task_id)
        joined = next(n for n in snapshot.nodes if n.node_id == "join_evidence")
        if joined.status is not NodeStatus.SUCCEEDED:
            raise ForgePilotError("invalid_state", "Analyst join is incomplete")
        for artifact_id in joined.result.output_artifact_ids:
            self._verify_evidence(task_id, self.events.artifacts.get(artifact_id).content)
        return joined.result.output_artifact_ids

    def revise(self, review, *, task_spec, plan):
        """Explicit user revision of the complete package, including scope.

        A revised plan must be supplied with its new TaskSpec; model replanning
        can instead start a fresh analysis run. Evidence is rechecked here.
        """
        spec = validate_task_spec(TaskSpec.model_validate(task_spec))
        evidence_ids = self._validate_review_evidence(review.task_id)
        original = self.events.artifacts.get(review.task_spec_artifact_id)
        # A revision cannot silently introduce new data permissions.
        if spec.data_policy != original.content.data_policy or spec.repository != original.content.repository:
            raise ForgePilotError("policy_denied", "Data policy and repository require a new explicit analysis request")
        revised = create_artifact(task_id=review.task_id, producer="orchestrator.v1", producer_node="review_revision",
            parent_artifact_ids=(original.artifact_id,), content=spec)
        plan_data = plan.model_dump(mode="json") if isinstance(plan, ExecutionPlan) else dict(plan)
        plan_data["task_spec_artifact_id"] = revised.artifact_id
        revised_plan = validate_execution_plan(spec, ExecutionPlan.model_validate(plan_data))
        self._validate_plan_paths(spec, revised_plan)
        self._validate_test_policy(spec)
        proposed = create_artifact(task_id=review.task_id, producer="orchestrator.v1", producer_node="review_revision",
            parent_artifact_ids=(revised.artifact_id, *evidence_ids, review.plan_artifact_id), content=revised_plan)
        with self.events.missions.consistent_view():
            if self.reviews.get(review.task_id) != review:
                raise ForgePilotError("approval_stale", "Review changed during revision")
            self.events.artifacts.put(revised)
            self.events.artifacts.put(proposed)
            run = self.events.missions.get_graph_run(review.task_id)
            graph = GraphDefinition.from_dict(json.loads(run["definition_json"]))
            gate = next(n for n in graph.nodes if n.node_id == "execution_approval")
            return self.reviews.replace(review, revised, proposed, gate, self.runner.policy.definition_hash)

    def _validate_plan_paths(self, spec, plan):
        from fnmatch import fnmatchcase
        from ..integrity import is_test_path
        files = {path for step in plan.steps for path in step.expected_files}
        if not spec.test_policy.allow_new_tests and any(step.adds_tests for step in plan.steps):
            raise ValueError("Plan adds tests forbidden by TaskSpec")
        if len(files) > spec.allowed_scope.max_changed_files:
            raise ValueError("Plan exceeds file budget")
        for path in files:
            normalized = self.repository.normalize_plan_path(path)
            if normalized != path:
                raise ValueError("Expected canonical relative file path")
            if (not any(fnmatchcase(path.casefold(), pattern.casefold()) for pattern in spec.allowed_scope.path_patterns)
                    or any(fnmatchcase(path.casefold(), pattern.casefold()) for pattern in spec.forbidden_scope)):
                raise ValueError("Plan file is outside TaskSpec scope")
            if is_test_path(path):
                exists = self.repository._path(path, allow_missing=True).exists()
                if exists and spec.test_policy.existing_tests_read_only:
                    raise ValueError("Existing tests require separate explicit approval")
                if not exists and not spec.test_policy.allow_new_tests:
                    raise ValueError("TaskSpec does not permit new tests")

    @staticmethod
    def _validate_test_policy(spec, *, allow_locked_test_scope=False):
        policy = spec.test_policy
        if policy.existing_tests_read_only and not policy.modifiable_existing_test_patterns:
            return
        if (allow_locked_test_scope and not policy.existing_tests_read_only
                and policy.modifiable_existing_test_patterns
                and all(not any(char in path for char in "*?[") for path in policy.modifiable_existing_test_patterns)):
            # This only admits exact paths into the frozen specification. The
            # writer and validator still need a separate plan-bound test grant.
            return
        if not policy.existing_tests_read_only or policy.modifiable_existing_test_patterns:
            raise ForgePilotError("policy_denied", "Existing tests require independent user approval")


__all__ = ["AnalysisRequest", "AnalysisService"]
