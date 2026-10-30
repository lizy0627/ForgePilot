"""Durable Bug Fix / Feature closure with one shared, three-round repair budget.

The graph is unrolled at creation, so retries, restarts and Reviewer objections
cannot replenish Repair. Models never decide whether validation succeeded.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import replace
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
from .implementation import ImplementationService


MAX_REPAIR_ROUNDS = 3


def validation_passed(report):
    """The sole success predicate used before independent review and delivery."""
    from ..validation import PIPELINE_STAGES
    return (report.status == "passed" and report.failure_classification == "none"
        and report.workspace_integrity == report.test_integrity == report.diff_scope == "passed"
        and report.hidden_oracle == "passed" and report.tests_passed > 0 and report.tests_failed == 0
        and tuple(report.pipeline_stages) == tuple(PIPELINE_STAGES)
        and {item.kind for item in report.commands} >= {"configure", "build", "test", "protected_test"}
        and all(item.exit_code == 0 for item in report.commands))


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
        self.reviewer = ReviewerService(gateway)
        self.repair_definition = self.agents.get("repair.v1")
        gateway.router.validate_agent(self.repair_definition)
        if workspace_manager.artifacts is None:
            workspace_manager.artifacts = events.artifacts
        self.runner.handlers.register("validator.v1", self._validation_handler)
        self.runner.handlers.register("reviewer.v1", self._review_handler)
        self.runner.handlers.register("repair.v1", self._handler)
        self.runner.handlers.register("delivery_bundle.v1", self._delivery_handler)

    def graph(self):
        gate, implementer = super().graph().nodes
        gate = replace(gate, settings={**_plain_json(gate.settings),
            "model_configuration_hash": content_sha256(self.gateway.router.configuration)})
        nodes, edges = [gate, implementer], []
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
        return GraphDefinition("validated-delivery.v1", 1, tuple(nodes), tuple(edges))

    def _analysis_graph(self, task_id, request, *, locked_task_spec=None):
        analysis = self._analysis_for(task_id).graph(request, locked_task_spec=locked_task_spec)
        execution = self.graph()
        gate = replace(execution.nodes[0], dependencies=analysis.nodes[-1].dependencies,
                       settings=_plain_json(execution.nodes[0].settings))
        return GraphDefinition("task-analysis-delivery-locked.v1" if locked_task_spec is not None
                               else "task-analysis-delivery.v1", 1,
            (*analysis.nodes[:-1], gate, *execution.nodes[1:]), execution.edges)

    def _stored_graph(self, task_id):
        graph = GraphDefinition.from_dict(json.loads(self.events.missions.get_graph_run(task_id)["definition_json"]))
        if graph.graph_id == "validated-delivery.v1":
            expected = self.graph()
        elif graph.graph_id in {"task-analysis-delivery.v1", "task-analysis-delivery-locked.v1"}:
            from .analysis import AnalysisRequest
            draft = next(node for node in graph.nodes if node.node_id == "draft_task_spec")
            locked = _plain_json(draft.settings["task_spec"]) if graph.graph_id.endswith("-locked.v1") else None
            expected = self._analysis_graph(task_id,
                AnalysisRequest.model_validate(_plain_json(draft.settings["request"])),
                locked_task_spec=locked)
        else:
            raise ForgePilotError("graph_definition", "Task is not a validated delivery graph")
        if expected.definition_hash != graph.definition_hash:
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

    async def _review_agent(self, context, spec, changes, validation, evidence, budget):
        with self.gateway.accounting.scoped_sink(lambda record: self._model_record(context, record)):
            return await self.reviewer.review(spec, changes, validation, evidence=evidence,
                budget=budget, is_cancelled=lambda: self._cancelled(context))

    def _gate(self, context):
        spec, _, _ = self._package(context.task_id, context.input_artifact_ids)
        try:
            self._await(self.validator.capture(context.task_id, spec.content,
                is_cancelled=lambda: self._cancelled(context)))
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)
        return super()._gate(context)

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
        with self.events._lock:
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

    def _changes_for_validation(self, context):
        writers = [node for node in self.runner.store.snapshot(context.task_id).nodes
                   if node.status is NodeStatus.SUCCEEDED and (node.node_id == "implementer" or node.node_id.startswith("repair_"))]
        artifacts = [self._node_artifact(context.task_id, node.node_id, ChangeSet) for node in writers]
        changes = self.workspace.final_change_set(context.task_id, [item.content for item in artifacts])
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
            changes = self._changes_for_validation(context)
            # Separate named-test approval is checked again at the validator
            # boundary; adding it to TaskSpec alone grants nothing.
            if self._granted(context.task_id, spec, plan, "existing_tests"):
                self.validator.approve_tests(context.task_id, spec.content.test_policy.modifiable_existing_test_patterns)
            report = self._await(self.validator.validate(context.task_id, spec.content, changes.content,
                is_cancelled=lambda: self._cancelled(context)))
            artifact = self._save(context, report, "validator.v1", (spec.artifact_id, changes.artifact_id))
            # Audit attempts count every validation, including infrastructure
            # retries. Repair rounds remain fixed by the graph, independently.
            with self.events._lock:
                attempt = self.events._db.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM validation_runs WHERE task_id=?",
                    (context.task_id,)).fetchone()[0]
                self.events.missions.record_validation(validation_id=str(uuid4()), task_id=context.task_id,
                    attempt=attempt, status=report.status,
                    started_at=report.commands[0].started_at, finished_at=report.commands[-1].finished_at,
                    report_artifact_id=artifact.artifact_id, error_code=report.failure_classification)
            outputs = (artifact.artifact_id,)
            if self._cancelled(context) or report.status == "cancelled":
                return NodeResult(NodeStatus.CANCELLED, output_artifact_ids=outputs)
            if validation_passed(report):
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
        if context.node.agent_id != "repair.v1":
            return {}
        round_number = context.node.settings["repair_round"]
        if not 1 <= round_number <= MAX_REPAIR_ROUNDS:
            raise ForgePilotError("repair_exhausted", "Repair budget exhausted")
        validation = self._node_artifact(context.task_id, f"validation_{round_number - 1}", ValidationReport)
        changes = self._changes_parent(validation)
        report = validation.content
        failures = report.summary
        source = "validation"
        if validation_passed(report):
            review = self._node_artifact(context.task_id, f"reviewer_{round_number - 1}", ReviewReport)
            if review.content.recommend_delivery:
                raise ForgePilotError("repair_policy", "An approved review cannot trigger Repair")
            failures = json.dumps({"summary": review.content.summary,
                "blocking_issues": [item.model_dump(mode="json") for item in review.content.blocking_issues],
                "unmet_criteria": [item.model_dump(mode="json") for item in review.content.acceptance_criteria if item.status != "met"]}, ensure_ascii=False)[:8192]
            source = "reviewer"
        elif report.failure_classification != "code_failure":
            raise ForgePilotError("repair_policy", "Only deterministic code failures can trigger Repair")
        spec, _, _ = self._package(context.task_id, context.input_artifact_ids)
        return {"previous_change_set": changes.content.model_dump(mode="json"),
                "validation_report": report.model_dump(mode="json"),
                "failure_classification": "code_failure", "failure_source": source,
                "failure_summary": failures, "allowed_scope": spec.content.allowed_scope.model_dump(mode="json"),
                "remaining_repair_rounds": MAX_REPAIR_ROUNDS - round_number + 1,
                "repair_round": round_number}

    def _review_handler(self, context):
        spec, _, evidence = self._package(context.task_id, context.input_artifact_ids)
        round_number = context.node.settings["repair_round"]
        validation = self._node_artifact(context.task_id, f"validation_{round_number}", ValidationReport)
        if not validation_passed(validation.content):
            return self._service_error(ForgePilotError("review_validation", "Review requires full validation"))
        changes = self._changes_parent(validation)
        try:
            key = context.task_id, context.node.node_id
            if key not in self._budgets:
                if context.attempt > 1:
                    raise ForgePilotError("llm_budget", "Restart cannot replenish Reviewer budget")
                self._budgets[key] = ModelBudget.from_limits(self.reviewer.definition.limits)
            report = self._await(self._review_agent(context, spec, changes, validation, evidence, self._budgets[key]))
            validate_review_coverage(spec.content, report)
            artifact = self._save(context, report, "reviewer.v1", (spec.artifact_id, changes.artifact_id, validation.artifact_id))
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
        spec, _, _ = self._package(context.task_id, context.input_artifact_ids)
        reviews = [self.events.artifacts.get(item) for item in context.input_artifact_ids
                   if isinstance(self.events.artifacts.get(item).content, ReviewReport)
                   and self.events.artifacts.get(item).content.recommend_delivery]
        if len(reviews) != 1:
            return self._service_error(ForgePilotError("delivery_review", "Delivery requires one approving review"))
        review = reviews[0]
        validations = [self.events.artifacts.get(item) for item in review.parent_artifact_ids
                       if isinstance(self.events.artifacts.get(item).content, ValidationReport)]
        if len(validations) != 1 or not validation_passed(validations[0].content):
            return self._service_error(ForgePilotError("delivery_validation", "Delivery requires complete validation"))
        validation = validations[0]
        changes = self._changes_parent(validation)
        validate_review_coverage(spec.content, review.content)
        try:
            if self._cancelled(context):
                return NodeResult(NodeStatus.CANCELLED)
            current = self.workspace.inspect(self.workspace.record(context.task_id))
            patch = self.workspace.export_validated_patch(context.task_id)
            bundle = DeliveryBundle(change_set_artifact_id=changes.artifact_id,
                validation_report_artifact_id=validation.artifact_id, review_report_artifact_id=review.artifact_id,
                commit=current.head_commit,
                build_test_summary=(f"{validation.content.toolchain_profile}: configure/build passed; "
                    f"CTest {validation.content.tests_passed} passed, {validation.content.tests_failed} failed; "
                    "Protected Oracle, workspace/test integrity and diff scope all passed."),
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
            artifact = self._save(context, bundle, "delivery.v1", (changes.artifact_id, validation.artifact_id, review.artifact_id))
            return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(artifact.artifact_id,),
                              decision_summary="Validated DeliveryBundle and patch ready for final user approval")
        except (FutureCancelledError, asyncio.CancelledError):
            return NodeResult(NodeStatus.CANCELLED)
        except ForgePilotError as exc:
            return self._service_error(exc)


__all__ = ["DeliveryWorkflow", "MAX_REPAIR_ROUNDS", "validation_passed"]
