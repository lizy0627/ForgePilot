"""Immutable file additions proposed by a writer and confirmed against its paused attempt."""
from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
import json
from uuid import uuid4

from pydantic import Field

from ..artifacts.canonical import content_sha256
from ..artifacts.models import ChangeSet, ExecutionPlan, PlanFileCriteria, STEP_ID_PATTERN, TaskSpec
from ..artifacts.validation import create_artifact
from ..errors import ForgePilotError
from ..integrity import is_test_path
from ..mission_events import append_mission_event
from ..path_identity import path_key
from .analysis_review import scope_content


class ScopeExpansionFile(PlanFileCriteria):
    plan_step_id: str = Field(pattern=STEP_ID_PATTERN, max_length=32)


def _revised_contents(spec, plan, files, repository, new_spec_id):
    """Only file lists and the minimum required file count may grow."""
    spec_data = spec.model_dump(mode="json")
    plan_data = plan.model_dump(mode="json")
    steps = {step["step_id"]: step for step in plan_data["steps"]}
    writer_paths = {path_key(path) for step in steps.values() if step["owner"] in {"implementer", "implementer.v1"}
                    for path in step["expected_files"]}
    requested = set()
    scope = spec_data["allowed_scope"]
    for item in files:
        path, key = item.path, path_key(item.path)
        if any(c in path for c in "*?[]") or repository.normalize_plan_path(path) != path:
            raise ValueError("Scope expansion requires concrete canonical file paths")
        if any(fnmatchcase(key, path_key(pattern)) for pattern in spec.forbidden_scope):
            raise ValueError("Scope expansion cannot override a forbidden path")
        step = steps.get(item.plan_step_id)
        if not step or step["owner"] not in {"implementer", "implementer.v1"}:
            raise ValueError("File must reference an existing Implementer-owned step")
        if not set(item.acceptance_criteria_ids) <= set(step["acceptance_criteria_ids"]):
            raise ValueError("File criteria must already belong to the selected step")
        if key in {path_key(path) for path in step["expected_files"]}:
            raise ValueError("File is already approved for this step")
        if is_test_path(path):
            if (repository._path(path, allow_missing=True).exists() or
                    not spec.test_policy.allow_new_tests or not step["adds_tests"]):
                raise ValueError("Scope expansion cannot grant existing test edits or change the test policy")
        step["expected_files"].append(path)
        step.setdefault("file_criteria", []).append(item.model_dump(mode="json", exclude={"plan_step_id"}))
        requested.add(key)
        if not any(fnmatchcase(key, path_key(pattern)) for pattern in scope["path_patterns"]):
            scope["path_patterns"].append(path)
    original_limit = scope["max_changed_files"]
    new_files = requested - writer_paths
    scope["max_changed_files"] = max(original_limit,
        min(original_limit + len(new_files), len(writer_paths | new_files)))
    plan_data["task_spec_artifact_id"] = new_spec_id
    return TaskSpec.model_validate(spec_data), ExecutionPlan.model_validate(plan_data)


