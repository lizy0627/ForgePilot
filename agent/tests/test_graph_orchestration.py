"""Fake-handler coverage for the serial, deterministic Graph Orchestrator."""
from __future__ import annotations

from collections import Counter

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.artifacts import create_artifact
from forgepilot.orchestration import (
    DefaultFailurePolicy, EdgeCondition, EdgeDefinition, FailureKind,
    GraphDefinition, GraphRunner, NodeDefinition, NodeMetrics, NodeResult,
    NodeStatus, NodeType, PolicyAction, TransitionError, validate_graph,
)
from forgepilot.orchestration.config import OrchestrationConfig, load_orchestration_config
from forgepilot.orchestration.graph import GraphValidationError
from forgepilot.orchestration.models import ALLOWED_TRANSITIONS, assert_transition, idempotency_key
from forgepilot.orchestration.node_handlers import HandlerRegistry, NodeExecutionError
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID, TASK_SPEC_ID, task_spec


def node(node_id, handler="ok", *, node_type=NodeType.DETERMINISTIC, dependencies=()):
    return NodeDefinition(node_id, node_type, handler, dependencies=dependencies)


def success(*, route=None, metrics=None):
    return NodeResult(NodeStatus.SUCCEEDED, decision_summary="fake success",
                      next_route=route, metrics=metrics or NodeMetrics())


def test_topology_is_stable_and_rejects_duplicates_missing_nodes_cycles_and_bad_types():
    graph = GraphDefinition("stable", 1, (
        node("root"), node("left", dependencies=("root",)), node("right"), node("last"),
    ), (
        EdgeDefinition("root", "right"), EdgeDefinition("left", "last"),
        EdgeDefinition("right", "last"),
    ))
    assert validate_graph(graph).topological_order == ("root", "left", "right", "last")

    with pytest.raises(GraphValidationError, match="duplicate node"):
        validate_graph(GraphDefinition("g", 1, (node("x"), node("x"))))
    with pytest.raises(GraphValidationError, match="missing dependencies"):
        validate_graph(GraphDefinition("g", 1, (node("x", dependencies=("absent",)),)))
    with pytest.raises(GraphValidationError, match="missing node"):
        validate_graph(GraphDefinition("g", 1, (node("x"),), (EdgeDefinition("x", "y"),)))
    with pytest.raises(GraphValidationError, match="cycle"):
        validate_graph(GraphDefinition("g", 1, (node("a"), node("b")), (
            EdgeDefinition("a", "b"), EdgeDefinition("b", "a"),
        )))
    with pytest.raises(ValueError):
        NodeDefinition("x", "shell", "bad")


def test_state_machine_has_all_documented_states_and_only_central_transitions():
    assert {state.value for state in NodeStatus} == {
        "pending", "ready", "running", "awaiting_human", "succeeded", "failed",
        "interrupted", "retryable", "skipped", "cancelled",
    }
    assert set(ALLOWED_TRANSITIONS) == set(NodeStatus)
    assert_transition(NodeStatus.PENDING, NodeStatus.READY)
    assert_transition(NodeStatus.RUNNING, NodeStatus.AWAITING_HUMAN)
    assert_transition(NodeStatus.INTERRUPTED, NodeStatus.RETRYABLE)
    assert_transition(NodeStatus.RETRYABLE, NodeStatus.READY)
    with pytest.raises(TransitionError):
        assert_transition(NodeStatus.SUCCEEDED, NodeStatus.RUNNING)
    with pytest.raises(TransitionError):
        assert_transition(NodeStatus.PENDING, NodeStatus.SUCCEEDED)


