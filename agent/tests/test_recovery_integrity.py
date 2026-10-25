"""Recovery must retain its frozen authority and require a fresh human decision."""
import json
from pathlib import Path

import pytest

from forgepilot.artifacts import ChangeSet, create_artifact
import forgepilot.artifacts.sqlite_store as artifact_sqlite
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeStatus, NodeType, validate_graph,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.store import GraphStore
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID, TASK_SPEC_ID, all_contents, plan, task_spec
from test_recovery import Crash, ok, writer_case


def test_changed_frozen_inputs_block_recovery_even_when_remaining_artifacts_are_valid(tmp_path):
    database = tmp_path / "frozen.sqlite3"
    store = SqliteEventStore(database, artifact_root=tmp_path / "artifacts")
    spec = create_artifact(
        task_id=TASK_ID, producer="orchestrator.v1", producer_node="input", content=task_spec(),
    )
    store.artifacts.put(spec)
    seen = []

    def handler(context):
        seen.append(context.input_artifact_ids)
        if context.attempt == 1:
            raise Crash()
        return ok()

    graph = GraphDefinition("frozen-recovery", 1, (
        NodeDefinition("work", NodeType.AGENT, "work", settings={"recovery_role": "orchestrator"}),
    ))
    registry = HandlerRegistry({"work": handler})
    runner = GraphRunner(store, registry)
    with pytest.raises(Crash):
        runner.run(TASK_ID, graph, input_artifact_ids=(spec.artifact_id,))
    store._db.execute("UPDATE task_node_attempts SET input_artifacts_json='[]'")
    store.close()
    reopened = SqliteEventStore(database, artifact_root=tmp_path / "artifacts")
    try:
        automatic = json.loads(reopened.missions.get_graph_run(TASK_ID)["recovery_plan_json"])
        assert automatic["blocked_reason"] == "recovery_integrity" and not automatic["token"]
        recovered = GraphRunner(reopened, registry)
        assert recovered.run(TASK_ID, graph).nodes[0].status is NodeStatus.INTERRUPTED
        recovery = recovered.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason == "recovery_integrity" and not recovery.token
        with pytest.raises(ForgePilotError):
            recovered.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                       expected_revision=recovery.revision)
        assert seen == [(spec.artifact_id,)]
        assert recovered.store.snapshot(TASK_ID).nodes[0].attempt == 1
    finally:
        reopened.close()


def test_matching_changeset_for_another_plan_cannot_be_adopted(tmp_path):
    store, runner, graph, backend, record, calls = writer_case(tmp_path)
    try:
        other_plan = create_artifact(
            task_id=TASK_ID, producer="orchestrator.v1", producer_node="other_plan",
            parent_artifact_ids=(TASK_SPEC_ID,),
            content=plan().model_copy(update={"summary": "A different, unfrozen execution plan."}),
        )
        store.artifacts.put(other_plan)
        change = create_artifact(
            task_id=TASK_ID, producer="implementer.v1", producer_node="write",
            parent_artifact_ids=(other_plan.artifact_id,),
            content=all_contents()[3].model_copy(
                update={"execution_plan_artifact_id": other_plan.artifact_id}),
        )
        store.artifacts.put(change)
        assert other_plan.artifact_id not in dict(runner.store.frozen_inputs(TASK_ID, "write", 1))
        before = backend.inspect(record)
        checkpoint_count = backend.checkpoint_count
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason == "recovery_integrity" and not recovery.token
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                    expected_revision=recovery.revision)
        assert calls == [1] and backend.restore_count == 0
        assert backend.checkpoint_count == checkpoint_count and backend.inspect(record) == before
        assert runner.store.snapshot(TASK_ID).nodes[1].status is NodeStatus.RUNNING
    finally:
        store.close()


