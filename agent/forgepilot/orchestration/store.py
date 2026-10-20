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
        return json.dumps(self.events_store.redactor.data(dict(value)), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _result_json(self, result: NodeResult) -> str:
        return json.dumps(self.events_store.redactor.data(result.to_dict()), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def create_run(self, task_id: str, graph: ValidatedGraph, *,
                   inputs: tuple[tuple[str, str], ...] = ()) -> GraphSnapshot:
        stamp = _now()
        try:
            with self.events_store._transaction():
                self.events_store._db.execute(
                    """INSERT INTO task_graph_runs(task_id,graph_id,graph_version,status,current_revision,
                       created_at,updated_at,definition_hash) VALUES(?,?,?,'pending',1,?,?,?)""",
                    (task_id, graph.definition.graph_id, graph.definition.version, stamp, stamp,
                     graph.definition.definition_hash),
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
                   policy_action: PolicyAction | None = None) -> GraphSnapshot:
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
                           idempotency_key,input_hash,started_at) VALUES(?,?,?,?,?,'running',?,?,?)""",
                        (task_id, node_id, attempt, node["node_type"], agent_id,
                         idempotency_key, input_hash, stamp),
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
                result: NodeResult) -> GraphSnapshot:
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
                revision = expected_revision + 1
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                db.execute(
                    """INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,attempt,state,
                       payload_json,created_at) VALUES(?,?,?,?,?,?,'succeeded',?,?)""",
                    (task_id, sequence, revision, "human_gate_approved", node_id, node["attempt"],
                     self._json({"from": "awaiting_human", "to": "succeeded"}), stamp),
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

    def cancel(self, task_id: str, *, expected_revision: int) -> GraphSnapshot:
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


__all__ = ["GraphStore"]