def test_serial_run_metrics_order_and_duplicate_call_are_durable(tmp_path):
    path = tmp_path / "serial.sqlite3"
    calls = []
    registry = HandlerRegistry({
        "first": lambda context: calls.append(context.node.node_id) or success(
            metrics=NodeMetrics(model_calls=1, tool_calls=2, total_tokens=33)),
        "second": lambda context: calls.append(context.node.node_id) or success(),
    })
    graph = GraphDefinition("serial", 1, (
        node("one", "first"), node("two", "second", dependencies=("one",)),
    ))
    store = SqliteEventStore(path)
    runner = GraphRunner(store, registry)
    result = runner.run("task-serial", graph)
    assert result.status is NodeStatus.SUCCEEDED
    assert calls == ["one", "two"]
    first = next(item for item in result.nodes if item.node_id == "one")
    assert first.result.metrics.model_calls == 1
    assert first.result.metrics.tool_calls == 2
    assert first.result.metrics.total_tokens == 33
    before = runner.events("task-serial")
    assert [event.sequence for event in before] == list(range(1, len(before) + 1))
    assert [event.revision for event in before] == list(range(1, len(before) + 1))
    assert [event.event_type for event in before] == [
        "graph_created", "node_ready", "node_started", "node_finished",
        "node_ready", "node_started", "node_finished", "graph_finished",
    ]
    assert runner.run("task-serial", graph) == result
    assert calls == ["one", "two"] and runner.events("task-serial") == before
    store.close()

    reopened = SqliteEventStore(path)
    try:
        durable = GraphRunner(reopened, registry)
        assert durable.run("task-serial", graph) == result
        assert durable.events("task-serial") == before
        assert calls == ["one", "two"]
    finally:
        reopened.close()


def test_same_graph_identity_cannot_hide_definition_changes():
    original = GraphDefinition("identity", 1, (node("one", "ok"),))
    changed = GraphDefinition("identity", 1, (node("one", "different"),))
    store = SqliteEventStore()
    try:
        runner = GraphRunner(store, HandlerRegistry({
            "ok": lambda context: success(), "different": lambda context: success(),
        }))
        runner.run("task-identity", original)
        with pytest.raises(ForgePilotError, match="definition"):
            runner.run("task-identity", changed)
    finally:
        store.close()


def test_conditional_routes_skip_nonmatching_branch():
    calls = []
    handlers = HandlerRegistry({
        "route": lambda context: calls.append("route") or success(route="yes"),
        "yes": lambda context: calls.append("yes") or success(),
        "no": lambda context: calls.append("no") or success(),
    })
    graph = GraphDefinition("conditional", 1, (
        node("choose", "route", node_type=NodeType.CONDITIONAL),
        node("yes", "yes"), node("no", "no"),
    ), (
        EdgeDefinition("choose", "yes", EdgeCondition(route="yes")),
        EdgeDefinition("choose", "no", EdgeCondition(route="no")),
    ))
    store = SqliteEventStore()
    try:
        result = GraphRunner(store, handlers).run("task-condition", graph)
        assert calls == ["route", "yes"]
        assert {item.node_id: item.status for item in result.nodes} == {
            "choose": NodeStatus.SUCCEEDED, "yes": NodeStatus.SUCCEEDED,
            "no": NodeStatus.SKIPPED,
        }
    finally:
        store.close()


def test_human_gate_pauses_then_approval_resumes_with_revision_check():
    calls = Counter()
    handlers = HandlerRegistry({
        "gate": lambda context: calls.update(["gate"]) or NodeResult(
            NodeStatus.AWAITING_HUMAN, decision_summary="approval required"),
        "deliver": lambda context: calls.update(["deliver"]) or success(),
    })
    graph = GraphDefinition("human", 1, (
        node("approval", "gate", node_type=NodeType.HUMAN_GATE),
        node("delivery", "deliver", node_type=NodeType.DELIVERY,
             dependencies=("approval",)),
    ))
    store = SqliteEventStore()
    runner = GraphRunner(store, handlers)
    try:
        paused = runner.run("task-human", graph)
        assert paused.status is NodeStatus.AWAITING_HUMAN
        assert calls == Counter(gate=1)
        with pytest.raises(ForgePilotError, match="revision"):
            runner.approve_human("task-human", graph, "approval",
                                 expected_revision=paused.revision - 1)
        done = runner.approve_human("task-human", graph, "approval",
                                    expected_revision=paused.revision)
        assert done.status is NodeStatus.SUCCEEDED
        assert calls == Counter(gate=1, deliver=1)
        assert "human_gate_approved" in [event.event_type for event in runner.events("task-human")]
    finally:
        store.close()


