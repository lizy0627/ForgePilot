"""Durable Bug Fix / Feature closure with one shared, three-round repair budget.

The graph is unrolled at creation, so retries, restarts and Reviewer objections
cannot replenish Repair. Models never decide whether validation succeeded.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
import json
from uuid import uuid4

from ..artifacts.canonical import content_sha256
from ..artifacts.models import (ChangeSet, DeliveryBundle, EvidenceBundle, ExecutionPlan,
                                ReviewReport, TaskSpec, ValidationReport)
from ..artifacts.validation import create_artifact, validate_review_coverage
from ..errors import ForgePilotError
from ..models.accounting import ModelBudget
from ..orchestration.models import (EdgeCondition, EdgeDefinition, FailureKind, GraphDefinition,
                                    NodeDefinition, NodeResult, NodeStatus, NodeType, _plain_json)
from ..repair import MAX_REPAIR_ROUNDS
from ..validation_evidence import validation_passed
from .implementation import ImplementationService
from .workflow_graphs import (
    LEGACY_DELIVERY_GRAPH_IDS, LOCKED_ANALYSIS_DELIVERY_IDS, MULTI_AGENT_DELIVERY_IDS,
    ORDINARY_ANALYSIS_DELIVERY_IDS, SINGLE_AGENT_DELIVERY_IDS, analysis_with_approval, execution_approval,
)


# Each graph version must opt in to validation without a protected Oracle.
_PUBLIC_VALIDATION_GRAPH_IDS = frozenset({
    "task-analysis-delivery.v2", "task-analysis-delivery.v3", "task-analysis-delivery.v4",
})


class DeliveryWorkflow(ImplementationService):
    """Backend entry point: prepare -> frozen approval -> validated DeliveryBundle.

    A successful graph prepares a bundle and patch. WorkspaceManager.deliver still
    owns the separate final approval for squash/keep-branch/export/abandon.
    """

    def __init__(self, events, workspace_manager, gateway, *, validator, **kwargs):
        super().__init__(events, workspace_manager, gateway, **kwargs)
        from .review import ReviewerService
        self.validator = validator
        if validator.workspace is not workspace_manager:
            raise ForgePilotError("workspace_unavailable", "Validator must own the same WorkspaceManager")
        self.reviewer = ReviewerService(gateway, single_agent=self.single_agent)
        self.repair_definition = self.agents.get("generalist.v1" if self.single_agent else "repair.v1")
        gateway.router.validate_agent(self.repair_definition)
        if workspace_manager.artifacts is None:
            workspace_manager.artifacts = events.artifacts
        self.runner.handlers.register("baseline_capture.v1", self._baseline_handler)
        self.runner.handlers.register("validator.v1", self._validation_handler)
        self.runner.handlers.register("reviewer.v1", self._review_handler)
        self.runner.handlers.register("repair.v1", self._handler)
        self.runner.handlers.register("delivery_bundle.v1", self._delivery_handler)

    def graph(self, task_id=None):
        if task_id is None:
            if len(self._mission_tools) > 1:
                raise ForgePilotError("graph_definition", "Select a task to build its delivery graph")
            tools = next(iter(self._mission_tools.values()), None)
        else:
            tools = self.mission_tools_for(task_id)
        return self._execution_graph(tools)

    def _execution_graph(self, tools):
        version = 2
        settings = {"model_configuration_hash": content_sha256(self.gateway.router.configuration)}
        if getattr(self, "mission_source_baseline", None) is not None:
            version = 3
            settings["source_baseline_hash"] = content_sha256(self.mission_source_baseline)
            settings["build_selection_hash"] = content_sha256(self.mission_build_selection)
        if tools is not None:
            version = 4
            settings["tool_selection_hash"] = content_sha256(tools.selection.to_dict())
        baseline = NodeDefinition("baseline_capture", NodeType.DETERMINISTIC, "baseline_capture.v1",
            dependencies=("execution_approval",), settings={"recovery_role": "deterministic"})
        nodes = [execution_approval(**settings), baseline, self._implementer_node("baseline_capture")]
        edges = []
        for round_number in range(MAX_REPAIR_ROUNDS + 1):
            writer = "implementer" if round_number == 0 else f"repair_{round_number}"
            validation, review = f"validation_{round_number}", f"reviewer_{round_number}"
            nodes.append(NodeDefinition(validation, NodeType.DETERMINISTIC, "validator.v1",
                dependencies=("execution_approval", writer), settings={"repair_round": round_number}))
            edges.append(EdgeDefinition(writer, validation))
            nodes.append(NodeDefinition(review, NodeType.AGENT, "reviewer.v1",
                dependencies=("execution_approval", validation), agent_id=self.reviewer.definition.agent_id,
                agent_definition_hash=self.reviewer.definition.definition_hash,
                settings={"repair_round": round_number}))
            edges.append(EdgeDefinition(validation, review, EdgeCondition(route="review")))
            if round_number < MAX_REPAIR_ROUNDS:
                repair = f"repair_{round_number + 1}"
                nodes.append(NodeDefinition(repair, NodeType.AGENT, "repair.v1",
                    dependencies=("execution_approval", validation, review), agent_id=self.repair_definition.agent_id,
                    agent_definition_hash=self.repair_definition.definition_hash,
                    settings={"repair_round": round_number + 1,
                    "limits": self.repair_definition.limits.model_dump(mode="json")}))
                edges.extend((EdgeDefinition(validation, repair, EdgeCondition(route="repair")),
                              EdgeDefinition(review, repair, EdgeCondition(route="repair"))))
        reviews = tuple(f"reviewer_{i}" for i in range(MAX_REPAIR_ROUNDS + 1))
        nodes.append(NodeDefinition("delivery_bundle", NodeType.DELIVERY, "delivery_bundle.v1",
            dependencies=("execution_approval", *reviews), settings={"recovery_role": "deterministic"}))
        edges.extend(EdgeDefinition(review, "delivery_bundle", EdgeCondition(route="deliver")) for review in reviews)
        return GraphDefinition(f"validated-delivery-single.v{version}" if self.single_agent
                               else f"validated-delivery.v{version}", version, tuple(nodes), tuple(edges))

    def _analysis_graph(self, task_id, request, *, locked_task_spec=None,
                        allow_locked_test_scope=False, skills=None):
        if skills is None:
            skills = getattr(self, "mission_skills", ())
        analysis = self._analysis_for(task_id).graph(request, locked_task_spec=locked_task_spec,
            allow_locked_test_scope=allow_locked_test_scope, skills=skills)
        execution = self.graph(task_id)
        gate = execution.node("execution_approval")
        graph_id = f"task-analysis-delivery.v{execution.version}"
        if locked_task_spec is not None:
            graph_id = "task-analysis-delivery-single-locked.v2" if self.single_agent else "task-analysis-delivery-locked.v2"
        return GraphDefinition(graph_id, execution.version, (
            *analysis_with_approval(analysis, gate),
            *(node for node in execution.nodes if node.node_id != gate.node_id),
        ), (*analysis.edges, *execution.edges))

    def _stored_graph(self, task_id):
        run = self.events.missions.get_graph_run(task_id)
        graph = GraphDefinition.from_dict(json.loads(run["definition_json"]))
        if graph.graph_id in LEGACY_DELIVERY_GRAPH_IDS:
            raise ForgePilotError("graph_definition",
                "This task uses the previous delivery graph; recreate the mission and approve its execution plan "
                "(请重新创建任务并审批执行方案)")
        if graph.graph_id in MULTI_AGENT_DELIVERY_IDS | SINGLE_AGENT_DELIVERY_IDS:
            expected = self.graph(task_id)
        elif graph.graph_id in ORDINARY_ANALYSIS_DELIVERY_IDS | LOCKED_ANALYSIS_DELIVERY_IDS:
            from .analysis import AnalysisRequest, stored_graph_skills, validate_skill_routes
            draft = graph.node("draft_task_spec")
            locked = _plain_json(draft.settings["task_spec"]) if graph.graph_id in LOCKED_ANALYSIS_DELIVERY_IDS else None
            request = AnalysisRequest.model_validate(_plain_json(draft.settings["request"]))
            skills = stored_graph_skills(self.events, task_id, graph)
            validate_skill_routes(self, skills, request.data_policy)
            expected = self._analysis_graph(task_id, request, locked_task_spec=locked, skills=skills,
                allow_locked_test_scope=bool(draft.settings.get("allow_locked_test_scope", False)))
        else:
            raise ForgePilotError("graph_definition", "Task is not a validated delivery graph")
        persisted_hash = run["definition_hash"]
        if expected.definition_hash != persisted_hash or graph.definition_hash != persisted_hash:
            raise ForgePilotError("graph_definition", "Frozen delivery graph changed")
        return expected

    def _package(self, task_id, artifact_ids):
        # The Graph carries reports between deterministic nodes; only explicit
        # role payload builders below may expose them to a model.
        artifacts = tuple(self.events.artifacts.get(item) for item in artifact_ids)
        if any(item.task_id != task_id for item in artifacts):
            raise ForgePilotError("artifact_scope", "Delivery inputs belong to another task")
        inputs = tuple(item.artifact_id for item in artifacts if isinstance(
            item.content, (TaskSpec, ExecutionPlan, EvidenceBundle)))
        return super()._package(task_id, inputs)

    def _requires_oracle(self, task_id, spec, plan):
        # Only ordinary HTTP missions may use the public validation path.
        # A frozen criterion or plan that asks for protected tests stays strict.
        if self._stored_graph(task_id).graph_id not in _PUBLIC_VALIDATION_GRAPH_IDS:
            return True
        return (any(item.verification == "protected_test" for item in spec.content.acceptance_criteria)
            or any("protected_test" in step.verification for step in plan.content.steps))

    def _await(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def _model_record(self, context, record):
        # Gateway retries share a logical call_id; each actual provider attempt
        # gets its own durable row, correlated with the frozen graph node.
        self.events.missions.record_model_call(call_id=str(uuid4()), task_id=context.task_id,
            node_id=context.node.node_id, role=record["role"], model=record["model"],
            prompt_version=record["prompt_version"], input_hash=record["input_hash"],
            tool_schema={"capabilities": list(self.agents.get(context.node.agent_id).capabilities)},
            duration_ms=record["duration_ms"], input_tokens=record["input_tokens"],
            output_tokens=record["output_tokens"], total_tokens=record["input_tokens"] + record["output_tokens"],
            retry_count=record["retry_count"], fallback_model=record["model"] if record["fallback_count"] else None,
            response_hash=record["response_hash"], error_code=None if record["status"] == "success" else record["status"])

    async def _agent(self, context):
        with self.gateway.accounting.scoped_sink(lambda record: self._model_record(context, record)):
            return await super()._agent(context)

    async def _review_agent(self, context, spec, changes, validation, evidence, budget, *, require_oracle):
        with self.gateway.accounting.scoped_sink(lambda record: self._model_record(context, record)):
            return await self.reviewer.review(spec, changes, validation, evidence=evidence,
                budget=budget, is_cancelled=lambda: self._cancelled(context),
                require_oracle=require_oracle)

    def _baseline_handler(self, context):
        spec, plan, _ = self._package(context.task_id, context.input_artifact_ids)
        try:
            if not self._granted(context.task_id, spec, plan, "execution"):
                raise ForgePilotError("approval_stale", "Baseline capture requires the frozen execution approval")
            checkpoint = self.runner.recovery.writer_preflight(context.task_id)
            approved_tests = (spec.content.test_policy.modifiable_existing_test_patterns
                if self._granted(context.task_id, spec, plan, "existing_tests") else ())
            self._await(self.validator.capture(context.task_id, spec.content,
                approved_test_files=approved_tests, approval_checkpoint=checkpoint,
                is_cancelled=lambda: self._cancelled(context),
                require_oracle=self._requires_oracle(context.task_id, spec, plan)))
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)
        return NodeResult(NodeStatus.SUCCEEDED, decision_summary="Approved execution baseline captured")

    def recover_storage_failure(self, task_id, *, expected_revision):
        """Publish observed writer bytes after an explicit storage-recovery request.

        This only reopens an unfinished storage-aborted writer. It never marks
        a node successful: the ordinary recovery confirmation must verify the
        ChangeSet and create a checkpoint before validation can run.
        """
        with self.runner._exclusive_control():
            self._stored_graph(task_id)
            with self.events.missions.consistent_view():
                snapshot = self.runner.store.snapshot(task_id)
                if snapshot.revision != expected_revision:
                    raise ForgePilotError("graph_conflict", "Storage recovery revision changed")
                if snapshot.status is not NodeStatus.FAILED:
                    return False
                failure = self.delivery_records.latest_failure(task_id)
                if failure is None or failure.get("error_code") != "graph_storage":
                    return False
                unfinished = [node for node in snapshot.nodes if node.status is NodeStatus.RUNNING]
                if len(unfinished) != 1 or unfinished[0].node_id != "implementer":
                    raise ForgePilotError("storage_recovery_blocked", "Storage recovery requires one unfinished Implementer")
                writer = unfinished[0]
                if writer.result is not None:
                    raise ForgePilotError("storage_recovery_blocked", "Writer already has a published result")
                attempt = self.runner.store.attempt_record(task_id, writer.node_id, writer.attempt)
                if attempt["finished_at"] is not None:
                    raise ForgePilotError("storage_recovery_blocked", "Writer attempt is already finished")
                frozen = self.runner.store.frozen_inputs(task_id, writer.node_id, writer.attempt)
                if frozen is None:
                    raise ForgePilotError("storage_recovery_blocked", "Writer has no frozen inputs")
                self.runner.recovery._verify_pairs(task_id, frozen)
                spec, plan, _ = self._package(task_id, tuple(item[0] for item in frozen))
                if not self._granted(task_id, spec, plan, "execution"):
                    raise ForgePilotError("approval_stale", "Storage recovery requires the original execution approval")
                baseline = self.runner.checkpoints.latest(task_id)
                if baseline is None or baseline.checkpoint_type != "plan_approved":
                    raise ForgePilotError("storage_recovery_blocked", "Writer has no approved baseline checkpoint")
                self.runner.checkpoints.verify(baseline)
                candidates = [item for item in self.events.artifacts.list_for_task(task_id)
                    if isinstance(item.content, ChangeSet) and item.producer_node == writer.node_id]
                if any(item.content.requires_additional_approval for item in candidates):
                    raise ForgePilotError("storage_recovery_blocked", "Partial writer output requires scope review")
                if len(candidates) > 1:
                    raise ForgePilotError("storage_recovery_blocked", "Writer ChangeSet provenance is ambiguous")
            observed = self.workspace.recover_observed_change_set(task_id, spec, plan, baseline)
            if candidates:
                artifact = candidates[0]
                if (artifact.content != observed or artifact.parent_artifact_ids != tuple(item[0] for item in frozen)
                        or artifact.producer != "workspace.v1"):
                    raise ForgePilotError("storage_recovery_blocked", "Stored recovery ChangeSet does not match observed bytes")
            else:
                artifact = create_artifact(task_id=task_id, producer="workspace.v1", producer_node=writer.node_id,
                    parent_artifact_ids=tuple(item[0] for item in frozen), content=observed)
            with self.events.missions.consistent_view():
                current = self.runner.store.snapshot(task_id)
                if current.revision != expected_revision or current.status is not NodeStatus.FAILED:
                    raise ForgePilotError("graph_conflict", "Graph changed during storage inspection")
                if not candidates:
                    self.events.artifacts.put(artifact)
                current = self.runner.store.snapshot(task_id)
                self.runner.store.transition(task_id, writer.node_id, expected_revision=current.revision,
                    source=NodeStatus.RUNNING, target=NodeStatus.INTERRUPTED,
                    graph_status=NodeStatus.INTERRUPTED, event_type="storage_recovery_requested",
                    payload={"reason": "graph_storage", "artifact_id": artifact.artifact_id,
                             "content_sha256": artifact.content_sha256})
                self.delivery_records.request_observed_writer_recovery(task_id,
                    writer.node_id, writer.attempt, artifact.artifact_id)
            return True

    async def resume_infrastructure(self, task_id):
        snapshot = self.runner.store.snapshot(task_id)
        paused = [node for node in snapshot.nodes if node.status is NodeStatus.RETRYABLE]
        if len(paused) != 1 or paused[0].result.failure_kind is not FailureKind.BUILD_CONFIGURATION:
            raise ForgePilotError("invalid_state", "No paused infrastructure validation to retry")
        return await self._run_worker(task_id, self.runner.retry, task_id, self._stored_graph(task_id),
            paused[0].node_id, expected_revision=snapshot.revision)

    @staticmethod
    def _service_error(exc):
        infrastructure = exc.code == "validation_infrastructure"
        return NodeResult(NodeStatus.RETRYABLE if infrastructure else NodeStatus.FAILED,
            failure_kind=FailureKind.BUILD_CONFIGURATION if infrastructure else FailureKind.TOOL_POLICY_VIOLATION,
            error_summary=exc.code, decision_summary=exc.code)

    def _error_result(self, context, code):
        # Denied automatic writes are policy failures; a model cannot route
        # itself through a permission pause to evade the fixed scope.
        return NodeResult(NodeStatus.FAILED, failure_kind=FailureKind.TOOL_POLICY_VIOLATION,
                          error_summary=code, decision_summary=code, metrics=self._metrics(context))

    def _save(self, context, content, producer, parents=None):
        artifact = create_artifact(task_id=context.task_id, producer=producer,
            producer_node=context.node.node_id, content=content,
            parent_artifact_ids=tuple(dict.fromkeys(parents if parents is not None else context.input_artifact_ids)))
        with self.events.missions.consistent_view():
            if isinstance(content, DeliveryBundle) and self._cancelled(context):
                raise FutureCancelledError()
            self.events.artifacts.put(artifact)
        return artifact

    def _node_artifact(self, task_id, node_id, content_type):
        node = next(item for item in self.runner.store.snapshot(task_id).nodes if item.node_id == node_id)
        matches = [self.events.artifacts.get(item) for item in node.result.output_artifact_ids
                   if isinstance(self.events.artifacts.get(item).content, content_type)] if node.result else []
        if len(matches) != 1:
            raise ForgePilotError("artifact_scope", "Expected exactly one result from the selected node")
        return matches[0]

    def _changes_for_validation(self, context, plan):
        writers = [node for node in self.runner.store.snapshot(context.task_id).nodes
                   if node.status is NodeStatus.SUCCEEDED and (node.node_id == "implementer" or node.node_id.startswith("repair_"))]
        artifacts = [self._node_artifact(context.task_id, node.node_id, ChangeSet) for node in writers]
        changes = self.workspace.final_change_set(context.task_id, [item.content for item in artifacts],
                                                   plan_artifact_id=plan.artifact_id)
        return self._save(context, changes, "workspace.v1",
            [changes.execution_plan_artifact_id, *(item.artifact_id for item in artifacts)])

    def _changes_parent(self, validation):
        parents = [self.events.artifacts.get(item) for item in validation.parent_artifact_ids]
        matches = [item for item in parents if isinstance(item.content, ChangeSet) and item.producer == "workspace.v1"]
        if len(matches) != 1:
            raise ForgePilotError("artifact_scope", "Validation must bind one observed final ChangeSet")
        return matches[0]

    def _validation_handler(self, context):
        spec, plan, _ = self._package(context.task_id, context.input_artifact_ids)
        try:
            if not self._granted(context.task_id, spec, plan, "execution"):
                raise ForgePilotError("approval_stale", "Validation requires the current approved execution plan")
            changes = self._changes_for_validation(context, plan)
            # Separate named-test approval is checked again at the validator
            # boundary; adding it to TaskSpec alone grants nothing.
            if self._granted(context.task_id, spec, plan, "existing_tests"):
                self.validator.approve_tests(context.task_id, spec.content.test_policy.modifiable_existing_test_patterns)
            # A scope revision changes the diff policy. Test bytes, inventory and
            # the integrity manifest still belong to the original capture attempt.
            capture = next(node for node in self.runner.store.snapshot(context.task_id).nodes
                           if node.node_id == "baseline_capture")
            captured = self.runner.store.frozen_inputs(context.task_id, capture.node_id, capture.attempt)
            if captured is None:
                raise ForgePilotError("validation_policy", "Baseline capture inputs are missing")
            baseline_spec, _, _ = self._package(context.task_id, tuple(item[0] for item in captured))
            report = self._await(self.validator.validate(context.task_id, spec.content, changes.content,
                baseline_spec=baseline_spec.content, is_cancelled=lambda: self._cancelled(context)))
            artifact = self._save(context, report, "validator.v1", (spec.artifact_id, changes.artifact_id))
            # Audit attempts count every validation, including infrastructure
            # retries. Repair rounds remain fixed by the graph, independently.
            self.delivery_records.record_next_validation(validation_id=str(uuid4()), task_id=context.task_id,
                status=report.status,
                started_at=report.commands[0].started_at, finished_at=report.commands[-1].finished_at,
                report_artifact_id=artifact.artifact_id, error_code=report.failure_classification)
            outputs = (artifact.artifact_id,)
            if self._cancelled(context) or report.status == "cancelled":
                return NodeResult(NodeStatus.CANCELLED, output_artifact_ids=outputs)
            if validation_passed(report, require_oracle=self._requires_oracle(context.task_id, spec, plan)):
                return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=outputs, next_route="review",
                                  decision_summary="Full deterministic validation passed")
            if report.failure_classification == "infrastructure_failure":
                return NodeResult(NodeStatus.RETRYABLE, output_artifact_ids=outputs,
                    failure_kind=FailureKind.BUILD_CONFIGURATION, error_summary="infrastructure_failure",
                    decision_summary="Infrastructure paused; Repair budget unchanged")
            if report.failure_classification == "code_failure" and context.node.settings["repair_round"] < MAX_REPAIR_ROUNDS:
                return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=outputs, next_route="repair",
                                  decision_summary="Code failure requires controlled Repair")
            return NodeResult(NodeStatus.FAILED, output_artifact_ids=outputs,
                failure_kind=FailureKind.BUILD_CODE_ERROR if report.failure_classification == "code_failure" else FailureKind.TOOL_POLICY_VIOLATION,
                error_summary="repair_exhausted" if report.failure_classification == "code_failure" else "policy_failure")
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)

    def _extra_agent_payload(self, context):
        if not context.node.node_id.startswith("repair_"):
            return {}
        round_number = context.node.settings["repair_round"]
        if not 1 <= round_number <= MAX_REPAIR_ROUNDS:
            raise ForgePilotError("repair_exhausted", "Repair budget exhausted")
        validation = self._node_artifact(context.task_id, f"validation_{round_number - 1}", ValidationReport)
        changes = self._changes_parent(validation)
        report = validation.content
        failures = report.summary
        source = "validation"
        spec, plan, _ = self._package(context.task_id, context.input_artifact_ids)
        if validation_passed(report, require_oracle=self._requires_oracle(context.task_id, spec, plan)):
            review = self._node_artifact(context.task_id, f"reviewer_{round_number - 1}", ReviewReport)
            if review.content.recommend_delivery:
                raise ForgePilotError("repair_policy", "An approved review cannot trigger Repair")
            failures = json.dumps({"summary": review.content.summary,
                "blocking_issues": [item.model_dump(mode="json") for item in review.content.blocking_issues],
                "unmet_criteria": [item.model_dump(mode="json") for item in review.content.acceptance_criteria if item.status != "met"]}, ensure_ascii=False)[:8192]
            source = "reviewer"
        elif report.failure_classification != "code_failure":
            raise ForgePilotError("repair_policy", "Only deterministic code failures can trigger Repair")
        return {"previous_change_set": changes.content.model_dump(mode="json"),
                "validation_report": report.model_dump(mode="json"),
                "failure_classification": "code_failure", "failure_source": source,
                "failure_summary": failures, "allowed_scope": spec.content.allowed_scope.model_dump(mode="json"),
                "remaining_repair_rounds": MAX_REPAIR_ROUNDS - round_number + 1,
                "repair_round": round_number}

    def _review_handler(self, context):
        spec, plan, evidence = self._package(context.task_id, context.input_artifact_ids)
        round_number = context.node.settings["repair_round"]
        validation = self._node_artifact(context.task_id, f"validation_{round_number}", ValidationReport)
        if not validation_passed(validation.content, require_oracle=self._requires_oracle(context.task_id, spec, plan)):
            return self._service_error(ForgePilotError("review_validation", "Review requires full validation"))
        changes = self._changes_parent(validation)
        if changes.content.execution_plan_artifact_id != plan.artifact_id:
            return self._service_error(ForgePilotError("review_input", "Validated changes reference another plan"))
        try:
            key = context.task_id, context.node.node_id
            if key not in self._budgets:
                if context.attempt > 1:
                    raise ForgePilotError("llm_budget", "Restart cannot replenish Reviewer budget")
                self._budgets[key] = ModelBudget.from_limits(self.reviewer.definition.limits)
            report = self._await(self._review_agent(context, spec, changes, validation, evidence,
                self._budgets[key], require_oracle=self._requires_oracle(context.task_id, spec, plan)))
            validate_review_coverage(spec.content, report)
            artifact = self._save(context, report, self.reviewer.definition.agent_id,
                                  (spec.artifact_id, plan.artifact_id, changes.artifact_id, validation.artifact_id))
            if self._cancelled(context):
                return NodeResult(NodeStatus.CANCELLED, output_artifact_ids=(artifact.artifact_id,))
            approved = report.verdict == "approve" and report.recommend_delivery and not report.blocking_issues and all(
                item.status == "met" for item in report.acceptance_criteria)
            if approved or round_number < MAX_REPAIR_ROUNDS:
                return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                    next_route="deliver" if approved else "repair", metrics=self._metrics(context),
                    decision_summary="Independent review approved delivery" if approved else "Reviewer requires controlled Repair")
            return NodeResult(NodeStatus.FAILED, output_artifact_ids=(artifact.artifact_id,),
                failure_kind=FailureKind.BUILD_CODE_ERROR, error_summary="repair_exhausted", metrics=self._metrics(context))
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)
        except (ValueError, TypeError):
            return self._service_error(ForgePilotError("review_contract", "Invalid independent review"))

    def _delivery_handler(self, context):
        spec, plan, _ = self._package(context.task_id, context.input_artifact_ids)
        reviews = [self.events.artifacts.get(item) for item in context.input_artifact_ids
                   if isinstance(self.events.artifacts.get(item).content, ReviewReport)
                   and self.events.artifacts.get(item).content.recommend_delivery]
        if len(reviews) != 1:
            return self._service_error(ForgePilotError("delivery_review", "Delivery requires one approving review"))
        review = reviews[0]
        validations = [self.events.artifacts.get(item) for item in review.parent_artifact_ids
                       if isinstance(self.events.artifacts.get(item).content, ValidationReport)]
        if len(validations) != 1 or not validation_passed(validations[0].content,
                require_oracle=self._requires_oracle(context.task_id, spec, plan)):
            return self._service_error(ForgePilotError("delivery_validation", "Delivery requires complete validation"))
        validation = validations[0]
        changes = self._changes_parent(validation)
        validate_review_coverage(spec.content, review.content)
        try:
            if changes.content.execution_plan_artifact_id != plan.artifact_id:
                raise ForgePilotError("delivery_changes", "Reviewed changes reference another plan")
            if self._cancelled(context):
                return NodeResult(NodeStatus.CANCELLED)
            current = self.workspace.inspect(self.workspace.record(context.task_id))
            patch = self.workspace.export_validated_patch(context.task_id)
            bundle = DeliveryBundle(change_set_artifact_id=changes.artifact_id,
                validation_report_artifact_id=validation.artifact_id, review_report_artifact_id=review.artifact_id,
                commit=current.head_commit,
                build_test_summary=(f"{validation.content.toolchain_profile}: configure/build passed; "
                    f"CTest {validation.content.tests_passed} passed, {validation.content.tests_failed} failed; "
                    + ("Protected Oracle passed; " if validation.content.hidden_oracle == "passed" else
                       "Protected Oracle not run; ")
                    + "workspace/test integrity and diff scope passed."),
                reviewer_conclusion=review.content.summary, changed_files=tuple(item.path for item in changes.content.files),
                usage_instructions=("Review the attached patch and reports, then choose squash, keep_branch or patch export using final delivery approval. "
                    f"Configure, build and run CTest with ToolchainProfile {validation.content.toolchain_profile}.\n"
                    f"Expected behavior: {spec.content.problem.expected_behavior[:4000]}"),
                risks=tuple(dict.fromkeys((
                    "Validation executes approved build/test code as the host user; process ownership is not a filesystem sandbox.",
                    *spec.content.risks, *review.content.non_blocking_suggestions)))[:128],
                rollback="Before merging, abandon the isolated task worktree. After squash delivery, revert the recorded delivery commit on the target branch.",
                patch_path=patch["path"])
            self.workspace.prepare_delivery_bundle(context.task_id, bundle)
            artifact = self._save(context, bundle, "delivery.v1",
                                  (plan.artifact_id, changes.artifact_id, validation.artifact_id, review.artifact_id))
            return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                              decision_summary="Validated DeliveryBundle and patch ready for final user approval")
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)


__all__ = ["DeliveryWorkflow", "MAX_REPAIR_ROUNDS", "validation_passed"]
