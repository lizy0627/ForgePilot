"""Atomic SQLite projection for graph state, attempts, results, and events."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Mapping

from ..errors import ForgePilotError
from .graph import ValidatedGraph
from .models import (
    GraphEvent, GraphSnapshot, NodeResult, NodeState, NodeStatus, NodeType,
    PolicyAction, TERMINAL_NODE_STATUSES, assert_transition,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GraphStore:
    """Uses SqliteEventStore's writer lock so projection and event never diverge."""

    def __init__(self, event_store) -> None:
        self.events_store = event_store

    def _json(self, value: Mapping[str, Any]) -> str:
        from ..mission_events import safe_event_data
        return json.dumps(safe_event_data(self.events_store.redactor, dict(value)), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _result_json(self, result: NodeResult) -> str:
        return json.dumps(self.events_store.redactor.data(result.to_dict()), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def create_run(self, task_id: str, graph: ValidatedGraph, *,
                   inputs: tuple[tuple[str, str], ...] = (), policy_hash: str | None = None) -> GraphSnapshot:
        if policy_hash is None:
            from .policies import DefaultFailurePolicy
            policy_hash = DefaultFailurePolicy().definition_hash
        stamp = _now()
        try:
            with self.events_store._transaction():
                self.events_store._db.execute(
                    """INSERT INTO task_graph_runs(task_id,graph_id,graph_version,status,current_revision,
                       created_at,updated_at,definition_hash,definition_json,policy_hash)
                       VALUES(?,?,?,'pending',1,?,?,?,?,?)""",
                    (task_id, graph.definition.graph_id, graph.definition.version, stamp, stamp,
                     graph.definition.definition_hash, json.dumps(graph.definition.to_dict()), policy_hash),
                )
                self.events_store._db.executemany(
                    """INSERT INTO graph_node_states(task_id,node_id,node_type,status,attempt,updated_at)
                       VALUES(?,?,?,'pending',0,?)""",
                    ((task_id, node.node_id, node.node_type.value, stamp)
                     for node in graph.definition.nodes),
                )
                self.events_store._db.executemany(
                    """INSERT INTO graph_run_inputs(task_id,ordinal,artifact_id,content_sha256)
                       VALUES(?,?,?,?)""",
                    ((task_id, ordinal, artifact_id, content_hash)
                     for ordinal, (artifact_id, content_hash) in enumerate(inputs)),
                )
                self.events_store._db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,payload_json,created_at)
                       VALUES(?,1,1,'graph_created',?,?)""",
                    (task_id, self._json({"graph_id": graph.definition.graph_id,
                                         "graph_version": graph.definition.version}), stamp),
                )
        except sqlite3.IntegrityError:
            raise ForgePilotError("graph_conflict", "Graph run already exists or is invalid") from None
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Graph run could not be created") from None
        return self.snapshot(task_id)

    def initial_inputs(self, task_id: str) -> tuple[tuple[str, str], ...]:
        with self.events_store._lock:
            rows = self.events_store._db.execute(
                """SELECT artifact_id,content_sha256 FROM graph_run_inputs
                   WHERE task_id=? ORDER BY ordinal""", (task_id,)
            ).fetchall()
        return tuple((row["artifact_id"], row["content_sha256"]) for row in rows)

    def snapshot(self, task_id: str) -> GraphSnapshot:
        with self.events_store._lock:
            run = self.events_store._db.execute(
                "SELECT * FROM task_graph_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            if run is None:
                raise ForgePilotError("unknown_graph", "Graph run does not exist")
            rows = self.events_store._db.execute(
                "SELECT * FROM graph_node_states WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
        nodes = tuple(NodeState(
            node_id=row["node_id"], node_type=NodeType(row["node_type"]),
            status=NodeStatus(row["status"]), attempt=row["attempt"],
            result=NodeResult.from_dict(json.loads(row["result_json"]))
            if row["result_json"] else None,
        ) for row in rows)
        return GraphSnapshot(
            task_id=run["task_id"], graph_id=run["graph_id"],
            graph_version=run["graph_version"], definition_hash=run["definition_hash"] or "",
            status=NodeStatus(run["status"]),
            revision=run["current_revision"], nodes=nodes,
        )

    def graph_events(self, task_id: str, *, after_sequence: int = 0) -> tuple[GraphEvent, ...]:
        with self.events_store._lock:
            rows = self.events_store._db.execute(
                "SELECT * FROM graph_events WHERE task_id=? AND sequence>? ORDER BY sequence",
                (task_id, after_sequence),
            ).fetchall()
        return tuple(GraphEvent(
            task_id=row["task_id"], sequence=row["sequence"], revision=row["revision"],
            event_type=row["event_type"], node_id=row["node_id"], attempt=row["attempt"],
            state=NodeStatus(row["state"]) if row["state"] else None,
            payload=json.loads(row["payload_json"]), created_at=row["created_at"],
        ) for row in rows)

    def successful_result(self, key: str) -> NodeResult | None:
        with self.events_store._lock:
            row = self.events_store._db.execute(
                "SELECT result_json FROM graph_node_results WHERE idempotency_key=? AND status='succeeded'",
                (key,),
            ).fetchone()
        return NodeResult.from_dict(json.loads(row[0])) if row else None

    def transition(self, task_id: str, node_id: str, *, expected_revision: int,
                   source: NodeStatus, target: NodeStatus, event_type: str = "node_state_changed",
                   payload: Mapping[str, Any] | None = None, graph_status: NodeStatus | None = None,
                   attempt: int | None = None, idempotency_key: str | None = None,
                   input_hash: str | None = None, agent_id: str | None = None,
                   result: NodeResult | None = None,
                   policy_action: PolicyAction | None = None,
                   checkpoint_id: str | None = None,
                   input_artifacts: tuple[tuple[str, str], ...] = ()) -> GraphSnapshot:
        assert_transition(source, target)
        stamp = _now()
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute(
                    "SELECT current_revision FROM task_graph_runs WHERE task_id=?", (task_id,)
                ).fetchone()
                if run is None:
                    raise ForgePilotError("unknown_graph", "Graph run does not exist")
                if run["current_revision"] != expected_revision:
                    raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
                node = db.execute(
                    "SELECT * FROM graph_node_states WHERE task_id=? AND node_id=?",
                    (task_id, node_id),
                ).fetchone()
                if node is None:
                    raise ForgePilotError("unknown_node", "Graph node does not exist")
                if NodeStatus(node["status"]) is not source:
                    raise ForgePilotError("graph_conflict", "Node state changed concurrently")
                actual_attempt = node["attempt"] if attempt is None else attempt
                if target is NodeStatus.RUNNING and source is NodeStatus.READY:
                    if (attempt is None or idempotency_key is None or input_hash is None
                            or attempt != node["attempt"] + 1):
                        raise ForgePilotError("node_attempt", "Invalid node attempt claim")
                    db.execute(
                        """INSERT INTO task_node_attempts(task_id,node_id,attempt,node_type,agent_id,status,
                           idempotency_key,input_hash,started_at,input_artifacts_json)
                           VALUES(?,?,?,?,?,'running',?,?,?,?)""",
                        (task_id, node_id, attempt, node["node_type"], agent_id,
                         idempotency_key, input_hash, stamp, json.dumps(input_artifacts)),
                    )
                result_json = (self._result_json(result)
                               if result is not None else node["result_json"])
                changed = db.execute(
                    """UPDATE graph_node_states SET status=?,attempt=?,result_json=?,updated_at=?
                       WHERE task_id=? AND node_id=? AND status=?""",
                    (target.value, actual_attempt, result_json, stamp,
                     task_id, node_id, source.value),
                ).rowcount
                if changed != 1:
                    raise ForgePilotError("graph_conflict", "Node state changed concurrently")

                if result is not None:
                    attempt_row = db.execute(
                        """SELECT idempotency_key FROM task_node_attempts
                           WHERE task_id=? AND node_id=? AND attempt=?""",
                        (task_id, node_id, actual_attempt),
                    ).fetchone()
                    if attempt_row is None:
                        raise ForgePilotError("node_attempt", "Node result has no active attempt")
                    finish = target is not NodeStatus.AWAITING_HUMAN
                    db.execute(
                        """UPDATE task_node_attempts SET status=?,finished_at=?,duration_ms=?,error_code=?,error_summary=?
                           WHERE task_id=? AND node_id=? AND attempt=?""",
                        (target.value, stamp if finish else None, result.metrics.duration_ms,
                         result.failure_kind.value if result.failure_kind else None,
                         self.events_store.redactor.text(result.error_summary)[:2_048]
                         if result.error_summary else None,
                         task_id, node_id, actual_attempt),
                    )
                    db.execute(
                        """INSERT INTO graph_node_results(task_id,node_id,attempt,idempotency_key,status,
                           result_json,policy_action,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (task_id, node_id, actual_attempt, attempt_row["idempotency_key"],
                         target.value, result_json,
                         policy_action.value if policy_action else None, stamp),
                    )
                if checkpoint_id is not None:
                    db.execute("""UPDATE task_node_attempts SET checkpoint_id=?
                        WHERE task_id=? AND node_id=? AND attempt=?""",
                        (checkpoint_id, task_id, node_id, actual_attempt))

                revision = expected_revision + 1
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                event_payload = {"from": source.value, "to": target.value, **dict(payload or {})}
                db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,attempt,state,
                       payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (task_id, sequence, revision, event_type, node_id, actual_attempt,
                     target.value, self._json(event_payload), stamp),
                )
                new_graph_status = (graph_status or NodeStatus.RUNNING).value
                db.execute(
                    """UPDATE task_graph_runs SET status=?,current_revision=?,updated_at=?
                       WHERE task_id=? AND current_revision=?""",
                    (new_graph_status, revision, stamp, task_id, expected_revision),
                )
        except ForgePilotError:
            raise
        except sqlite3.IntegrityError:
            raise ForgePilotError("graph_conflict", "Graph transition conflicts with durable state") from None
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Graph transition could not be saved") from None
        return self.snapshot(task_id)

    def approve(self, task_id: str, node_id: str, *, expected_revision: int,
                result: NodeResult, checkpoint_id: str | None = None,
                approval: Mapping[str, Any] | None = None) -> GraphSnapshot:
        stamp = _now()
        if result.status is not NodeStatus.SUCCEEDED:
            raise ValueError("Human approval result must succeed")
        result_json = self._result_json(result)
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute("SELECT current_revision FROM task_graph_runs WHERE task_id=?",
                                 (task_id,)).fetchone()
                node = db.execute(
                    "SELECT * FROM graph_node_states WHERE task_id=? AND node_id=?",
                    (task_id, node_id),
                ).fetchone()
                if run is None or node is None:
                    raise ForgePilotError("unknown_graph", "Graph run or node does not exist")
                if run[0] != expected_revision or node["status"] != NodeStatus.AWAITING_HUMAN.value:
                    raise ForgePilotError("graph_conflict", "Graph revision or human gate state changed")
                if approval is not None:
                    # A transport approval is published in the same transaction
                    # as the gate transition. A failed gate never leaves a grant.
                    from ..orchestration.analysis_review import AnalysisReviewStore
                    current = AnalysisReviewStore(self.events_store).get(task_id)
                    if any(approval[name] != getattr(current, name) for name in (
                            "task_spec_hash", "plan_hash", "scope_hash")):
                        raise ForgePilotError("mission_approval_conflict",
                            "TaskSpec, Plan or Scope changed; review the current approval package")
                    db.execute("""INSERT INTO approvals(approval_id,task_id,approval_type,status,
                        task_spec_artifact_id,task_spec_hash,plan_artifact_id,plan_hash,scope_hash,
                        approved_by,created_at) VALUES(?,?,'execution','approved',?,?,?,?,?,'user',?)""",
                        (approval["approval_id"], task_id, current.task_spec_artifact_id,
                         current.task_spec_hash, current.plan_artifact_id, current.plan_hash,
                         current.scope_hash, stamp))
                assert_transition(NodeStatus.AWAITING_HUMAN, NodeStatus.SUCCEEDED)
                db.execute(
                    """UPDATE graph_node_states SET status='succeeded',result_json=?,updated_at=?
                       WHERE task_id=? AND node_id=?""", (result_json, stamp, task_id, node_id))
                db.execute(
                    """UPDATE task_node_attempts SET status='succeeded',finished_at=?,duration_ms=?
                       WHERE task_id=? AND node_id=? AND attempt=?""",
                    (stamp, result.metrics.duration_ms, task_id, node_id, node["attempt"]),
                )
                db.execute(
                    """UPDATE graph_node_results SET status='succeeded',result_json=?
                       WHERE task_id=? AND node_id=? AND attempt=?""",
                    (result_json, task_id, node_id, node["attempt"]),
                )
                if checkpoint_id is not None:
                    db.execute("""UPDATE task_node_attempts SET checkpoint_id=?
                        WHERE task_id=? AND node_id=? AND attempt=?""",
                        (checkpoint_id, task_id, node_id, node["attempt"]))
                revision = expected_revision + 1
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                approval_payload = {"from": "awaiting_human", "to": "succeeded"}
                if approval is not None:
                    approval_payload.update({key: approval[key] for key in (
                        "approval_id", "task_spec_hash", "plan_hash", "scope_hash")})
                    approval_payload.update(decision="approved", _actor="user")
                db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,attempt,state,
                       payload_json,created_at) VALUES(?,?,?,?,?,?,'succeeded',?,?)""",
                    (task_id, sequence, revision, "human_gate_approved", node_id, node["attempt"],
                     self._json(approval_payload), stamp),
                )
                db.execute(
                    """UPDATE task_graph_runs SET status='running',current_revision=?,updated_at=?
                       WHERE task_id=? AND current_revision=?""",
                    (revision, stamp, task_id, expected_revision),
                )
        except ForgePilotError:
            raise
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Human gate decision could not be saved") from None
        return self.snapshot(task_id)

    def finish_graph(self, task_id: str, *, expected_revision: int,
                     status: NodeStatus) -> GraphSnapshot:
        if status not in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}:
            raise ValueError("Invalid terminal graph status")
        stamp = _now()
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute("SELECT current_revision,status FROM task_graph_runs WHERE task_id=?",
                                 (task_id,)).fetchone()
                if run is None:
                    raise ForgePilotError("unknown_graph", "Graph run does not exist")
                if run["current_revision"] != expected_revision:
                    raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
                revision = expected_revision + 1
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,state,payload_json,created_at)
                       VALUES(?,?,?,'graph_finished',?,?,?)""",
                    (task_id, sequence, revision, status.value,
                     self._json({"from": run["status"], "to": status.value}), stamp),
                )
                db.execute(
                    """UPDATE task_graph_runs SET status=?,current_revision=?,updated_at=?,finished_at=?
                       WHERE task_id=? AND current_revision=?""",
                    (status.value, revision, stamp, stamp, task_id, expected_revision),
                )
        except ForgePilotError:
            raise
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Graph completion could not be saved") from None
        return self.snapshot(task_id)

    def cancel(self, task_id: str, *, expected_revision: int, rejected_approval=None) -> GraphSnapshot:
        stamp = _now()
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute("SELECT current_revision,status FROM task_graph_runs WHERE task_id=?",
                                 (task_id,)).fetchone()
                if run is None:
                    raise ForgePilotError("unknown_graph", "Graph run does not exist")
                if run["current_revision"] != expected_revision:
                    raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
                if run["status"] == NodeStatus.CANCELLED.value:
                    return self.snapshot(task_id)
                if run["status"] in {NodeStatus.SUCCEEDED.value, NodeStatus.FAILED.value}:
                    raise ForgePilotError("invalid_state", "A finished graph cannot be cancelled")
                if rejected_approval is not None:
                    review = rejected_approval
                    db.execute("""INSERT INTO approvals(approval_id,task_id,approval_type,status,
                        task_spec_artifact_id,task_spec_hash,plan_artifact_id,plan_hash,scope_hash,approved_by,created_at)
                        VALUES(?,?,'execution','rejected',?,?,?,?,?,'user',?)""", (
                        review["approval_id"], task_id, review["task_spec_artifact_id"], review["task_spec_hash"],
                        review["plan_artifact_id"], review["plan_hash"], review["scope_hash"], stamp))
                revision = expected_revision
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                nodes = db.execute(
                    "SELECT * FROM graph_node_states WHERE task_id=? ORDER BY rowid", (task_id,)
                ).fetchall()
                for node in nodes:
                    source = NodeStatus(node["status"])
                    if source in TERMINAL_NODE_STATUSES:
                        continue
                    assert_transition(source, NodeStatus.CANCELLED)
                    revision += 1
                    sequence += 1
                    db.execute(
                        "UPDATE graph_node_states SET status='cancelled',updated_at=? WHERE task_id=? AND node_id=?",
                        (stamp, task_id, node["node_id"]),
                    )
                    if node["attempt"]:
                        db.execute(
                            """UPDATE task_node_attempts SET status='cancelled',finished_at=?,error_code='user_cancelled',
                               error_summary='Graph cancelled by user' WHERE task_id=? AND node_id=? AND attempt=?
                               AND finished_at IS NULL""",
                            (stamp, task_id, node["node_id"], node["attempt"]),
                        )
                    db.execute(
                        """INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,attempt,state,
                           payload_json,created_at) VALUES(?,?,?,?,?,?,'cancelled',?,?)""",
                        (task_id, sequence, revision, "node_state_changed", node["node_id"],
                         node["attempt"], self._json({"from": source.value, "to": "cancelled",
                                                      "failure_kind": "user_cancelled"}), stamp),
                    )
                revision += 1
                sequence += 1
                db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,state,payload_json,created_at)
                       VALUES(?,?,?,'graph_cancelled','cancelled',?,?)""",
                    (task_id, sequence, revision,
                     self._json({"from": run["status"], "to": "cancelled"}), stamp),
                )
                db.execute(
                    """UPDATE task_graph_runs SET status='cancelled',current_revision=?,updated_at=?,finished_at=?
                       WHERE task_id=? AND current_revision=?""",
                    (revision, stamp, stamp, task_id, expected_revision),
                )
                if rejected_approval is not None:
                    from ..mission_events import append_mission_event
                    append_mission_event(self.events_store, task_id, "approval.rejected", {
                        key: rejected_approval[key] for key in ("approval_id", "task_spec_hash", "plan_hash", "scope_hash")},
                        actor="user")
        except ForgePilotError:
            raise
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Graph cancellation could not be saved") from None
        return self.snapshot(task_id)

    def is_cancelled(self, task_id: str, node_id: str) -> bool:
        with self.events_store._lock:
            row = self.events_store._db.execute(
                """SELECT g.status,n.status FROM task_graph_runs g JOIN graph_node_states n
                   ON n.task_id=g.task_id WHERE g.task_id=? AND n.node_id=?""",
                (task_id, node_id),
            ).fetchone()
        return (row is None or row[0] == NodeStatus.CANCELLED.value
                or row[1] == NodeStatus.CANCELLED.value)

    def recovery_required(self, task_id: str) -> bool:
        return bool(self.events_store.missions.get_graph_run(task_id)["recovery_required"])

    def attempt_record(self, task_id: str, node_id: str, attempt: int):
        with self.events_store._lock:
            row = self.events_store._db.execute("""SELECT * FROM task_node_attempts
                WHERE task_id=? AND node_id=? AND attempt=?""", (task_id, node_id, attempt)).fetchone()
        return dict(row) if row else None

    def frozen_inputs(self, task_id: str, node_id: str, attempt: int):
        row = self.attempt_record(task_id, node_id, attempt)
        if row is None or row["input_artifacts_json"] is None:
            return None
        return tuple(tuple(item) for item in json.loads(row["input_artifacts_json"]))

    def workspace_record(self, task_id: str):
        run = self.events_store.missions.get_graph_run(task_id)
        with self.events_store._lock:
            rows = self.events_store._db.execute(
                "SELECT * FROM workspace_records WHERE task_id=?", (task_id,)).fetchall()
        if not rows:
            if run["workspace_id"] is not None:
                raise ForgePilotError("workspace_corrupted", "Graph Worktree record is missing")
            return None
        row = dict(rows[0])
        if run["workspace_id"] != row["workspace_id"]:
            raise ForgePilotError("workspace_corrupted", "Graph and Worktree records disagree")
        return row

    def reconcile_events(self, task_id: str) -> None:
        """Repair a missing event from a consistent durable projection, never execute a node.

        Normal publication is atomic. This also handles imported/older partial records;
        conflicting successful results remain an integrity error instead of a replay.
        """
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute("SELECT * FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone()
                if run is None:
                    raise ForgePilotError("unknown_graph", "Graph run does not exist")
                revision = max(run["current_revision"], db.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0])
                sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM graph_events WHERE task_id=?",
                                      (task_id,)).fetchone()[0]
                changed = False
                for node in db.execute("SELECT * FROM graph_node_states WHERE task_id=? ORDER BY node_id",
                                       (task_id,)).fetchall():
                    if node["status"] == "succeeded":
                        result = db.execute("""SELECT * FROM graph_node_results
                            WHERE task_id=? AND node_id=? AND attempt=?""",
                            (task_id, node["node_id"], node["attempt"])).fetchone()
                        attempt = db.execute("""SELECT status FROM task_node_attempts
                            WHERE task_id=? AND node_id=? AND attempt=?""",
                            (task_id, node["node_id"], node["attempt"])).fetchone()
                        if (not result or not attempt or result["status"] != "succeeded"
                                or attempt[0] != "succeeded" or result["result_json"] != node["result_json"]):
                            raise ForgePilotError("recovery_integrity", "Successful node records disagree")
                    last = db.execute("""SELECT state,attempt FROM graph_events
                        WHERE task_id=? AND node_id=? ORDER BY sequence DESC LIMIT 1""",
                        (task_id, node["node_id"])).fetchone()
                    if ((last and last[0] == node["status"] and last[1] == node["attempt"])
                            or (not last and node["status"] == "pending" and node["attempt"] == 0)):
                        continue
                    revision += 1
                    sequence += 1
                    changed = True
                    db.execute("""INSERT INTO graph_events(task_id,sequence,revision,event_type,
                        node_id,attempt,state,payload_json,created_at) VALUES(?,?,?,'node_reconciled',?,?,?,?,?)""",
                        (task_id, sequence, revision, node["node_id"], node["attempt"], node["status"],
                         self._json({"reason": "durable_projection", "to": node["status"]}), _now()))
                if changed:
                    db.execute("UPDATE task_graph_runs SET current_revision=?,updated_at=? WHERE task_id=?",
                               (revision, _now(), task_id))
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Recovery event reconciliation failed") from None

    def save_recovery_plan(self, task_id: str, *, expected_revision: int, plan: dict) -> None:
        try:
            with self.events_store._transaction():
                changed = self.events_store._db.execute("""UPDATE task_graph_runs SET
                    recovery_required=1,recovery_plan_json=? WHERE task_id=? AND current_revision=?
                    AND status NOT IN ('cancelled','failed','succeeded')""",
                    (json.dumps(plan, sort_keys=True, separators=(",", ":")), task_id, expected_revision)).rowcount
                if changed != 1:
                    raise ForgePilotError("graph_conflict", "Graph revision or recovery state changed")
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Recovery plan could not be saved") from None

    def apply_recovery(self, task_id: str, *, expected_revision: int, token: str,
                       changes: tuple[dict, ...]) -> GraphSnapshot:
        """The only audited exception to ordinary node transitions: confirmed recovery.

        A verified checkpoint can finish an abandoned attempt; retries receive a new
        attempt on the next claim. State, checkpoint links, confirmation and events
        commit together, so a duplicate confirmation cannot run or apply code twice.
        """
        try:
            with self.events_store._transaction():
                db = self.events_store._db
                run = db.execute("SELECT * FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone()
                saved = json.loads(run["recovery_plan_json"] or "{}") if run else {}
                if saved.get("token") == token and saved.get("confirmed"):
                    return self.snapshot(task_id)
                if (not run or run["current_revision"] != expected_revision or not run["recovery_required"]
                        or saved.get("token") != token or run["status"] in {"succeeded", "cancelled", "failed"}):
                    raise ForgePilotError("graph_conflict", "Recovery confirmation is stale")
                revision = expected_revision
                sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM graph_events WHERE task_id=?",
                                      (task_id,)).fetchone()[0]
                for change in changes:
                    node_id, target = change["node_id"], change["target"]
                    node = db.execute("SELECT * FROM graph_node_states WHERE task_id=? AND node_id=?",
                                      (task_id, node_id)).fetchone()
                    result = change.get("result")
                    encoded = self._result_json(result) if result else None
                    db.execute("""UPDATE graph_node_states SET status=?,result_json=?,updated_at=?
                        WHERE task_id=? AND node_id=?""", (target, encoded, _now(), task_id, node_id))
                    if result is not None and node["attempt"]:
                        attempt = self.attempt_record(task_id, node_id, node["attempt"])
                        db.execute("""UPDATE task_node_attempts SET status=?,finished_at=?,error_code=NULL,
                            error_summary=NULL WHERE task_id=? AND node_id=? AND attempt=?""",
                            (target, _now() if target == "succeeded" else None, task_id, node_id, node["attempt"]))
                        db.execute("""INSERT INTO graph_node_results(task_id,node_id,attempt,idempotency_key,
                            status,result_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,node_id,attempt)
                            DO UPDATE SET status=excluded.status,result_json=excluded.result_json""",
                            (task_id, node_id, node["attempt"], attempt["idempotency_key"], target, encoded, _now()))
                    if change.get("checkpoint_id"):
                        db.execute("""UPDATE task_node_attempts SET checkpoint_id=?
                            WHERE task_id=? AND node_id=? AND attempt=?""",
                            (change["checkpoint_id"], task_id, node_id, node["attempt"]))
                    revision += 1
                    sequence += 1
                    db.execute("""INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,
                        attempt,state,payload_json,created_at) VALUES(?,?,?,'node_recovery_confirmed',?,?,?,?,?)""",
                        (task_id, sequence, revision, node_id, node["attempt"], target,
                         self._json({"from": node["status"], "to": target, "action": change["action"]}), _now()))
                revision += 1
                sequence += 1
                saved["confirmed"] = True
                db.execute("""INSERT INTO graph_events(task_id,sequence,revision,event_type,payload_json,
                    created_at) VALUES(?,?,?,'recovery_confirmed',?,?)""",
                    (task_id, sequence, revision, self._json({"token": token}), _now()))
                has_gate = db.execute("SELECT 1 FROM graph_node_states WHERE task_id=? AND status='awaiting_human'",
                                     (task_id,)).fetchone()
                db.execute("""UPDATE task_graph_runs SET status=?,recovery_required=0,recovery_plan_json=?,
                    current_revision=?,updated_at=? WHERE task_id=?""",
                    ("awaiting_human" if has_gate else "running", json.dumps(saved), revision, _now(), task_id))
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Recovery confirmation could not be saved") from None
        return self.snapshot(task_id)


__all__ = ["GraphStore"]