def test_retry_is_explicit_and_attempt_changes_idempotency_key():
    attempts = []

    def flaky(context):
        attempts.append(context.attempt)
        if context.attempt == 1:
            raise NodeExecutionError(FailureKind.MODEL_TRANSIENT, "temporary")
        return success()

    graph = GraphDefinition("retry", 1, (node("flaky", "flaky", node_type=NodeType.AGENT),))
    store = SqliteEventStore()
    runner = GraphRunner(store, HandlerRegistry({"flaky": flaky}))
    try:
        paused = runner.run("task-retry", graph)
        current = paused.nodes[0]
        assert paused.status is NodeStatus.RETRYABLE
        assert current.status is NodeStatus.RETRYABLE and current.attempt == 1
        assert current.result.status is NodeStatus.RETRYABLE and current.result.retryable
        done = runner.retry("task-retry", graph, "flaky", expected_revision=paused.revision)
        assert done.status is NodeStatus.SUCCEEDED and done.nodes[0].attempt == 2
        assert attempts == [1, 2]
        rows = store._db.execute(
            "SELECT attempt,idempotency_key,status FROM task_node_attempts ORDER BY attempt"
        ).fetchall()
        assert [row["status"] for row in rows] == ["retryable", "succeeded"]
        assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
    finally:
        store.close()


def test_cancel_is_atomic_idempotent_and_stale_revision_is_rejected():
    graph = GraphDefinition("cancel", 1, (
        node("gate", "gate", node_type=NodeType.HUMAN_GATE),
        node("later", "ok", dependencies=("gate",)),
    ))
    store = SqliteEventStore()
    runner = GraphRunner(store, HandlerRegistry({
        "gate": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN),
        "ok": lambda context: success(),
    }))
    try:
        paused = runner.run("task-cancel", graph)
        with pytest.raises(ForgePilotError, match="revision"):
            runner.cancel("task-cancel", expected_revision=paused.revision - 1)
        cancelled = runner.cancel("task-cancel", expected_revision=paused.revision)
        assert cancelled.status is NodeStatus.CANCELLED
        assert {node.status for node in cancelled.nodes} == {NodeStatus.CANCELLED}
        before = runner.events("task-cancel")
        assert runner.cancel("task-cancel", expected_revision=cancelled.revision) == cancelled
        assert runner.events("task-cancel") == before
    finally:
        store.close()


@pytest.mark.parametrize("failure,action", [
    (FailureKind.MODEL_TRANSIENT, PolicyAction.RETRY),
    (FailureKind.MODEL_RATE_LIMIT, PolicyAction.RETRY),
    (FailureKind.MODEL_CONTRACT_VIOLATION, PolicyAction.REPAIR),
    (FailureKind.TOOL_TRANSIENT, PolicyAction.RETRY),
    (FailureKind.TOOL_POLICY_VIOLATION, PolicyAction.STOP),
    (FailureKind.REPOSITORY_CONFLICT, PolicyAction.PAUSE),
    (FailureKind.WORKSPACE_CORRUPTED, PolicyAction.STOP),
    (FailureKind.BUILD_CONFIGURATION, PolicyAction.PAUSE),
    (FailureKind.BUILD_CODE_ERROR, PolicyAction.REPAIR),
    (FailureKind.TEST_FAILURE, PolicyAction.REPAIR),
    (FailureKind.TEST_INTEGRITY_VIOLATION, PolicyAction.STOP),
    (FailureKind.HIDDEN_ORACLE_FAILURE, PolicyAction.REPAIR),
    (FailureKind.STORAGE_FAILURE, PolicyAction.STOP),
    (FailureKind.USER_CANCELLED, PolicyAction.STOP),
    (FailureKind.APPLICATION_INTERRUPTED, PolicyAction.PAUSE),
])
def test_all_documented_failures_have_an_explicit_policy(failure, action):
    assert DefaultFailurePolicy().action_for(failure, attempt=1) is action