class ScopeExpansionStore:
    def __init__(self, events):
        self.events = events

    def propose(self, context, spec, plan, files, *, repository, partial_change_set_artifact_id,
                checkpoint_id, budget_usage):
        task_id, node_id = context.task_id, context.node.node_id
        if node_id != "implementer" and node_id not in {"repair_1", "repair_2", "repair_3"}:
            raise ValueError("Only a paused writer may request a scope expansion")
        if not 1 <= len(files) <= 64:
            raise ValueError("Expected a bounded non-empty list of files")
        files = tuple(ScopeExpansionFile.model_validate(item) for item in files)
        files = tuple(item.model_copy(update={"reason": self.events.redactor.text(item.reason)}) for item in files)
        new_spec_id = str(uuid4())
        spec_content, plan_content = _revised_contents(spec.content, plan.content, files, repository, new_spec_id)
        with self.events.missions.consistent_view():
            run = self.events.missions.get_graph_run(task_id)
            if "locked" in run["graph_id"]:
                raise ForgePilotError("policy_denied", "Locked TaskSpec scope is bound to the task manifest")
            if run["task_spec_artifact_id"] != spec.artifact_id or run["plan_artifact_id"] != plan.artifact_id:
                raise ForgePilotError("approval_stale", "Writer inputs differ from the current grant")
            if partial_change_set_artifact_id:
                partial = self.events.artifacts.get(partial_change_set_artifact_id)
                if (partial.task_id != task_id or partial.producer_node != node_id
                        or not isinstance(partial.content, ChangeSet) or not partial.content.requires_additional_approval
                        or partial.content.execution_plan_artifact_id != plan.artifact_id):
                    raise ValueError("Partial ChangeSet differs from the active writer")
            proposed_spec = self.events.artifacts.revise(spec.artifact_id, content=spec_content,
                producer="orchestrator.v1", producer_node="scope_expansion", artifact_id_override=new_spec_id)
            proposed_plan = create_artifact(task_id=task_id, producer="orchestrator.v1",
                producer_node="scope_expansion", parent_artifact_ids=(proposed_spec.artifact_id, plan.artifact_id),
                content=plan_content)
            self.events.artifacts.put(proposed_plan)
            with self.events.transaction() as db:
                current = db.execute("""SELECT status,attempt FROM graph_node_states
                    WHERE task_id=? AND node_id=?""", (task_id, node_id)).fetchone()
                if current is None or tuple(current) != ("running", context.attempt):
                    raise ForgePilotError("invalid_state", "Writer attempt is no longer running")
                db.execute("""INSERT INTO writer_scope_expansions(task_id,node_id,attempt,base_spec_id,
                    base_plan_id,spec_id,plan_id,checkpoint_id,partial_change_set_id,budget_json,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'pending')""", (task_id, node_id, context.attempt,
                    spec.artifact_id, plan.artifact_id, proposed_spec.artifact_id, proposed_plan.artifact_id,
                    checkpoint_id, partial_change_set_artifact_id, json.dumps(budget_usage, allow_nan=False)))

    def approved_package(self, task_id):
        with self.events.locked_connection() as db:
            row = db.execute("""SELECT x.spec_id,x.plan_id FROM writer_scope_expansions x
                JOIN task_graph_runs g ON x.task_id=g.task_id AND x.spec_id=g.task_spec_artifact_id
                    AND x.plan_id=g.plan_artifact_id
                WHERE x.task_id=? AND x.status='approved'""", (task_id,)).fetchone()
            return tuple(row) if row else None

    def approved_budget(self, task_id, node_id, previous_attempt):
        with self.events.locked_connection() as db:
            row = db.execute("""SELECT budget_json FROM writer_scope_expansions WHERE task_id=?
                AND node_id=? AND attempt=? AND status='approved'""", (task_id, node_id, previous_attempt)).fetchone()
            return json.loads(row[0]) if row else None

    def preview(self, task_id):
        with self.events.locked_connection() as db:
            row = db.execute("""SELECT x.*,g.current_revision,g.status AS graph_status,
                    n.status AS node_status,n.attempt AS current_attempt
                FROM writer_scope_expansions x JOIN task_graph_runs g ON x.task_id=g.task_id
                JOIN graph_node_states n ON n.task_id=x.task_id AND n.node_id=x.node_id
                WHERE x.task_id=? ORDER BY x.rowid DESC LIMIT 1""", (task_id,)).fetchone()
            if (row is None or (row["status"] == "pending" and
                    (row["graph_status"] != "awaiting_human" or row["node_status"] != "awaiting_human"
                     or row["current_attempt"] != row["attempt"]))):
                raise ForgePilotError("scope_expansion_not_found", "No published scope expansion for this mission")
            spec = self.events.artifacts.get(row["spec_id"])
            plan = self.events.artifacts.get(row["plan_id"])
            base = self.events.artifacts.get(row["base_spec_id"])
            original_plan = self.events.artifacts.get(row["base_plan_id"])
            if (any(item.task_id != task_id for item in (base, original_plan, spec, plan))
                    or spec.parent_artifact_ids != (base.artifact_id,)
                    or plan.parent_artifact_ids != (spec.artifact_id, original_plan.artifact_id)
                    or plan.content.task_spec_artifact_id != spec.artifact_id
                    or original_plan.content.task_spec_artifact_id != base.artifact_id):
                raise ForgePilotError("mission_approval_conflict", "Scope expansion revision lineage changed")
            previous = {(step.step_id, path_key(item.path)) for step in original_plan.content.steps
                        for item in step.file_criteria}
            files = [item.model_dump(mode="json") | {"plan_step_id": step.step_id} for step in plan.content.steps
                     for item in step.file_criteria if (step.step_id, path_key(item.path)) not in previous]
            return dict(task_id=task_id, revision=row["current_revision"], node_id=row["node_id"],
                attempt=row["attempt"], status=row["status"], files=files,
                task_spec_artifact_id=spec.artifact_id, task_spec_hash=spec.content_sha256,
                plan_artifact_id=plan.artifact_id, plan_hash=plan.content_sha256,
                scope_hash=content_sha256(scope_content(spec.content)), checkpoint_id=row["checkpoint_id"],
                partial_change_set_artifact_id=row["partial_change_set_id"],
                previous_max_changed_files=base.content.allowed_scope.max_changed_files,
                max_changed_files=spec.content.allowed_scope.max_changed_files,
                replay_behavior="Approval restores the task worktree to the checkpoint before this writer. "
                    "Its partial changes are discarded from the worktree and remain viewable in the saved diff. "
                    "The same node runs again with the expanded plan. Earlier completed nodes are preserved.")

    def decide(self, task_id, *, expected_revision, task_spec_hash, plan_hash, scope_hash, approved,
               before_approve=None):
        # BEGIN IMMEDIATE covers the revision comparison, idempotent worktree
        # restore and SQLite publication. A competing decision cannot observe a
        # half-applied authorization even through a second connection.
        with self.events.transaction() as db:
            preview = self.preview(task_id)
            if preview["revision"] != expected_revision:
                raise ForgePilotError("mission_revision_conflict", "Refresh the mission before deciding")
            if (preview["status"] != "pending" or
                    (task_spec_hash, plan_hash, scope_hash) != tuple(preview[key] for key in
                    ("task_spec_hash", "plan_hash", "scope_hash"))):
                raise ForgePilotError("mission_approval_conflict", "Review the current pending expansion")
            node_id, attempt = preview["node_id"], preview["attempt"]
            current = db.execute("""SELECT 1 FROM writer_scope_expansions x
                    JOIN task_graph_runs g ON x.task_id=g.task_id AND g.task_spec_artifact_id=x.base_spec_id
                        AND g.plan_artifact_id=x.base_plan_id
                    JOIN approvals a ON a.task_id=x.task_id AND a.task_spec_artifact_id=x.base_spec_id
                        AND a.plan_artifact_id=x.base_plan_id AND a.scope_hash=g.scope_hash
                    WHERE x.task_id=? AND x.node_id=? AND x.attempt=? AND a.status='approved'
                      AND a.approval_type IN ('execution','scope_expansion')""", (task_id, node_id, attempt)).fetchone()
            if not current:
                raise ForgePilotError("mission_approval_conflict", "Original writer grant has changed")
            if approved:
                if before_approve is None:
                    raise ValueError("Writer replay requires an explicit checkpoint restore")
                before_approve(preview)
            decision = "approved" if approved else "rejected"
            approval_id, stamp = str(uuid4()), datetime.now(timezone.utc).isoformat()
            changed = db.execute("""UPDATE writer_scope_expansions SET status=? WHERE task_id=?
                AND node_id=? AND attempt=? AND status='pending'""", (decision, task_id, node_id, attempt))
            if changed.rowcount != 1:
                raise ForgePilotError("mission_approval_conflict", "Expansion was already decided")
            if approved:
                db.execute("""UPDATE task_graph_runs SET task_spec_artifact_id=?, plan_artifact_id=?,
                    scope_hash=?,status='running',recovery_required=0,recovery_plan_json=NULL WHERE task_id=?""",
                    (preview["task_spec_artifact_id"], preview["plan_artifact_id"], scope_hash, task_id))
                db.execute("""UPDATE graph_node_states SET status='ready',result_json=NULL,updated_at=?
                    WHERE task_id=? AND node_id=?""", (stamp, task_id, node_id))
                db.execute("""UPDATE task_node_attempts SET finished_at=? WHERE task_id=? AND node_id=?
                    AND attempt=?""", (stamp, task_id, node_id, attempt))
            db.execute("""INSERT INTO approvals(approval_id,task_id,approval_type,status,task_spec_artifact_id,
                task_spec_hash,plan_artifact_id,plan_hash,scope_hash,approved_by,created_at)
                VALUES(?,?,'scope_expansion',?,?,?,?,?,?,'user',?)""", (approval_id, task_id, decision,
                preview["task_spec_artifact_id"], task_spec_hash, preview["plan_artifact_id"], plan_hash,
                scope_hash, stamp))
            append_mission_event(self.events, task_id, "approval." + decision, {
                "approval_id": approval_id, "approval_type": "scope_expansion", "attempt": attempt,
                "task_spec_hash": task_spec_hash, "plan_hash": plan_hash, "scope_hash": scope_hash,
                "checkpoint_id": preview["checkpoint_id"],
            }, node_id=node_id, actor="user")