@pytest.mark.parametrize("external", [False, True], ids=["inline", "external"])
def test_artifact_tampering_blocks_recovery_without_worktree_side_effects(tmp_path, monkeypatch, external):
    if external:
        monkeypatch.setattr(artifact_sqlite, "INLINE_LIMIT_BYTES", 1)
    store, runner, graph, backend, record, calls = writer_case(tmp_path, with_changes=True)
    try:
        change = next(item for item in store.artifacts.list_for_task(TASK_ID)
                      if isinstance(item.content, ChangeSet))
        if external:
            target = tmp_path / "artifacts" / TASK_ID / "artifacts" / f"{change.artifact_id}.json"
            payload = target.read_bytes()
            target.write_bytes(payload[:-1] + b" ")
        else:
            store._db.execute("UPDATE artifacts SET inline_json=? WHERE artifact_id=?",
                              (b"{}", change.artifact_id))
        before = backend.inspect(record)
        checkpoint_count = backend.checkpoint_count
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason and not recovery.token
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                    expected_revision=recovery.revision)
        assert calls == [1] and backend.restore_count == 0
        assert backend.checkpoint_count == checkpoint_count and backend.inspect(record) == before
    finally:
        store.close()


def _ready_graph(database: Path):
    store = SqliteEventStore(database)
    graph = GraphDefinition("between-nodes", 1, (NodeDefinition("work", NodeType.DETERMINISTIC, "work"),))
    graph_store = GraphStore(store)
    created = graph_store.create_run(TASK_ID, validate_graph(graph))
    ready = graph_store.transition(
        TASK_ID, "work", expected_revision=created.revision,
        source=NodeStatus.PENDING, target=NodeStatus.READY,
    )
    return store, graph, ready


def test_restart_between_nodes_waits_for_confirmation_before_claiming_ready_node(tmp_path):
    database = tmp_path / "between-nodes.sqlite3"
    store, graph, _ = _ready_graph(database)
    assert not store._db.execute("SELECT 1 FROM task_node_attempts").fetchone()
    store.close()
    reopened = SqliteEventStore(database)
    calls = []
    try:
        automatic = json.loads(reopened.missions.get_graph_run(TASK_ID)["recovery_plan_json"])
        assert automatic["requires_confirmation"] and automatic["token"]
        assert automatic["blocked_reason"] is None
        recovered = GraphRunner(reopened, HandlerRegistry({"work": lambda context: calls.append(1) or ok()}))
        waiting = recovered.run(TASK_ID, graph)
        assert waiting.nodes[0].status is NodeStatus.READY
        assert recovered.store.recovery_required(TASK_ID) and not calls
        assert not reopened._db.execute("SELECT 1 FROM task_node_attempts").fetchone()
        recovery = recovered.prepare_recovery(TASK_ID, graph)
        assert recovery.requires_confirmation and not recovery.blocked_reason
        assert recovered.run(TASK_ID, graph).nodes[0].status is NodeStatus.READY and not calls
        recovered.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                   expected_revision=recovery.revision)
        assert not calls
        assert recovered.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
        assert calls == [1]
    finally:
        reopened.close()


def test_cancelled_graph_stays_cancelled_after_restart_and_cannot_be_recovered(tmp_path):
    database = tmp_path / "cancelled.sqlite3"
    store, graph, ready = _ready_graph(database)
    cancelled = GraphRunner(store, HandlerRegistry()).cancel(TASK_ID, expected_revision=ready.revision)
    assert cancelled.status is NodeStatus.CANCELLED
    store.close()
    reopened = SqliteEventStore(database)
    calls = []
    try:
        assert reopened.missions.get_graph_run(TASK_ID)["recovery_plan_json"] is None
        recovered = GraphRunner(reopened, HandlerRegistry({"work": lambda context: calls.append(1) or ok()}))
        assert recovered.run(TASK_ID, graph).status is NodeStatus.CANCELLED
        recovery = recovered.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason == "terminal_graph" and not recovery.requires_confirmation
        assert not recovery.token and recovery.actions == ()
        with pytest.raises(ForgePilotError):
            recovered.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                       expected_revision=recovery.revision)
        assert recovered.run(TASK_ID, graph).status is NodeStatus.CANCELLED
        assert not reopened._db.execute("SELECT 1 FROM task_node_attempts").fetchone()
        assert not calls
    finally:
        reopened.close()