def test_repair_is_reported_but_not_executed_and_parallel_boundaries_are_deterministic():
    for task_id, graph in (
        ("task-repair", GraphDefinition("repair", 1, (node("x", "bad"),))),
        ("task-fanout", GraphDefinition("fanout", 1, (
            node("x", "unused", node_type=NodeType.PARALLEL_FANOUT),))),
        ("task-join", GraphDefinition("join", 1, (
            node("x", "unused", node_type=NodeType.JOIN),))),
    ):
        handlers = HandlerRegistry({
            "bad": lambda context: NodeResult(
                NodeStatus.FAILED, failure_kind=FailureKind.TEST_FAILURE,
                error_summary="controlled failure"),
        })
        store = SqliteEventStore()
        try:
            runner = GraphRunner(store, handlers)
            result = runner.run(task_id, graph)
            assert result.status is (NodeStatus.FAILED if task_id == "task-repair" else NodeStatus.SUCCEEDED)
            event = next(event for event in runner.events(task_id)
                         if event.event_type == "node_finished")
            expected = "repair" if task_id == "task-repair" else None
            assert event.payload["policy_action"] == expected
        finally:
            store.close()


def test_idempotency_key_covers_every_required_boundary():
    base = dict(task_id="task-key", node_id="node", attempt=1,
                input_artifact_hashes=("1" * 64,), agent_definition_hash="2" * 64,
                policy_hash="3" * 64)
    original = idempotency_key(**base)
    variants = [
        {**base, "task_id": "task-other"}, {**base, "node_id": "other"},
        {**base, "attempt": 2}, {**base, "input_artifact_hashes": ("4" * 64,)},
        {**base, "agent_definition_hash": "5" * 64},
        {**base, "policy_hash": "6" * 64},
    ]
    assert all(idempotency_key(**variant) != original for variant in variants)
    assert idempotency_key(**{**base, "input_artifact_hashes": ("1" * 64, "4" * 64)}) \
        == idempotency_key(**{**base, "input_artifact_hashes": ("4" * 64, "1" * 64)})


def test_runner_reuses_artifact_store_and_persists_input_hash_boundary(tmp_path):
    path = tmp_path / "artifact-input.sqlite3"
    store = SqliteEventStore(path, artifact_root=tmp_path / "artifacts")
    artifact = create_artifact(
        task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
        producer_node="draft", content=task_spec(),
    )
    store.artifacts.put(artifact)
    seen = []

    def handler(context):
        seen.append(context.artifacts.get(TASK_SPEC_ID).content_sha256)
        assert context.input_artifact_ids == (TASK_SPEC_ID,)
        assert context.input_artifact_hashes == (artifact.content_sha256,)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(TASK_SPEC_ID,))

    definition = NodeDefinition(
        "agent", NodeType.AGENT, "agent", agent_id="implementer.v1",
        agent_definition_hash="c" * 64,
        settings={"recovery_role": "deterministic"},
    )
    graph = GraphDefinition("artifact-boundary", 1, (definition,))
    runner = GraphRunner(store, HandlerRegistry({"agent": handler}))
    result = runner.run(TASK_ID, graph, input_artifact_ids=(TASK_SPEC_ID,))
    row = store._db.execute("SELECT idempotency_key,input_hash FROM task_node_attempts").fetchone()
    expected = idempotency_key(
        task_id=TASK_ID, node_id="agent", attempt=1,
        input_artifact_hashes=(artifact.content_sha256,),
        agent_definition_hash="c" * 64, policy_hash=runner.policy.definition_hash,
    )
    assert result.status is NodeStatus.SUCCEEDED
    assert row["idempotency_key"] == expected and len(row["input_hash"]) == 64
    store.close()

    reopened = SqliteEventStore(path, artifact_root=tmp_path / "artifacts")
    try:
        GraphRunner(reopened, HandlerRegistry({"agent": handler})).run(TASK_ID, graph)
        assert seen == [artifact.content_sha256]
    finally:
        reopened.close()


def test_default_config_is_legacy_and_multi_agent_flag_is_dormant(tmp_path):
    assert OrchestrationConfig().mode == "legacy"
    assert load_orchestration_config().mode == "legacy"
    path = tmp_path / "forgepilot.toml"
    path.write_text('[orchestration]\nmode = "multi_agent"\n', encoding="utf-8")
    assert load_orchestration_config(path.resolve()).mode == "multi_agent"
    with pytest.raises(ValueError):
        OrchestrationConfig("unsafe")
