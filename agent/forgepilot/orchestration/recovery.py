"""Verified node boundaries and explicit, revision-bound restart decisions.

This module never restores a model stream or process. Worktree operations are
injected through a strict contract; the default backend cannot touch a repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Mapping

from ..artifacts.models import ChangeSet, ExecutionPlan
from ..errors import ForgePilotError
from .checkpoint import CheckpointStore
from .execution import ExecutionCoordinator, coordinator_for_storage
from .models import GraphDefinition, NodeDefinition, NodeResult, NodeStatus, NodeType, idempotency_key
from .store import GraphStore
from .workspace import UnavailableWorkspaceBackend, WorkspaceSnapshot


CHECKPOINT_TYPES = frozenset({
    "task_spec", "evidence", "plan_approved", "implementation", "repair", "validated", "reviewed",
})


def recovery_role(node: NodeDefinition) -> str:
    role = node.settings.get("recovery_role")
    if role is not None:
        if role not in {"analyst", "orchestrator", "writer", "validator", "reviewer", "delivery", "deterministic"}:
            raise ForgePilotError("recovery_policy", "Unknown node recovery role")
        return role
    if node.node_type is NodeType.DELIVERY:
        return "delivery"
    name = (node.agent_id or node.handler).lower()
    if ("implementer" in name or "repair" in name
            or (name == "generalist.v1" and (node.node_id == "implementer" or node.handler == "repair.v1"))):
        return "writer"
    for role in ("analyst", "orchestrator", "validator", "reviewer"):
        if role in name:
            return role
    return "deterministic"


def checkpoint_type(node, artifacts, *, approved=False):
    explicit = node.settings.get("checkpoint_type")
    if explicit is not None and explicit not in CHECKPOINT_TYPES:
        raise ForgePilotError("checkpoint_policy", "Unknown checkpoint boundary")
    if node.node_type is NodeType.HUMAN_GATE and not approved:
        return None
    if explicit == "plan_approved" and not approved:
        return None
    if explicit == "validated" and any(item.artifact_type == "ValidationReport"
            and item.content.status != "passed" for item in artifacts):
        raise ForgePilotError("checkpoint_policy", "Failed validation is not a safe validation boundary")
    if explicit:
        return explicit
    types = {item.artifact_type for item in artifacts}
    if "ChangeSet" in types:
        return "repair" if "repair" in node.handler.lower() or "repair" in (node.agent_id or "").lower() else "implementation"
    if "ValidationReport" in types:
        reports = [item for item in artifacts if item.artifact_type == "ValidationReport"]
        return "validated" if all(item.content.status == "passed" for item in reports) else None
    for artifact_type, boundary in (("ReviewReport", "reviewed"), ("EvidenceBundle", "evidence"),
                                    ("TaskSpec", "task_spec")):
        if artifact_type in types:
            return boundary
    if approved and "ExecutionPlan" in types:
        return "plan_approved"
    return None


@dataclass(frozen=True)
class RecoveryPlan:
    task_id: str
    revision: int
    token: str
    checkpoint_id: str | None
    actions: tuple[dict, ...]
    checks: tuple[str, ...]
    blocked_reason: str | None = None
    requires_confirmation: bool = True
    evidence: dict | None = None

    def to_dict(self):
        return {"task_id": self.task_id, "revision": self.revision, "token": self.token,
                "checkpoint_id": self.checkpoint_id, "actions": list(self.actions),
                "checks": list(self.checks), "blocked_reason": self.blocked_reason,
                "requires_confirmation": self.requires_confirmation, "evidence": self.evidence}


class RecoveryManager:
    def __init__(self, event_store, *, artifacts=None, workspace_backend=None, policy_hash: str,
                 coordinator: ExecutionCoordinator | None = None):
        self.events = event_store
        self.store = GraphStore(event_store)
        self.coordinator = coordinator if coordinator is not None else coordinator_for_storage(event_store)
        self.checkpoints = CheckpointStore(event_store)
        self.artifacts = artifacts or event_store.artifacts
        self.workspace = workspace_backend or UnavailableWorkspaceBackend()
        self.policy_hash = policy_hash

    def _artifact_pairs(self, task_id, ids):
        pairs = []
        for artifact_id in ids:
            artifact = self.artifacts.get(artifact_id)
            if artifact.task_id != task_id:
                raise ForgePilotError("artifact_scope", "Recovery Artifact belongs to another task")
            pairs.append((artifact_id, artifact.content_sha256))
        return tuple(pairs)

    def _verify_pairs(self, task_id, pairs):
        if self._artifact_pairs(task_id, tuple(item[0] for item in pairs)) != tuple(pairs):
            raise ForgePilotError("artifact_integrity", "Frozen recovery Artifact changed")

    @staticmethod
    def _snapshot_data(snapshot):
        if snapshot is None:
            return None
        return {"workspace_id": snapshot.workspace_id, "task_id": snapshot.task_id,
                "head_commit": snapshot.head_commit, "file_hashes": dict(snapshot.file_hashes),
                "known": snapshot.known}

    def _inspect_workspace(self, task_id, *, required=False):
        record = self.store.workspace_record(task_id)
        if record is None:
            if required:
                raise ForgePilotError("workspace_corrupted", "Writer recovery requires a registered Worktree")
            return None, None
        if record["status"] != "active" or record["released_at"] is not None:
            raise ForgePilotError("workspace_corrupted", "Worktree is not active")
        current = self.workspace.inspect(record)
        if (not current.known or current.task_id != task_id
                or current.workspace_id != record["workspace_id"]):
            raise ForgePilotError("workspace_corrupted", "Worktree state or identity is unknown")
        return record, current

    def writer_preflight(self, task_id):
        record, current = self._inspect_workspace(task_id, required=True)
        latest = self.checkpoints.latest(task_id)
        if latest is None or latest.workspace_id != current.workspace_id:
            raise ForgePilotError("checkpoint_required", "Writer requires a verified safe checkpoint")
        self.checkpoints.verify(latest)
        self.workspace.verify_checkpoint(record, latest.checkpoint_id, latest.head_commit, latest.file_hashes)
        if current.head_commit != latest.head_commit or dict(current.file_hashes) != dict(latest.file_hashes):
            raise ForgePilotError("workspace_corrupted", "Writer does not start at the safe checkpoint")
        return latest

    def _verify_changes(self, changes, baseline, current):
        if baseline is None or current is None:
            raise ForgePilotError("checkpoint_required", "ChangeSet requires a Worktree safe point")
        before = {path: value for path, value in baseline.file_hashes.items() if value is not None}
        after = {path: value for path, value in current.file_hashes.items() if value is not None}
        expected = dict(before)
        seen = set()
        for artifact in changes:
            for changed in artifact.content.files:
                path = changed.path
                if path in seen or before.get(path) != changed.before_sha256:
                    raise ForgePilotError("workspace_corrupted", "ChangeSet preimage does not match checkpoint")
                seen.add(path)
                if changed.after_sha256 is None:
                    expected.pop(path, None)
                else:
                    expected[path] = changed.after_sha256
        # Complete manifest comparison catches unrelated edits, additions and deletions.
        if expected != after:
            raise ForgePilotError("workspace_corrupted", "ChangeSet does not match complete Worktree file hashes")

    def record_completed(self, task_id, node, attempt, result, *, approved=False, baseline=None):
        if result.status is not NodeStatus.SUCCEEDED:
            return None
        outputs = tuple(self.artifacts.get(item) for item in result.output_artifact_ids)
        boundary = checkpoint_type(node, outputs, approved=approved)
        if boundary is None:
            if recovery_role(node) == "writer":
                raise ForgePilotError("checkpoint_policy", "Successful writer must produce a verifiable ChangeSet")
            return None
        changes = tuple(item for item in outputs if isinstance(item.content, ChangeSet))
        record, current = self._inspect_workspace(task_id, required=boundary in {"implementation", "repair"})
        if boundary in {"implementation", "repair"}:
            if not changes or any(item.content.requires_additional_approval for item in changes):
                raise ForgePilotError("checkpoint_policy", "Writer completion requires a ChangeSet")
            safe_point = baseline or self.checkpoints.latest(task_id)
            if safe_point is None or current.head_commit != safe_point.head_commit:
                raise ForgePilotError("workspace_corrupted", "Writer changed HEAD before checkpoint publication")
            self._verify_changes(changes, safe_point, current)
        frozen = self.store.frozen_inputs(task_id, node.node_id, attempt)
        if frozen is None:
            raise ForgePilotError("recovery_integrity", "Checkpoint is missing frozen attempt inputs")
        if changes and any(item.content.execution_plan_artifact_id not in {pair[0] for pair in frozen}
                           for item in changes):
            raise ForgePilotError("checkpoint_policy", "Writer ChangeSet is outside its frozen execution plan")
        attempt_record = self.store.attempt_record(task_id, node.node_id, attempt)
        if current is not None and boundary not in {"implementation", "repair"}:
            latest = self.checkpoints.latest(task_id)
            if latest and (current.head_commit != latest.head_commit
                           or dict(current.file_hashes) != dict(latest.file_hashes)):
                raise ForgePilotError("workspace_corrupted", "Read-only node changed the Worktree")
        intent = self.checkpoints.save_intent(task_id, node.node_id, attempt, {
            "checkpoint_type": boundary, "result": result.to_dict(),
            "workspace": self._snapshot_data(current), "input_artifacts": [list(item) for item in frozen],
            "output_artifacts": [list(item) for item in self._artifact_pairs(task_id, result.output_artifact_ids)],
            "approved_paths": sorted({path for item in outputs if isinstance(item.content, ExecutionPlan)
                                      for step in item.content.steps for path in step.expected_files})
                              if boundary == "plan_approved" else None,
            "idempotency_key": attempt_record["idempotency_key"],
        })
        return self._complete_intent(intent, record, current)

    def _complete_intent(self, intent, record, current):
        payload = intent["payload"]
        if payload["workspace"] is not None:
            boundary = getattr(self.workspace, "checkpoint_boundary", None)
            if boundary is None:
                current = self.workspace.checkpoint(record, intent["checkpoint_id"], current)
            else:
                current = boundary(record, intent["checkpoint_id"], current,
                                   kind=payload["checkpoint_type"], approved_paths=payload.get("approved_paths"))
        return self.checkpoints.create(
            intent["task_id"], intent["node_id"], intent["attempt"], payload["checkpoint_type"],
            NodeResult.from_dict(payload["result"]), workspace_id=current.workspace_id if current else None,
            head_commit=current.head_commit if current else None,
            file_hashes=dict(current.file_hashes) if current else {},
            input_artifacts=tuple(tuple(item) for item in payload["input_artifacts"]),
            output_artifacts=tuple(tuple(item) for item in payload["output_artifacts"]),
            idempotency_key=payload["idempotency_key"],
        )

    def _inspect(self, task_id, graph: GraphDefinition):
        checks = []
        self.store.verify_recovery_integrity()
        checks.append("sqlite")
        snapshot = self.store.snapshot(task_id)
        definitions = {node.node_id: node for node in graph.nodes}
        executed_writers = [state for state in snapshot.nodes if state.attempt
                            and recovery_role(definitions[state.node_id]) == "writer"]
        required = bool(executed_writers)
        record, current = self._inspect_workspace(task_id, required=required)
        checks.append("worktree_record")
        checkpoints = self.checkpoints.list_for_task(task_id)
        latest = checkpoints[-1] if checkpoints else None
        intents = self.checkpoints.intents_for_task(task_id)
        pending_intents = {}
        intent_workspace = None
        for intent in intents:
            state = next((item for item in snapshot.nodes if item.node_id == intent["node_id"]), None)
            if state is None or state.attempt != intent["attempt"]:
                continue
            if self.checkpoints.for_attempt(task_id, state.node_id, state.attempt) is not None:
                continue
            payload = intent["payload"]
            result = NodeResult.from_dict(payload["result"])
            if result.status is not NodeStatus.SUCCEEDED or payload["checkpoint_type"] not in CHECKPOINT_TYPES:
                raise ForgePilotError("recovery_integrity", "Checkpoint intent is not a completed safe node")
            pending_intents[state.node_id] = intent
            if payload["workspace"] is not None:
                expected = WorkspaceSnapshot(**payload["workspace"])
                if current is None or expected.workspace_id != current.workspace_id or expected.task_id != task_id:
                    raise ForgePilotError("workspace_corrupted", "Checkpoint intent belongs to another Worktree")
                if intent_workspace is not None:
                    raise ForgePilotError("workspace_corrupted", "Multiple unpublished Worktree checkpoint intents")
                if dict(expected.file_hashes) != dict(current.file_hashes):
                    raise ForgePilotError("workspace_corrupted", "Checkpoint intent files changed")
                if expected.head_commit != current.head_commit:
                    # A durable intent permits recognition of an already-created
                    # external checkpoint; verification is read-only here.
                    self.workspace.verify_checkpoint(record, intent["checkpoint_id"], current.head_commit,
                                                     current.file_hashes)
                intent_workspace = expected
        if current is not None:
            heads = {record["head_commit"]}
            if latest and latest.workspace_id == current.workspace_id:
                heads.add(latest.head_commit)
            if intent_workspace is not None:
                heads.add(current.head_commit)
            if current.head_commit not in heads or current.head_commit is None:
                raise ForgePilotError("workspace_corrupted", "Worktree HEAD does not match durable records")
        checks.append("head")
        for checkpoint in checkpoints:
            self.checkpoints.verify(checkpoint)
            if checkpoint.workspace_id:
                if current is None or checkpoint.workspace_id != current.workspace_id:
                    raise ForgePilotError("workspace_corrupted", "Checkpoint Worktree is missing")
                self.workspace.verify_checkpoint(record, checkpoint.checkpoint_id,
                                                 checkpoint.head_commit, checkpoint.file_hashes)
        checks.append("checkpoint")
        abandoned_writers = [state for state in executed_writers if state.status in {
            NodeStatus.INTERRUPTED, NodeStatus.RUNNING, NodeStatus.RETRYABLE}]
        if len(abandoned_writers) > 1:
            raise ForgePilotError("workspace_corrupted", "Multiple abandoned writers have ambiguous ownership")
        if current is not None:
            if (latest is None or latest.workspace_id != current.workspace_id) and intent_workspace is None:
                raise ForgePilotError("checkpoint_required", "Worktree has no verified safe checkpoint")
            if intent_workspace is not None and intent_workspace.head_commit != (
                    latest.head_commit if latest else record["head_commit"]):
                raise ForgePilotError("workspace_corrupted", "Checkpoint intent did not start at the safe HEAD")
            if intent_workspace is None and current.head_commit != latest.head_commit:
                raise ForgePilotError("workspace_corrupted", "Worktree HEAD differs from the latest safe point")
            if not abandoned_writers and intent_workspace is None and dict(current.file_hashes) != dict(latest.file_hashes):
                raise ForgePilotError("workspace_corrupted", "Worktree files changed outside an interrupted writer")
            if abandoned_writers and self.checkpoints.for_attempt(
                    task_id, abandoned_writers[0].node_id, abandoned_writers[0].attempt) is not None \
                    and dict(current.file_hashes) != dict(latest.file_hashes):
                raise ForgePilotError("workspace_corrupted", "Completed writer files differ from its checkpoint")
        checks.append("file_hash")
        reconcile = getattr(self.artifacts, "reconcile_for_task", None)
        artifacts = tuple(reconcile(task_id)) if reconcile else ()
        self._verify_pairs(task_id, self.store.initial_inputs(task_id))
        for checkpoint in checkpoints:
            self._verify_pairs(task_id, checkpoint.input_artifacts)
            self._verify_pairs(task_id, checkpoint.output_artifacts)
        actions = []
        for state in snapshot.nodes:
            node = definitions[state.node_id]
            if state.result:
                self._artifact_pairs(task_id, state.result.output_artifact_ids)
            if not state.attempt:
                continue
            frozen = self.store.frozen_inputs(task_id, state.node_id, state.attempt)
            if frozen is None:
                # Schema-9 attempts did not retain IDs. Derivation is safe only for
                # the empty input set; nonempty inputs cannot be guessed on restart.
                attempt = self.store.attempt_record(task_id, state.node_id, state.attempt)
                empty_hash = sha256(b"[]").hexdigest()
                if attempt["input_hash"] != empty_hash:
                    raise ForgePilotError("recovery_integrity", "Legacy attempt has no frozen Artifact inputs")
                frozen = ()
            self._verify_pairs(task_id, frozen)
            attempt = self.store.attempt_record(task_id, state.node_id, state.attempt)
            expected = idempotency_key(task_id=task_id, node_id=state.node_id, attempt=state.attempt,
                input_artifact_hashes=tuple(value for _, value in frozen),
                agent_definition_hash=node.effective_agent_definition_hash, policy_hash=self.policy_hash)
            input_hash = sha256(json.dumps(frozen, separators=(",", ":")).encode()).hexdigest()
            if expected != attempt["idempotency_key"] or input_hash != attempt["input_hash"]:
                raise ForgePilotError("recovery_integrity", "Frozen attempt execution identity changed")
            cp = self.checkpoints.for_attempt(task_id, state.node_id, state.attempt)
            role = recovery_role(node)
            if cp:
                if expected != cp.idempotency_key or cp.input_artifacts != frozen:
                    raise ForgePilotError("recovery_integrity", "Checkpoint execution identity changed")
            intent = pending_intents.get(state.node_id)
            if intent:
                payload = intent["payload"]
                if (payload["idempotency_key"] != expected or
                        tuple(tuple(item) for item in payload["input_artifacts"]) != frozen):
                    raise ForgePilotError("recovery_integrity", "Checkpoint intent execution identity changed")
                self._verify_pairs(task_id, tuple(tuple(item) for item in payload["output_artifacts"]))
                intent_result = NodeResult.from_dict(payload["result"])
                if tuple(item[0] for item in payload["output_artifacts"]) != intent_result.output_artifact_ids:
                    raise ForgePilotError("recovery_integrity", "Checkpoint intent outputs disagree")
                if role == "writer":
                    changes = [self.artifacts.get(item) for item in intent_result.output_artifact_ids
                               if isinstance(self.artifacts.get(item).content, ChangeSet)]
                    if not changes or any(item.content.requires_additional_approval or
                            item.content.execution_plan_artifact_id not in {pair[0] for pair in frozen}
                            for item in changes):
                        raise ForgePilotError("recovery_integrity", "Checkpoint ChangeSet is outside frozen plan")
                    self._verify_changes(changes, latest, current)
            if state.status is NodeStatus.SUCCEEDED:
                if role == "delivery":
                    actions.append({"node_id": state.node_id, "action": "delivery_approval"})
                    continue
                if role == "writer" and cp is None and intent is None:
                    raise ForgePilotError("checkpoint_required", "Completed writer has no safe checkpoint")
                if cp and state.result.to_dict() != cp.result.to_dict():
                    raise ForgePilotError("recovery_integrity", "Checkpoint and successful node disagree")
                if intent and state.result.to_dict() != intent_result.to_dict():
                    raise ForgePilotError("recovery_integrity", "Checkpoint intent and successful node disagree")
                if cp and self.store.attempt_record(task_id, state.node_id, state.attempt)["checkpoint_id"] != cp.checkpoint_id:
                    actions.append({"node_id": state.node_id, "action": "reuse_checkpoint",
                                    "checkpoint_id": cp.checkpoint_id})
                if intent:
                    actions.append({"node_id": state.node_id, "action": "complete_intent",
                                    "checkpoint_id": intent["checkpoint_id"]})
                continue
            if state.status not in {NodeStatus.INTERRUPTED, NodeStatus.RUNNING, NodeStatus.RETRYABLE} \
                    and not (state.status is NodeStatus.AWAITING_HUMAN and (cp or intent)):
                continue
            action = {"node_id": state.node_id, "action": "retry_frozen"}
            if role == "delivery":
                action["action"] = "delivery_approval"
            elif role == "validator":
                action["action"] = "rerun_validation"
            elif cp is not None:
                action.update(action="reuse_checkpoint", checkpoint_id=cp.checkpoint_id)
            elif intent is not None:
                action.update(action="complete_intent", checkpoint_id=intent["checkpoint_id"])
            elif role == "writer":
                attempt = self.store.attempt_record(task_id, state.node_id, state.attempt)
                candidates = [item for item in artifacts if isinstance(item.content, ChangeSet)
                              and not item.content.requires_additional_approval
                              and item.producer_node == state.node_id
                              and datetime.fromisoformat(item.created_at.replace("Z", "+00:00")) >=
                                  datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))]
                if candidates:
                    if len(candidates) != 1:
                        raise ForgePilotError("recovery_integrity", "Abandoned writer has ambiguous ChangeSets")
                    if any(item.content.execution_plan_artifact_id not in {pair[0] for pair in frozen}
                           for item in candidates):
                        raise ForgePilotError("recovery_integrity", "Abandoned ChangeSet is outside the frozen plan")
                    self._verify_changes(candidates, latest, current)
                    action.update(action="adopt_changes", artifact_ids=[item.artifact_id for item in candidates])
                else:
                    if latest is None or current is None:
                        raise ForgePilotError("checkpoint_required", "Interrupted writer has no rollback point")
                    action.update(action="rollback_writer", checkpoint_id=latest.checkpoint_id)
            actions.append(action)
        checks.append("artifact_hash")
        fingerprint = {"task_id": task_id, "revision": snapshot.revision,
                       "definition": graph.definition_hash, "policy": self.policy_hash,
                       "workspace": self._snapshot_data(current),
                       "checkpoints": [item.checkpoint_id for item in checkpoints],
                       "intents": [(item["checkpoint_id"], item["data_sha256"]) for item in intents],
                       "artifacts": [(item.artifact_id, item.content_sha256) for item in artifacts],
                       "actions": actions}
        token = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return RecoveryPlan(task_id, snapshot.revision, token,
                            latest.checkpoint_id if latest else None, tuple(actions), tuple(checks),
                            evidence=fingerprint), record, current

    def _same_authorized_effects(self, saved, plan, current):
        """Recognize only effects journaled by this exact confirmed plan.

        This permits repeating confirmation after restore/commit succeeded but the
        SQLite publication failed. New input, files or unrelated commits still
        invalidate the token; startup clears this authority and asks again.
        """
        if not saved.get("applying") or not saved.get("evidence"):
            return False
        before, after = saved["evidence"], plan.evidence
        for field in ("task_id", "revision", "definition", "policy", "artifacts"):
            # Stored JSON converts tuples to lists; compare canonical JSON.
            if json.dumps(before[field], sort_keys=True) != json.dumps(after[field], sort_keys=True):
                return False
        old_actions = {item["node_id"]: item for item in saved["actions"]}
        new_actions = {item["node_id"]: item for item in plan.actions}
        if set(old_actions) != set(new_actions):
            return False
        added_ids = set(after["checkpoints"]) - set(before["checkpoints"])
        allowed_ids = set()
        workspace_matches = before["workspace"] == after["workspace"]
        for node_id, original in old_actions.items():
            latest_action = new_actions[node_id]
            kind = original["action"]
            if kind == "rollback_writer":
                if latest_action != original:
                    return False
                cp = self.checkpoints.latest(plan.task_id)
                workspace_matches |= (current is not None and current.head_commit == cp.head_commit
                                      and dict(current.file_hashes) == dict(cp.file_hashes))
            elif kind in {"adopt_changes", "complete_intent"}:
                if latest_action["action"] not in {kind, "complete_intent", "reuse_checkpoint"}:
                    return False
                state = next(item for item in self.store.snapshot(plan.task_id).nodes if item.node_id == node_id)
                cp_id = self.checkpoints.checkpoint_id_for(plan.task_id, node_id, state.attempt)
                allowed_ids.add(cp_id)
                intent = self.checkpoints.intent_for_attempt(plan.task_id, node_id, state.attempt)
                if intent is None:
                    return False
                if kind == "adopt_changes" and [item[0] for item in intent["payload"]["output_artifacts"]] != original["artifact_ids"]:
                    return False
                if current is not None:
                    expected = intent["payload"]["workspace"]
                    workspace_matches |= (expected is not None and dict(current.file_hashes) == expected["file_hashes"])
            elif latest_action != original:
                return False
        old_intents, new_intents = dict(before["intents"]), dict(after["intents"])
        if any(new_intents.get(key) != value for key, value in old_intents.items()):
            return False
        if set(new_intents) - set(old_intents) - allowed_ids or added_ids - allowed_ids:
            return False
        return workspace_matches

    def prepare(self, task_id, graph):
        with self.store.recovery_boundary():
            self.coordinator.assert_recovery_idle(task_id)
            return self._prepare(task_id, graph)

    def _prepare(self, task_id, graph):
        snapshot = self.store.snapshot(task_id)
        if snapshot.status in {NodeStatus.CANCELLED, NodeStatus.SUCCEEDED, NodeStatus.FAILED}:
            return RecoveryPlan(task_id, snapshot.revision, "", None, (), (), "terminal_graph", False)
        try:
            self.store.assert_recovery_storage()
            self.store.reconcile_events(task_id)
            plan, _, _ = self._inspect(task_id, graph)
        except Exception as exc:
            code = exc.code if isinstance(exc, ForgePilotError) else "recovery_integrity"
            snapshot = self.store.snapshot(task_id)
            plan = RecoveryPlan(task_id, snapshot.revision, "", None, (), (), code)
        self.store.save_recovery_plan(task_id, expected_revision=plan.revision, plan=plan.to_dict())
        return plan

    def confirm(self, task_id, graph, *, token, expected_revision):
        # Serialize cancellation with the complete compare/effect/publication
        # boundary. Backend operations must also compare their own expected
        # Worktree snapshot atomically; a SQLite transaction cannot cover Git.
        with self.store.recovery_boundary():
            return self._confirm(task_id, graph, token=token, expected_revision=expected_revision)

    def _confirm(self, task_id, graph, *, token, expected_revision):
        run = self.store.run_record(task_id)
        saved = json.loads(run["recovery_plan_json"] or "{}")
        if saved.get("token") == token and token and saved.get("confirmed"):
            return self.store.snapshot(task_id)
        if (not token or saved.get("token") != token or saved.get("blocked_reason")
                or run["current_revision"] != expected_revision or saved.get("revision") != expected_revision):
            raise ForgePilotError("graph_conflict", "Recovery confirmation is stale or blocked")
        plan, record, current = self._inspect(task_id, graph)
        if plan.token != token and not self._same_authorized_effects(saved, plan, current):
            raise ForgePilotError("recovery_changed", "Recovery evidence changed; inspect and confirm again")
        saved["applying"] = True
        self.store.save_recovery_plan(task_id, expected_revision=expected_revision, plan=saved)
        current_run = self.store.run_record(task_id)
        if current_run["current_revision"] != expected_revision or current_run["status"] in {"cancelled", "failed", "succeeded"}:
            raise ForgePilotError("graph_conflict", "Graph changed before recovery effects")
        definitions = {node.node_id: node for node in graph.nodes}
        states = {node.node_id: node for node in self.store.snapshot(task_id).nodes}
        changes = []
        for action in plan.actions:
            node_id, kind = action["node_id"], action["action"]
            state = states[node_id]
            change = {**action, "target": "ready"}
            if kind == "reuse_checkpoint":
                cp = self.checkpoints.for_attempt(task_id, node_id, state.attempt)
                change.update(target="succeeded", result=cp.result)
            elif kind == "complete_intent":
                intent = self.checkpoints.intent_for_attempt(task_id, node_id, state.attempt)
                cp = self._complete_intent(intent, record, current)
                change.update(target="succeeded", result=cp.result, checkpoint_id=cp.checkpoint_id)
                if record:
                    current = self.workspace.inspect(record)
            elif kind == "delivery_approval":
                change.update(target="awaiting_human", result=NodeResult(
                    NodeStatus.AWAITING_HUMAN, decision_summary="Delivery requires a new human approval"))
            elif kind == "rollback_writer":
                current = self.workspace.restore(record, action["checkpoint_id"], expected=current)
                cp = self.checkpoints.latest(task_id)
                if current.head_commit != cp.head_commit or dict(current.file_hashes) != dict(cp.file_hashes):
                    raise ForgePilotError("workspace_corrupted", "Rollback did not restore the verified safe point")
            elif kind == "adopt_changes":
                result = NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=tuple(action["artifact_ids"]),
                                    decision_summary="Recovered verified completed changes")
                cp = self.record_completed(task_id, definitions[node_id], state.attempt, result)
                change.update(target="succeeded", result=result, checkpoint_id=cp.checkpoint_id)
            elif kind == "rerun_validation":
                intent = self.checkpoints.intent_for_attempt(task_id, node_id, state.attempt)
                if intent and self.checkpoints.for_attempt(task_id, node_id, state.attempt) is None:
                    cp = self._complete_intent(intent, record, current)
                    change["checkpoint_id"] = cp.checkpoint_id
            changes.append(change)
        return self.store.apply_recovery(task_id, expected_revision=expected_revision, token=token,
                                         changes=tuple(changes))


__all__ = ["RecoveryManager", "RecoveryPlan", "checkpoint_type", "recovery_role"]
