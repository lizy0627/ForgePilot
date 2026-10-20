"""Crash recovery and atomic publication evidence for graph persistence."""
from __future__ import annotations

import sqlite3

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeResult, NodeStatus, NodeType,
    validate_graph,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.store import GraphStore
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore


def graph():
    return GraphDefinition("persistent", 1, (
        NodeDefinition("work", NodeType.DETERMINISTIC, "ok"),
    ))


def test_reopen_marks_running_attempt_interrupted_and_explicit_retry_resumes(tmp_path):
    path = tmp_path / "recovery.sqlite3"
    first = SqliteEventStore(path)
    durable = GraphStore(first)
    definition = validate_graph(graph())
    created = durable.create_run("task-recovery", definition)
    ready = durable.transition(
        "task-recovery", "work", expected_revision=created.revision,
        source=NodeStatus.PENDING, target=NodeStatus.READY,
    )
    durable.transition(
        "task-recovery", "work", expected_revision=ready.revision,
        source=NodeStatus.READY, target=NodeStatus.RUNNING,
        attempt=1, idempotency_key="a" * 64, input_hash="b" * 64,
    )
    first.close()

    reopened = SqliteEventStore(path)
    try:
        runner = GraphRunner(reopened, HandlerRegistry({
            "ok": lambda context: NodeResult(NodeStatus.SUCCEEDED),
        }))
        recovered = runner.store.snapshot("task-recovery")
        assert recovered.status is NodeStatus.INTERRUPTED
        assert recovered.nodes[0].status is NodeStatus.INTERRUPTED
        attempt = reopened._db.execute("SELECT * FROM task_node_attempts").fetchone()
        assert attempt["status"] == "interrupted"
        assert attempt["error_code"] == "application_interrupted"
        recovery_event = runner.events("task-recovery")[-1]
        assert recovery_event.state is NodeStatus.INTERRUPTED
        assert recovery_event.payload["failure_kind"] == "application_interrupted"

        done = runner.retry(
            "task-recovery", graph(), "work", expected_revision=recovered.revision)
        assert done.status is NodeStatus.SUCCEEDED
        assert done.nodes[0].attempt == 2
    finally:
        reopened.close()


def test_event_insert_failure_rolls_back_state_and_attempt(tmp_path):
    store = SqliteEventStore(tmp_path / "atomic.sqlite3")
    durable = GraphStore(store)
    created = durable.create_run("task-atomic", validate_graph(graph()))
    store._db.execute("""CREATE TRIGGER reject_graph_event BEFORE INSERT ON graph_events
        WHEN NEW.event_type='node_ready' BEGIN SELECT RAISE(ABORT,'injected'); END""")
    runner = GraphRunner(store, HandlerRegistry({
        "ok": lambda context: NodeResult(NodeStatus.SUCCEEDED),
    }))
    try:
        with pytest.raises(ForgePilotError):
            runner.run("task-atomic", graph())
        saved = durable.snapshot("task-atomic")
        assert saved == created
        assert [event.event_type for event in durable.graph_events("task-atomic")] == ["graph_created"]
        assert store._db.execute("SELECT COUNT(*) FROM task_node_attempts").fetchone()[0] == 0
    finally:
        store.close()

def test_result_is_queryable_only_after_state_and_event_commit(tmp_path, monkeypatch):
    store = SqliteEventStore(tmp_path / "publication.sqlite3")
    observed = []

    def fake(context):
        observed.append(("handler", context.attempt))
        return NodeResult(NodeStatus.SUCCEEDED)

    runner = GraphRunner(store, HandlerRegistry({"ok": fake}))
    real_snapshot = runner.store.snapshot

    def snapshot(task_id):
        value = real_snapshot(task_id)
        if value.nodes and value.nodes[0].status is NodeStatus.SUCCEEDED:
            events = runner.events(task_id)
            assert any(event.event_type == "node_finished" for event in events)
            assert store._db.execute(
                "SELECT status FROM graph_node_results WHERE task_id=?", (task_id,)
            ).fetchone()[0] == "succeeded"
            observed.append(("published", value.revision))
        return value

    monkeypatch.setattr(runner.store, "snapshot", snapshot)
    try:
        result = runner.run("task-publication", graph())
        assert result.status is NodeStatus.SUCCEEDED
        assert observed[0] == ("handler", 1)
        assert any(item[0] == "published" for item in observed[1:])
    finally:
        store.close()


def test_node_result_summaries_are_redacted_before_durable_publication(tmp_path):
    secret = "private-graph-secret"
    store = SqliteEventStore(tmp_path / "redacted.sqlite3", redactor=Redactor((secret,)))
    runner = GraphRunner(store, HandlerRegistry({
        "ok": lambda context: NodeResult(
            NodeStatus.SUCCEEDED, decision_summary=f"result contains {secret}"),
    }))
    try:
        result = runner.run("task-redacted", graph())
        assert result.nodes[0].result.decision_summary == "result contains [REDACTED]"
        persisted = " ".join(row[0] for row in store._db.execute(
            "SELECT result_json FROM graph_node_states UNION ALL SELECT result_json FROM graph_node_results"
        ).fetchall())
        assert secret not in persisted
    finally:
        store.close()
