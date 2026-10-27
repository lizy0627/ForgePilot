"""One durable, revision-bound approval for the complete analysis package.

Revisions append a new human-gate attempt; prior artifacts and attempts remain
auditable. This boundary grants no file, process, or Git capabilities.
"""
from dataclasses import dataclass
from functools import wraps
from uuid import uuid4
import json
import sqlite3

from ..artifacts.canonical import content_sha256
from ..artifacts.sqlite_store import ArtifactStorageError
from ..errors import ForgePilotError
from .models import NodeResult, NodeStatus, idempotency_key
from .store import GraphStore, _now


def _storage_boundary(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error:
            raise ArtifactStorageError("Analysis review persistence failed") from None
    return checked


def scope_content(spec):
    return {key: spec.model_dump(mode="json")[key] for key in (
        "allowed_scope", "forbidden_scope", "test_policy", "data_policy")}


@dataclass(frozen=True)
class AnalysisReview:
    task_id: str
    revision: int
    task_spec_artifact_id: str
    task_spec_hash: str
    plan_artifact_id: str
    plan_hash: str
    scope_hash: str
    review_hash: str


class AnalysisReviewStore:
    gate_id = "execution_approval"

    def __init__(self, events):
        self.events = events
        self.graphs = GraphStore(events)

    @_storage_boundary
    def bind(self, task_id, spec, plan):
        """Called only inside the running gate, before its visible publication."""
        with self.events._transaction():
            row = self.events._db.execute(
                "SELECT status FROM graph_node_states WHERE task_id=? AND node_id=?",
                (task_id, self.gate_id)).fetchone()
            if row is None or row[0] != "running":
                raise ForgePilotError("invalid_state", "Review gate is not running")
            self.events._db.execute(
                "UPDATE task_graph_runs SET task_spec_artifact_id=?,plan_artifact_id=?,scope_hash=? WHERE task_id=?",
                (spec.artifact_id, plan.artifact_id, content_sha256(scope_content(spec.content)), task_id))

    @_storage_boundary
    def get(self, task_id):
        with self.events._lock:
            run = self.events.missions.get_graph_run(task_id)
            if not run["task_spec_artifact_id"] or not run["plan_artifact_id"]:
                raise ForgePilotError("invalid_state", "Analysis has no review package")
            spec = self.events.artifacts.get(run["task_spec_artifact_id"])
            plan = self.events.artifacts.get(run["plan_artifact_id"])
            scope_hash = content_sha256(scope_content(spec.content))
            if scope_hash != run["scope_hash"]:
                raise ForgePilotError("approval_stale", "Review scope has changed")
            values = dict(task_id=task_id, revision=run["current_revision"],
                task_spec_artifact_id=spec.artifact_id, task_spec_hash=spec.content_sha256,
                plan_artifact_id=plan.artifact_id, plan_hash=plan.content_sha256,
                scope_hash=scope_hash)
            return AnalysisReview(**values, review_hash=content_sha256(values))

    @_storage_boundary
    def approve(self, review, *, approved_by):
        # Identifier validation belongs to the trusted application boundary.
        from ..mission_store import _identifier
        _identifier(approved_by, "approver")
        stamp, approval_id = _now(), str(uuid4())
        with self.events._transaction():
            current = self.get(review.task_id)
            if current != review:
                raise ForgePilotError("approval_stale", "TaskSpec, Plan, Scope or revision changed")
            db = self.events._db
            row = db.execute("SELECT * FROM graph_node_states WHERE task_id=? AND node_id=?",
                             (review.task_id, self.gate_id)).fetchone()
            run = self.events.missions.get_graph_run(review.task_id)
            if (row is None or row["status"] != "awaiting_human"
                    or run["status"] != "awaiting_human" or run["recovery_required"]):
                raise ForgePilotError("invalid_state", "Execution review is not awaiting approval")
            result = NodeResult(NodeStatus.SUCCEEDED,
                output_artifact_ids=(review.task_spec_artifact_id, review.plan_artifact_id),
                decision_summary="TaskSpec, ExecutionPlan and Scope approved together")
            result_json = self.graphs._result_json(result)
            db.execute("""INSERT INTO approvals(approval_id,task_id,approval_type,status,
                task_spec_artifact_id,task_spec_hash,plan_artifact_id,plan_hash,scope_hash,approved_by,created_at)
                VALUES(?,?,'execution','approved',?,?,?,?,?,?,?)""",
                (approval_id, review.task_id, review.task_spec_artifact_id, review.task_spec_hash,
                 review.plan_artifact_id, review.plan_hash, review.scope_hash, approved_by, stamp))
            db.execute("UPDATE graph_node_states SET status='succeeded',result_json=?,updated_at=? WHERE task_id=? AND node_id=?",
                       (result_json, stamp, review.task_id, self.gate_id))
            db.execute("UPDATE task_node_attempts SET status='succeeded',finished_at=? WHERE task_id=? AND node_id=? AND attempt=?",
                       (stamp, review.task_id, self.gate_id, row["attempt"]))
            db.execute("UPDATE graph_node_results SET status='succeeded',result_json=? WHERE task_id=? AND node_id=? AND attempt=?",
                       (result_json, review.task_id, self.gate_id, row["attempt"]))
            db.execute("UPDATE task_graph_runs SET status='succeeded',current_revision=current_revision+1,updated_at=?,finished_at=? WHERE task_id=?",
                       (stamp, stamp, review.task_id))
            self._event(db, review.task_id, review.revision + 1, row["attempt"],
                        "execution_approved", "succeeded", {"approval_id": approval_id,
                        "review_hash": review.review_hash}, stamp)
        return self.events.missions.get_approval(approval_id)

    @_storage_boundary
    def replace(self, review, spec, plan, gate_definition, policy_hash):
        """New immutable package and attempt; changes invalidate previous approvals."""
        stamp = _now()
        with self.events._transaction():
            if self.get(review.task_id) != review:
                raise ForgePilotError("approval_stale", "Review changed during revision")
            db = self.events._db
            run = self.events.missions.get_graph_run(review.task_id)
            if run["status"] not in {"awaiting_human", "succeeded"} or run["recovery_required"]:
                raise ForgePilotError("invalid_state", "Cannot revise an active or cancelled analysis")
            row = db.execute("SELECT * FROM graph_node_states WHERE task_id=? AND node_id=?",
                             (review.task_id, self.gate_id)).fetchone()
            attempt = row["attempt"] + 1
            pairs = ((spec.artifact_id, spec.content_sha256), (plan.artifact_id, plan.content_sha256))
            key = idempotency_key(task_id=review.task_id, node_id=self.gate_id, attempt=attempt,
                input_artifact_hashes=tuple(p[1] for p in pairs),
                agent_definition_hash=gate_definition.effective_agent_definition_hash, policy_hash=policy_hash)
            result = NodeResult(NodeStatus.AWAITING_HUMAN,
                output_artifact_ids=tuple(p[0] for p in pairs), decision_summary="Revised execution package requires approval")
            result_json = self.graphs._result_json(result)
            db.execute("""INSERT INTO task_node_attempts(task_id,node_id,attempt,node_type,status,
                idempotency_key,input_hash,started_at,input_artifacts_json,duration_ms)
                VALUES(?,?,?,'human_gate','awaiting_human',?,?,?,?,0)""",
                (review.task_id, self.gate_id, attempt, key, content_sha256(pairs), stamp, json.dumps(pairs)))
            db.execute("""INSERT INTO graph_node_results(task_id,node_id,attempt,idempotency_key,status,result_json,created_at)
                VALUES(?,?,?,?,'awaiting_human',?,?)""", (review.task_id, self.gate_id, attempt, key, result_json, stamp))
            db.execute("UPDATE graph_node_states SET status='awaiting_human',attempt=?,result_json=?,updated_at=? WHERE task_id=? AND node_id=?",
                       (attempt, result_json, stamp, review.task_id, self.gate_id))
            db.execute("""UPDATE task_graph_runs SET task_spec_artifact_id=?,plan_artifact_id=?,scope_hash=?,
                status='awaiting_human',current_revision=current_revision+1,updated_at=?,finished_at=NULL WHERE task_id=?""",
                (spec.artifact_id, plan.artifact_id, content_sha256(scope_content(spec.content)), stamp, review.task_id))
            self._event(db, review.task_id, review.revision + 1, attempt, "execution_review_revised",
                        "awaiting_human", {"task_spec_artifact_id": spec.artifact_id,
                        "plan_artifact_id": plan.artifact_id}, stamp)
        return self.get(review.task_id)

    def _event(self, db, task_id, revision, attempt, kind, state, payload, stamp):
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)).fetchone()[0]
        db.execute("""INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,attempt,state,payload_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (task_id, sequence, revision, kind, self.gate_id, attempt,
            state, self.graphs._json(payload), stamp))
