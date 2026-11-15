"""Bounded Analyst fan-out with durable-before-visible state publication."""
from __future__ import annotations

from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from time import monotonic
from threading import Barrier, BrokenBarrierError
from typing import Callable, Iterable

from ..artifacts.store import ArtifactNotFoundError
from ..artifacts.sqlite_store import ArtifactStorageError
from ..errors import ForgePilotError
from .graph import ValidatedGraph, validate_graph
from .models import (
    FailureKind, GraphDefinition, GraphEvent, GraphSnapshot, NodeMetrics, NodeResult,
    NodeState, NodeStatus, NodeType, PolicyAction, idempotency_key,
)
from .node_handlers import HandlerRegistry, NodeContext, NodeExecutionError
from .policies import DefaultFailurePolicy, FailurePolicy
from .store import GraphStore
from .recovery import RecoveryManager, recovery_role
from .workspace import WorkspaceSnapshot


class _WriterNotCompleted(Exception):
    """Exit the lease through its interrupted-writer path without hiding result."""


class GraphRunner:
    """Runs two trusted Analysts concurrently; every other node is exclusive."""

    def __init__(self, event_store, handlers: HandlerRegistry, *,
                 artifact_store=None, policy: FailurePolicy | None = None,
                 workspace_backend=None) -> None:
        self.store = GraphStore(event_store)
        self.handlers = handlers
        self.artifacts = artifact_store or event_store.artifacts
        self.policy = policy or DefaultFailurePolicy()
        self.recovery = RecoveryManager(event_store, artifacts=self.artifacts,
                                        workspace_backend=workspace_backend or getattr(event_store, "workspace_backend", None),
                                        policy_hash=self.policy.definition_hash)
        self.checkpoints = self.recovery.checkpoints

    def run(self, task_id: str, graph: GraphDefinition, *,
            input_artifact_ids: Iterable[str] = ()) -> GraphSnapshot:
        validated = validate_graph(graph)
        requested_inputs = tuple(input_artifact_ids)
        requested_hashes = self._validate_artifacts(task_id, requested_inputs)
        try:
            snapshot = self.store.snapshot(task_id)
        except ForgePilotError as exc:
            if exc.code != "unknown_graph":
                raise
            snapshot = self.store.create_run(
                task_id, validated, inputs=tuple(zip(requested_inputs, requested_hashes)),
                policy_hash=self.policy.definition_hash)
        self._assert_definition(snapshot, validated)
        stored_inputs = self.store.initial_inputs(task_id)
        if requested_inputs and stored_inputs != tuple(zip(requested_inputs, requested_hashes)):
            raise ForgePilotError("graph_inputs_changed", "Persisted graph inputs do not match")
        initial_inputs = tuple(artifact_id for artifact_id, _ in stored_inputs)
        actual_hashes = self._validate_artifacts(task_id, initial_inputs)
        if actual_hashes != tuple(value for _, value in stored_inputs):
            raise ForgePilotError("artifact_integrity", "Persisted graph input Artifact changed")

        while True:
            snapshot = self.store.snapshot(task_id)
            if snapshot.status in {
                    NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
                return snapshot
            if self.store.recovery_required(task_id) or any(
                    node.status in {NodeStatus.INTERRUPTED, NodeStatus.RUNNING} for node in snapshot.nodes):
                return snapshot
            by_id = {node.node_id: node for node in snapshot.nodes}
            if any(node.status is NodeStatus.FAILED for node in snapshot.nodes):
                return self.store.finish_graph(
                    task_id, expected_revision=snapshot.revision, status=NodeStatus.FAILED)
            if any(node.status is NodeStatus.CANCELLED for node in snapshot.nodes):
                return self.store.cancel(task_id, expected_revision=snapshot.revision)

            progressed = False
            for node_id in validated.topological_order:
                state = by_id[node_id]
                if state.status is not NodeStatus.PENDING:
                    continue
                readiness = self._readiness(validated, node_id, by_id)
                if readiness is None:
                    continue
                target = NodeStatus.READY if readiness else NodeStatus.SKIPPED
                snapshot = self.store.transition(
                    task_id, node_id, expected_revision=snapshot.revision,
                    source=NodeStatus.PENDING, target=target,
                    event_type="node_ready" if readiness else "node_skipped",
                    payload={"reason": "dependencies_satisfied" if readiness
                             else "edge_conditions_not_matched"},
                )
                by_id = {node.node_id: node for node in snapshot.nodes}
                progressed = True

            ready_nodes = [node_id for node_id in validated.topological_order
                           if by_id[node_id].status is NodeStatus.READY
                           and self._readiness(validated, node_id, by_id) is True]
            if ready_nodes:
                ready = ready_nodes[0]
                batch = [ready]
                fanout = validated.analyst_fanout(ready)
                if fanout is not None:
                    # A group contains at most one of each built-in Analyst.
                    sibling = next((item for item in ready_nodes[1:]
                                    if validated.analyst_fanout(item) == fanout
                                    and validated.nodes[item].agent_id != validated.nodes[ready].agent_id), None)
                    if sibling is not None:
                        batch.append(sibling)
                existing = self.store.begin_execution(task_id)
                if existing is not None:
                    return existing
                acquired = False
                try:
                    while not acquired:
                        if self.store.is_cancelled(task_id, ready):
                            return self.store.snapshot(task_id)
                        acquired = self.store.acquire_execution_slot(timeout=0.05)
                    if len(batch) == 1:
                        self._execute(task_id, validated, by_id[ready], initial_inputs,
                                      expected_revision=snapshot.revision)
                    else:
                        # Join the entire batch even when one worker raises.
                        boundary = Barrier(len(batch))
                        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="forgepilot-analyst") as pool:
                            futures = [pool.submit(self._execute_parallel, task_id, validated,
                                                   by_id[item], initial_inputs, boundary) for item in batch]
                            for future in futures:
                                future.result()
                finally:
                    if acquired:
                        self.store.release_execution_slot()
                    self.store.end_execution(task_id)
                continue

            snapshot = self.store.snapshot(task_id)
            if all(node.status in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED}
                   for node in snapshot.nodes):
                return self.store.finish_graph(
                    task_id, expected_revision=snapshot.revision, status=NodeStatus.SUCCEEDED)
            if any(node.status in {NodeStatus.AWAITING_HUMAN, NodeStatus.RETRYABLE,
                                   NodeStatus.INTERRUPTED, NodeStatus.RUNNING}
                   for node in snapshot.nodes):
                return snapshot
            if not progressed:
                raise ForgePilotError("graph_stalled", "Graph has no runnable or terminal node")

    def approve_human(self, task_id: str, graph: GraphDefinition, node_id: str, *,
                      expected_revision: int, decision_summary: str = "Approved",
                      approval=None, on_approved=None) -> GraphSnapshot:
        with self._exclusive_control():
            validated = validate_graph(graph)
            snapshot = self.store.snapshot(task_id)
            self._assert_definition(snapshot, validated)
            if self.store.recovery_required(task_id):
                raise ForgePilotError("recovery_confirmation_required", "Inspect and confirm recovery before approval")
            if snapshot.revision != expected_revision:
                raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
            current = self._node(snapshot, node_id)
            if current.status is not NodeStatus.AWAITING_HUMAN or current.result is None:
                raise ForgePilotError("invalid_state", "Node is not awaiting a human decision")
            result = NodeResult(
                status=NodeStatus.SUCCEEDED,
                output_artifact_ids=current.result.output_artifact_ids,
                decision_summary=decision_summary,
                next_route=current.result.next_route,
                metrics=current.result.metrics,
            )
            with self.store.publication_boundary():
                if self.store.snapshot(task_id).revision != expected_revision:
                    raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
                cp = self.recovery.record_completed(task_id, validated.nodes[node_id], current.attempt,
                                                     result, approved=True)
                self.store.approve(task_id, node_id, expected_revision=expected_revision, result=result,
                                   checkpoint_id=cp.checkpoint_id if cp else None, approval=approval)
        if on_approved is not None:
            on_approved()
        # run() takes the same non-reentrant execution lock for its next batch.
        return self.run(task_id, graph)

    def retry(self, task_id: str, graph: GraphDefinition, node_id: str, *,
              expected_revision: int, input_artifact_ids: Iterable[str] = ()) -> GraphSnapshot:
        validated = validate_graph(graph)
        snapshot = self.store.snapshot(task_id)
        self._assert_definition(snapshot, validated)
        current = self._node(snapshot, node_id)
        if (self.store.recovery_required(task_id) or current.status in {NodeStatus.INTERRUPTED, NodeStatus.RUNNING}
                or recovery_role(validated.nodes[node_id]) in {"writer", "delivery"}):
            raise ForgePilotError("recovery_confirmation_required", "Inspect and confirm the recovery plan before retrying")
        if current.status is not NodeStatus.RETRYABLE:
            raise ForgePilotError("invalid_state", "Only retryable or interrupted nodes can retry")
        self.store.transition(
            task_id, node_id, expected_revision=expected_revision,
            source=NodeStatus.RETRYABLE, target=NodeStatus.READY,
            event_type="node_retry_started", payload={"reason": "explicit_retry"},
        )
        return self.run(task_id, graph, input_artifact_ids=input_artifact_ids)

    def cancel(self, task_id: str, *, expected_revision: int) -> GraphSnapshot:
        return self.store.cancel(task_id, expected_revision=expected_revision)

    def prepare_recovery(self, task_id: str, graph: GraphDefinition):
        with self._exclusive_control():
            validated = validate_graph(graph)
            self._assert_definition(self.store.snapshot(task_id), validated)
            return self.recovery.prepare(task_id, graph)

    def confirm_recovery(self, task_id: str, graph: GraphDefinition, *, token: str,
                         expected_revision: int) -> GraphSnapshot:
        with self._exclusive_control():
            validated = validate_graph(graph)
            self._assert_definition(self.store.snapshot(task_id), validated)
            return self.recovery.confirm(task_id, graph, token=token, expected_revision=expected_revision)

    @contextmanager
    def _exclusive_control(self):
        # Approval can create a Git checkpoint; recovery can inspect Git,
        # restore files, or finish a commit. Acquire execution before SQLite,
        # matching run(), and never block an event loop needed by live Analysts.
        if not self.store.acquire_execution_slot(blocking=False):
            raise ForgePilotError("graph_active", "A live node batch must stop before approval or recovery")
        try:
            yield
        finally:
            self.store.release_execution_slot()

    def events(self, task_id: str, *, after_sequence: int = 0) -> tuple[GraphEvent, ...]:
        return self.store.graph_events(task_id, after_sequence=after_sequence)

    @staticmethod
    def _node(snapshot: GraphSnapshot, node_id: str) -> NodeState:
        try:
            return next(node for node in snapshot.nodes if node.node_id == node_id)
        except StopIteration:
            raise ForgePilotError("unknown_node", "Graph node does not exist") from None

    def _assert_definition(self, snapshot: GraphSnapshot, graph: ValidatedGraph) -> None:
        if (snapshot.graph_id != graph.definition.graph_id
                or snapshot.graph_version != graph.definition.version
                or snapshot.definition_hash != graph.definition.definition_hash
                or {node.node_id: node.node_type for node in snapshot.nodes}
                != {node.node_id: node.node_type for node in graph.definition.nodes}):
            raise ForgePilotError("graph_definition_changed", "Persisted graph definition does not match")
        persisted = self.store.run_record(snapshot.task_id)
        if persisted["policy_hash"] is not None and persisted["policy_hash"] != self.policy.definition_hash:
            raise ForgePilotError("graph_policy_changed", "Persisted graph policy does not match")

    @staticmethod
    def _readiness(graph: ValidatedGraph, node_id: str,
                   states: dict[str, NodeState]) -> bool | None:
        definition = graph.nodes[node_id]
        dependencies = [states[item] for item in definition.dependencies]
        if any(item.status not in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED}
               for item in dependencies):
            return None
        incoming = graph.incoming_edges(node_id)
        if not incoming:
            return True
        sources = [states[edge.source] for edge in incoming]
        if any(source.status not in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED}
               for source in sources):
            return None
        if definition.node_type is NodeType.JOIN:
            # The claim executes an all-success check, including skipped and
            # non-matching branches. Never silently drop one side of a join.
            return True
        return any(edge.condition.matches(states[edge.source].result,
                                          states[edge.source].status)
                   for edge in incoming)

    def _inputs(self, graph: ValidatedGraph, node_id: str,
                states: dict[str, NodeState], initial: tuple[str, ...]) -> tuple[str, ...]:
        result = list(initial)
        for predecessor in graph.predecessors(node_id):
            prior = states[predecessor].result
            if prior:
                result.extend(prior.output_artifact_ids)
        return tuple(dict.fromkeys(result))

    def _validate_artifacts(self, task_id: str, artifact_ids: tuple[str, ...]) -> tuple[str, ...]:
        hashes = []
        for artifact_id in artifact_ids:
            artifact = self.artifacts.get(artifact_id)
            if artifact.task_id != task_id:
                raise ForgePilotError("artifact_scope", "Input Artifact belongs to another task")
            hashes.append(artifact.content_sha256)
        return tuple(hashes)

    def _execute(self, task_id: str, graph: ValidatedGraph, state: NodeState,
                 initial_inputs: tuple[str, ...], *, expected_revision: int | None) -> GraphSnapshot:
        writer = getattr(self.recovery.workspace, "writer", None)
        if writer is None or recovery_role(graph.nodes[state.node_id]) != "writer":
            return self._execute_owned(task_id, graph, state, initial_inputs,
                                       expected_revision=expected_revision)
        baseline = self.recovery.writer_preflight(task_id)
        expected = WorkspaceSnapshot(baseline.workspace_id, task_id, baseline.head_commit, baseline.file_hashes)
        result = None
        try:
            # Ownership spans the handler, ChangeSet verification, Git checkpoint,
            # and durable graph publication, including other tasks of this repo.
            with writer(task_id, expected=expected):
                result = self._execute_owned(task_id, graph, state, initial_inputs,
                                             expected_revision=expected_revision)
                if self._node(result, state.node_id).status is not NodeStatus.SUCCEEDED:
                    raise _WriterNotCompleted()
        except _WriterNotCompleted:
            pass
        return result

    def _execute_parallel(self, task_id: str, graph: ValidatedGraph, state: NodeState,
                          initial_inputs: tuple[str, ...], boundary: Barrier) -> GraphSnapshot:
        arrived = False

        def handlers_completed():
            nonlocal arrived
            arrived = True
            boundary.wait()

        try:
            # Eligibility already guarantees an Analyst, never a writer. The
            # barrier precedes publication because recovery checkpoints may
            # touch Git even for a read-only Artifact boundary.
            return self._execute_owned(task_id, graph, state, initial_inputs,
                                       expected_revision=None, before_publish=handlers_completed)
        except BrokenBarrierError:
            if self.store.is_cancelled(task_id, state.node_id):
                return self.store.snapshot(task_id)
            raise ForgePilotError("graph_interrupted", "Parallel branch stopped before publication") from None
        finally:
            if not arrived:
                boundary.abort()

    def _execute_owned(self, task_id: str, graph: ValidatedGraph, state: NodeState,
                       initial_inputs: tuple[str, ...], *, expected_revision: int | None,
                       before_publish: Callable[[], None] | None = None) -> GraphSnapshot:
        definition = graph.nodes[state.node_id]
        states = {node.node_id: node for node in self.store.snapshot(task_id).nodes}
        artifact_ids = self._inputs(graph, state.node_id, states, initial_inputs)
        frozen = self.store.frozen_inputs(task_id, state.node_id, state.attempt)
        if frozen is not None:
            artifact_ids = tuple(artifact_id for artifact_id, _ in frozen)
        hashes = self._validate_artifacts(task_id, artifact_ids)
        if frozen is not None and hashes != tuple(value for _, value in frozen):
            raise ForgePilotError("artifact_integrity", "Frozen retry inputs changed")
        baseline = None
        if recovery_role(definition) == "writer":
            baseline = self.recovery.writer_preflight(task_id)
        attempt = state.attempt + 1
        key = idempotency_key(
            task_id=task_id, node_id=state.node_id, attempt=attempt,
            input_artifact_hashes=hashes,
            agent_definition_hash=definition.effective_agent_definition_hash,
            policy_hash=self.policy.definition_hash,
        )
        input_hash = sha256(json.dumps(list(zip(artifact_ids, hashes)), separators=(",", ":")).encode()).hexdigest()
        # A successful key is authoritative. In normal flow a succeeded state is
        # observed before reaching here; the lookup also guards duplicate callers.
        reused = self.store.successful_result(key)
        if reused is not None:
            raise ForgePilotError(
                "graph_conflict", "Successful idempotency key conflicts with node projection")
        with self.store.publication_boundary():
            current = self.store.snapshot(task_id)
            if current.status is NodeStatus.CANCELLED:
                return current
            self.store.transition(
                task_id, state.node_id,
                expected_revision=current.revision if expected_revision is None else expected_revision,
                source=NodeStatus.READY, target=NodeStatus.RUNNING,
                event_type="node_started", payload={"idempotency_key": key,
                    "input_artifact_ids": list(artifact_ids),
                    "agent_definition_hash": definition.effective_agent_definition_hash},
                attempt=attempt, idempotency_key=key, input_hash=input_hash,
                agent_id=definition.agent_id,
                input_artifacts=tuple(zip(artifact_ids, hashes)),
            )
        context = NodeContext(
            task_id=task_id, node=definition, attempt=attempt,
            input_artifact_ids=artifact_ids, input_artifact_hashes=hashes,
            artifacts=self.artifacts,
            is_cancelled=lambda: self.store.is_cancelled(task_id, state.node_id),
        )
        started = monotonic()
        try:
            if definition.node_type is NodeType.JOIN and (
                    any(states[item].status is not NodeStatus.SUCCEEDED
                        for item in graph.predecessors(state.node_id))
                    or any(not edge.condition.matches(states[edge.source].result,
                                                       states[edge.source].status)
                           for edge in graph.incoming_edges(state.node_id))):
                raise NodeExecutionError(FailureKind.MODEL_CONTRACT_VIOLATION,
                                         "Join requires every input branch to succeed")
            result = self.handlers.resolve(definition)(context)
            if not isinstance(result, NodeResult):
                raise NodeExecutionError(
                    FailureKind.MODEL_CONTRACT_VIOLATION,
                    "Node handler returned an invalid result",
                )
        except NodeExecutionError as exc:
            result = NodeResult(
                status=NodeStatus.FAILED, failure_kind=exc.failure_kind,
                error_summary=exc.summary,
            )
        except ArtifactStorageError:
            # Keep the claimed attempt unfinished for inspection. A storage
            # failure cannot be downgraded into a model/tool retry.
            raise ForgePilotError("graph_storage", "Artifact persistence failed during node execution") from None
        except Exception:
            result = NodeResult(
                status=NodeStatus.FAILED, failure_kind=FailureKind.TOOL_TRANSIENT,
                error_summary="Node handler raised an unexpected exception",
            )
        elapsed = max(0, int((monotonic() - started) * 1000))
        result = result.with_duration(max(elapsed, result.metrics.duration_ms))
        if before_publish is not None:
            before_publish()

        current = self.store.snapshot(task_id)
        persisted = self._node(current, state.node_id)
        if persisted.status is NodeStatus.CANCELLED:
            return current
        try:
            self._validate_artifacts(task_id, result.output_artifact_ids)
        except (ArtifactNotFoundError, ForgePilotError):
            result = NodeResult(
                status=NodeStatus.FAILED,
                decision_summary=result.decision_summary,
                failure_kind=FailureKind.MODEL_CONTRACT_VIOLATION,
                error_summary="Node result references an unavailable or out-of-scope Artifact",
                metrics=result.metrics,
            )

        policy_action = None
        target = result.status
        if result.status in {NodeStatus.FAILED, NodeStatus.RETRYABLE}:
            policy_action = self.policy.action_for(result.failure_kind, attempt=attempt)
            target = (NodeStatus.RETRYABLE if policy_action in {
                PolicyAction.RETRY, PolicyAction.PAUSE} else NodeStatus.FAILED)
            if result.status is not target or result.retryable != (target is NodeStatus.RETRYABLE):
                result = NodeResult(
                    status=target, output_artifact_ids=result.output_artifact_ids,
                    decision_summary=result.decision_summary, next_route=result.next_route,
                    retryable=target is NodeStatus.RETRYABLE, metrics=result.metrics,
                    failure_kind=result.failure_kind, error_summary=result.error_summary,
                )
        graph_status = (NodeStatus.AWAITING_HUMAN if target is NodeStatus.AWAITING_HUMAN
                        else NodeStatus.RETRYABLE if target is NodeStatus.RETRYABLE
                        else NodeStatus.FAILED if target is NodeStatus.FAILED
                        else NodeStatus.INTERRUPTED if target is NodeStatus.INTERRUPTED
                        else NodeStatus.RUNNING)
        payload = {
            "idempotency_key": key,
            "policy_action": policy_action.value if policy_action else None,
            "failure_kind": result.failure_kind.value if result.failure_kind else None,
            "next_route": result.next_route,
            "metrics": result.to_dict()["metrics"],
            "output_artifact_ids": list(result.output_artifact_ids),
        }
        with self.store.publication_boundary():
            current = self.store.snapshot(task_id)
            persisted = self._node(current, state.node_id)
            if persisted.status is NodeStatus.CANCELLED:
                return current
            if (persisted.status is not NodeStatus.RUNNING or persisted.attempt != attempt
                    or self.store.recovery_required(task_id)):
                raise ForgePilotError("graph_conflict", "Node changed before checkpoint publication")
            # Concurrent successful siblings cannot erase a failure or pause.
            projected = {node.status for node in current.nodes if node.node_id != state.node_id} | {target}
            for status in (NodeStatus.FAILED, NodeStatus.INTERRUPTED,
                           NodeStatus.RETRYABLE, NodeStatus.AWAITING_HUMAN):
                if status in projected:
                    graph_status = status
                    break
            cp = self.recovery.record_completed(task_id, definition, attempt, result, baseline=baseline)
            return self.store.transition(
                task_id, state.node_id, expected_revision=current.revision,
                source=NodeStatus.RUNNING, target=target,
                event_type="node_finished", payload=payload, graph_status=graph_status,
                attempt=attempt, result=result, policy_action=policy_action,
                checkpoint_id=cp.checkpoint_id if cp else None,
            )


__all__ = ["GraphRunner"]
