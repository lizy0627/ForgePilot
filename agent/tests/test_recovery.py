"""Fault injection across node publication, confirmation and Worktree boundaries."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeResult, NodeStatus, NodeType, validate_graph,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.store import GraphStore
from forgepilot.orchestration.workspace import StrictFakeWorkspaceBackend, WorkspaceSnapshot, SimulatedWorkspaceCrash
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID, TASK_SPEC_ID, PLAN_ID, HASH_A, HASH_B, all_contents, plan, task_spec


class Crash(BaseException):
    pass


def ok(*ids):
    return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=tuple(ids))


def confirm(runner, graph):
    recovery = runner.prepare_recovery(TASK_ID, graph)
    assert not recovery.blocked_reason, recovery
    return runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)


def writer_case(tmp_path, *, with_changes=False):
    backend = StrictFakeWorkspaceBackend()
    store = SqliteEventStore(tmp_path / "recovery.sqlite3", artifact_root=tmp_path / "artifacts")
    spec = create_artifact(task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
                           producer_node="spec", content=task_spec())
    execution = create_artifact(task_id=TASK_ID, artifact_id=PLAN_ID, producer="orchestrator.v1",
                                producer_node="plan", parent_artifact_ids=(TASK_SPEC_ID,), content=plan())
    store.artifacts.put(spec)
    store.artifacts.put(execution)
    graph = GraphDefinition("writer-recovery", 1, (
        NodeDefinition("spec", NodeType.AGENT, "spec"),
        NodeDefinition("write", NodeType.AGENT, "writer", dependencies=("spec",),
                       settings={"recovery_role": "writer"}),
        NodeDefinition("wait", NodeType.HUMAN_GATE, "wait", dependencies=("write",)),
    ))
    created = GraphStore(store).create_run(TASK_ID, validate_graph(graph), inputs=((PLAN_ID, execution.content_sha256),))
    record = store.missions.save_workspace(workspace_id="worktree-1", task_id=TASK_ID,
        repository_path="D:/fake/repo", worktree_path="D:/fake/worktree", base_ref="main",
        status="active", head_commit="a" * 40)
    store.missions.update_graph_run(TASK_ID, previous_revision=created.revision, workspace_id="worktree-1")
    backend.register(WorkspaceSnapshot("worktree-1", TASK_ID, "a" * 40, {"src/window.cpp": HASH_A}), record)
    calls = []

    def writer(context):
        calls.append(context.attempt)
        snapshot = backend.inspect(record)
        backend.set_snapshot(replace(snapshot, file_hashes={"src/window.cpp": HASH_B}))
        if with_changes:
            change = create_artifact(task_id=TASK_ID, producer="implementer.v1", producer_node="write",
                                     parent_artifact_ids=(PLAN_ID,), content=all_contents()[3])
            store.artifacts.put(change)
        raise Crash()

    registry = HandlerRegistry({"spec": lambda context: ok(TASK_SPEC_ID), "writer": writer,
                                "wait": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN)})
    runner = GraphRunner(store, registry, workspace_backend=backend)
    with pytest.raises(Crash):
        runner.run(TASK_ID, graph)
    return store, runner, graph, backend, record, calls


def test_writer_without_changeset_waits_for_confirmation_then_rolls_back(tmp_path):
    store, runner, graph, backend, record, calls = writer_case(tmp_path)
    try:
        assert runner.run(TASK_ID, graph).nodes[1].status is NodeStatus.RUNNING
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.checks == ("sqlite", "worktree_record", "head", "checkpoint", "file_hash", "artifact_hash")
        assert recovery.actions[0]["action"] == "rollback_writer"
        assert backend.restore_count == 0 and calls == [1]
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token="invalid", expected_revision=recovery.revision)
        resumed = runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert resumed.nodes[1].status is NodeStatus.READY
        assert backend.inspect(record).file_hashes["src/window.cpp"] == HASH_A
        assert backend.restore_count == 1 and calls == [1]
        assert runner.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                      expected_revision=recovery.revision) == resumed
        assert backend.restore_count == 1
    finally:
        store.close()


def test_changeset_written_before_result_is_adopted_without_reapplying(tmp_path):
    store, runner, graph, backend, record, calls = writer_case(tmp_path, with_changes=True)
    try:
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.actions[0]["action"] == "adopt_changes"
        saved = runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert saved.nodes[1].status is NodeStatus.SUCCEEDED
        assert calls == [1] and backend.restore_count == 0
        assert runner.checkpoints.latest(TASK_ID).checkpoint_type == "implementation"
        assert runner.run(TASK_ID, graph).status is NodeStatus.AWAITING_HUMAN
        assert calls == [1]
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["unknown", "head", "files", "artifact", "missing_record"])
def test_unverifiable_writer_recovery_is_blocked(tmp_path, mutation):
    store, runner, graph, backend, record, calls = writer_case(tmp_path, with_changes=True)
    try:
        current = backend.inspect(record)
        if mutation == "unknown":
            backend.set_snapshot(replace(current, known=False))
        elif mutation == "head":
            backend.set_snapshot(replace(current, head_commit="f" * 40))
        elif mutation == "files":
            backend.set_snapshot(replace(current, file_hashes={**current.file_hashes, "unowned.txt": HASH_A}))
        elif mutation == "artifact":
            store._db.execute("UPDATE artifacts SET inline_json=? WHERE artifact_id=?", (b"{}", PLAN_ID))
        else:
            store._db.execute("DELETE FROM workspace_records")
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason and not recovery.token
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        runner.run(TASK_ID, graph) if mutation != "artifact" else None
        assert calls == [1] and backend.restore_count == 0
    finally:
        store.close()


def test_changed_files_after_inspection_invalidate_confirmation(tmp_path):
    store, runner, graph, backend, record, _ = writer_case(tmp_path)
    try:
        recovery = runner.prepare_recovery(TASK_ID, graph)
        backend.set_snapshot(replace(backend.inspect(record), file_hashes={"src/window.cpp": "c" * 64}))
        with pytest.raises(ForgePilotError, match="changed"):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert backend.restore_count == 0
    finally:
        store.close()


def test_restore_completed_before_process_dies_converges_after_new_confirmation(tmp_path):
    store, runner, graph, backend, record, calls = writer_case(tmp_path)
    recovery = runner.prepare_recovery(TASK_ID, graph)
    backend.fail_after_next_restore()
    with pytest.raises(SimulatedWorkspaceCrash):
        runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
    assert backend.restore_count == 1
    store.close()
    reopened = SqliteEventStore(tmp_path / "recovery.sqlite3", artifact_root=tmp_path / "artifacts")
    try:
        recovered = GraphRunner(reopened, runner.handlers, workspace_backend=backend)
        assert recovered.run(TASK_ID, graph).nodes[1].status is NodeStatus.INTERRUPTED
        confirm(recovered, graph)
        assert backend.restore_count == 1 and calls == [1]
        assert backend.inspect(record).file_hashes["src/window.cpp"] == HASH_A
    finally:
        reopened.close()


@pytest.mark.parametrize("role,action", [("analyst", "retry_frozen"), ("orchestrator", "retry_frozen"),
    ("reviewer", "retry_frozen"), ("validator", "rerun_validation"), ("delivery", "delivery_approval")])
def test_interrupted_node_policy_and_frozen_inputs(tmp_path, role, action):
    path = tmp_path / "node.sqlite3"
    store = SqliteEventStore(path)
    calls = []
    graph = GraphDefinition("node-policy", 1, (NodeDefinition("work", NodeType.AGENT, "work",
        settings={"recovery_role": role}),))

    def handler(context):
        calls.append((context.attempt, context.input_artifact_ids))
        if context.attempt == 1:
            raise Crash()
        return ok()

    runner = GraphRunner(store, HandlerRegistry({"work": handler}))
    with pytest.raises(Crash):
        runner.run(TASK_ID, graph)
    store.close()
    reopened = SqliteEventStore(path)
    try:
        recovered = GraphRunner(reopened, runner.handlers)
        recovery = recovered.prepare_recovery(TASK_ID, graph)
        assert recovery.actions[0]["action"] == action
        assert recovered.run(TASK_ID, graph).nodes[0].attempt == 1
        recovered.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        result = recovered.run(TASK_ID, graph)
        if role == "delivery":
            assert result.status is NodeStatus.AWAITING_HUMAN and len(calls) == 1
            assert recovered.approve_human(TASK_ID, graph, "work", expected_revision=result.revision).status is NodeStatus.SUCCEEDED
            assert len(calls) == 1  # No merge or delivery side effect on recovery.
        else:
            assert result.status is NodeStatus.SUCCEEDED and calls == [(1, ()), (2, ())]
    finally:
        reopened.close()


def test_checkpoint_created_before_result_transaction_failure_is_reused(tmp_path):
    store = SqliteEventStore(tmp_path / "event.sqlite3")
    graph = GraphDefinition("checkpoint-window", 1, (NodeDefinition("work", NodeType.AGENT, "work"),))
    calls = []

    def handler(context):
        calls.append(context.attempt)
        artifact = create_artifact(task_id=TASK_ID, producer="orchestrator.v1", producer_node="work", content=task_spec())
        store.artifacts.put(artifact)
        return ok(artifact.artifact_id)

    runner = GraphRunner(store, HandlerRegistry({"work": handler}))
    store._db.execute("""CREATE TRIGGER reject_finish BEFORE INSERT ON graph_events
        WHEN NEW.event_type='node_finished' BEGIN SELECT RAISE(ABORT,'injected disk failure'); END""")
    try:
        with pytest.raises(ForgePilotError):
            runner.run(TASK_ID, graph)
        assert runner.store.snapshot(TASK_ID).nodes[0].status is NodeStatus.RUNNING
        assert runner.checkpoints.latest(TASK_ID) is not None
        assert store._db.execute("SELECT COUNT(*) FROM graph_node_results").fetchone()[0] == 0
        store._db.execute("DROP TRIGGER reject_finish")
        confirm(runner, graph)
        assert runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
        assert calls == [1]
        assert store._db.execute("SELECT checkpoint_id FROM task_node_attempts").fetchone()[0]
    finally:
        store.close()


def test_real_process_exit_after_checkpoint_never_silently_resumes(tmp_path):
    path = tmp_path / "process.sqlite3"
    source = """
import os, sys
sys.path.insert(0, 'tests')
from test_artifact_protocol import TASK_ID, task_spec
from forgepilot.artifacts import create_artifact
from forgepilot.orchestration import GraphDefinition, GraphRunner, NodeDefinition, NodeType, NodeResult, NodeStatus
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.sqlite_store import SqliteEventStore
store = SqliteEventStore(sys.argv[1])
def handler(ctx):
    a = create_artifact(task_id=TASK_ID, producer='orchestrator.v1', producer_node='work', content=task_spec())
    store.artifacts.put(a)
    return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(a.artifact_id,))
runner = GraphRunner(store, HandlerRegistry({'work': handler}))
real = runner.store.transition
def transition(*args, **kwargs):
    if kwargs.get('event_type') == 'node_finished': os._exit(72)
    return real(*args, **kwargs)
runner.store.transition = transition
runner.run(TASK_ID, GraphDefinition('process', 1, (NodeDefinition('work', NodeType.AGENT, 'work'),)))
"""
    exited = subprocess.run([sys.executable, "-c", source, str(path)], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, timeout=30)
    assert exited.returncode == 72, exited.stderr.decode(errors="replace")
    store = SqliteEventStore(path)
    calls = []
    graph = GraphDefinition("process", 1, (NodeDefinition("work", NodeType.AGENT, "work"),))
    runner = GraphRunner(store, HandlerRegistry({"work": lambda ctx: calls.append(1) or ok()}))
    try:
        assert runner.run(TASK_ID, graph).nodes[0].status is NodeStatus.INTERRUPTED
        confirm(runner, graph)
        assert runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED and not calls
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["rollback", "adopt"])
def test_side_effect_then_confirmation_sqlite_failure_retries_same_token_once(tmp_path, operation):
    store, runner, graph, backend, record, calls = writer_case(tmp_path, with_changes=operation == "adopt")
    try:
        recovery = runner.prepare_recovery(TASK_ID, graph)
        store._db.execute("""CREATE TRIGGER reject_confirmation BEFORE INSERT ON graph_events
            WHEN NEW.event_type='recovery_confirmed' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        side_effects = (backend.restore_count, backend.checkpoint_count)
        assert runner.store.snapshot(TASK_ID).nodes[1].status is NodeStatus.RUNNING
        store._db.execute("DROP TRIGGER reject_confirmation")
        recovered = runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert recovered.nodes[1].status is (NodeStatus.READY if operation == "rollback" else NodeStatus.SUCCEEDED)
        assert (backend.restore_count, backend.checkpoint_count) == side_effects
        assert calls == [1]
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["sqlite", "process"])
def test_external_checkpoint_before_sql_checkpoint_recovers_from_intent(tmp_path, failure):
    store, runner, graph, backend, record, calls = writer_case(tmp_path, with_changes=True)
    recovery = runner.prepare_recovery(TASK_ID, graph)
    if failure == "sqlite":
        store._db.execute("""CREATE TRIGGER reject_checkpoint BEFORE INSERT ON checkpoints
            WHEN NEW.node_id='write' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with pytest.raises(ForgePilotError):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        store._db.execute("DROP TRIGGER reject_checkpoint")
        confirmed = runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
    else:
        backend.fail_after_next_checkpoint()
        with pytest.raises(SimulatedWorkspaceCrash):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        store.close()
        store = SqliteEventStore(tmp_path / "recovery.sqlite3", artifact_root=tmp_path / "artifacts",
                                 workspace_backend=backend)
        runner = GraphRunner(store, runner.handlers)
        prepared = runner.prepare_recovery(TASK_ID, graph)
        assert not prepared.blocked_reason
        assert prepared.actions[0]["action"] == "complete_intent"
        confirmed = runner.confirm_recovery(TASK_ID, graph, token=prepared.token, expected_revision=prepared.revision)
    try:
        assert confirmed.nodes[1].status is NodeStatus.SUCCEEDED
        assert backend.checkpoint_count == 2 and calls == [1]
        assert runner.checkpoints.latest(TASK_ID).node_id == "write"
        assert runner.run(TASK_ID, graph).status is NodeStatus.AWAITING_HUMAN
    finally:
        store.close()


def test_missing_event_reconciles_projection_once_and_event_write_failure_is_atomic(tmp_path):
    store = SqliteEventStore(tmp_path / "missing.sqlite3")
    graph = GraphDefinition("missing-event", 1, (NodeDefinition("gate", NodeType.HUMAN_GATE, "gate"),))
    runner = GraphRunner(store, HandlerRegistry({"gate": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN)}))
    try:
        runner.run(TASK_ID, graph)
        store._db.execute("DELETE FROM graph_events WHERE event_type='node_finished'")
        store._db.execute("""CREATE TRIGGER reject_reconcile BEFORE INSERT ON graph_events
            WHEN NEW.event_type='node_reconciled' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason == "graph_storage"
        assert runner.store.snapshot(TASK_ID).nodes[0].status is NodeStatus.AWAITING_HUMAN
        store._db.execute("DROP TRIGGER reject_reconcile")
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert not recovery.blocked_reason
        before = runner.events(TASK_ID)
        assert before[-1].event_type == "node_reconciled"
        assert runner.prepare_recovery(TASK_ID, graph) == recovery
        assert runner.events(TASK_ID) == before
    finally:
        store.close()


def test_plan_approval_checkpoint_before_state_failure_is_reconciled(tmp_path):
    store = SqliteEventStore(tmp_path / "plan-window.sqlite3")
    spec = create_artifact(task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
                           producer_node="spec", content=task_spec())
    execution = create_artifact(task_id=TASK_ID, artifact_id=PLAN_ID, producer="orchestrator.v1",
        producer_node="gate", parent_artifact_ids=(TASK_SPEC_ID,), content=plan())
    store.artifacts.put(spec)
    store.artifacts.put(execution)
    graph = GraphDefinition("plan-window", 1, (
        NodeDefinition("spec", NodeType.AGENT, "spec"),
        NodeDefinition("gate", NodeType.HUMAN_GATE, "gate", dependencies=("spec",)),
    ))
    runner = GraphRunner(store, HandlerRegistry({"spec": lambda context: ok(TASK_SPEC_ID),
        "gate": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=(PLAN_ID,))}))
    try:
        paused = runner.run(TASK_ID, graph)
        store._db.execute("""CREATE TRIGGER reject_approval BEFORE INSERT ON graph_events
            WHEN NEW.event_type='human_gate_approved' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with pytest.raises(ForgePilotError):
            runner.approve_human(TASK_ID, graph, "gate", expected_revision=paused.revision)
        assert runner.checkpoints.latest(TASK_ID).checkpoint_type == "plan_approved"
        assert runner.store.snapshot(TASK_ID).nodes[1].status is NodeStatus.AWAITING_HUMAN
        store._db.execute("DROP TRIGGER reject_approval")
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.actions[0]["action"] == "reuse_checkpoint"
        runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
    finally:
        store.close()


def test_interrupted_validator_reruns_entire_handler_even_with_a_success_checkpoint(tmp_path):
    store = SqliteEventStore(tmp_path / "validator-window.sqlite3")
    graph = GraphDefinition("validator-window", 1, (NodeDefinition("validate", NodeType.DETERMINISTIC,
        "validator.v1"),))
    phases = []

    def validator(context):
        phases.append((context.attempt, ("workspace", "test_integrity", "configure", "build", "ctest", "oracle", "diff")))
        report = create_artifact(task_id=TASK_ID, producer="validator.v1", producer_node="validate", content=all_contents()[4])
        context.artifacts.put(report)
        return ok(report.artifact_id)

    runner = GraphRunner(store, HandlerRegistry({"validator.v1": validator}))
    store._db.execute("""CREATE TRIGGER reject_validate BEFORE INSERT ON graph_events
        WHEN NEW.event_type='node_finished' BEGIN SELECT RAISE(ABORT,'injected'); END""")
    try:
        with pytest.raises(ForgePilotError):
            runner.run(TASK_ID, graph)
        assert runner.checkpoints.latest(TASK_ID).checkpoint_type == "validated"
        store._db.execute("DROP TRIGGER reject_validate")
        recovery = runner.prepare_recovery(TASK_ID, graph)
        assert recovery.actions[0]["action"] == "rerun_validation"
        runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
        assert [attempt for attempt, _ in phases] == [1, 2]
        assert phases[0][1] == phases[1][1]
        assert len(runner.checkpoints.list_for_task(TASK_ID)) == 2
    finally:
        store.close()


def test_cancellation_before_recovery_effects_prevents_rollback(tmp_path, monkeypatch):
    store, runner, graph, backend, record, calls = writer_case(tmp_path)
    try:
        recovery = runner.prepare_recovery(TASK_ID, graph)
        save = runner.recovery.store.save_recovery_plan

        def cancel_after_authorization(*args, **kwargs):
            save(*args, **kwargs)
            runner.cancel(TASK_ID, expected_revision=recovery.revision)

        monkeypatch.setattr(runner.recovery.store, "save_recovery_plan", cancel_after_authorization)
        with pytest.raises(ForgePilotError, match="changed"):
            runner.confirm_recovery(TASK_ID, graph, token=recovery.token, expected_revision=recovery.revision)
        assert runner.store.snapshot(TASK_ID).status is NodeStatus.CANCELLED
        assert backend.restore_count == 0 and calls == [1]
    finally:
        store.close()


def test_live_handler_cannot_be_recovered_while_it_is_still_writing(tmp_path):
    store = SqliteEventStore(tmp_path / "live.sqlite3")
    entered, release = Event(), Event()
    outcomes = []
    graph = GraphDefinition("live", 1, (NodeDefinition("work", NodeType.DETERMINISTIC, "work"),))

    def handler(context):
        entered.set()
        assert release.wait(5)
        return ok()

    runner = GraphRunner(store, HandlerRegistry({"work": handler}))
    thread = Thread(target=lambda: outcomes.append(runner.run(TASK_ID, graph)))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ForgePilotError, match="live node"):
            runner.prepare_recovery(TASK_ID, graph)
        running = runner.store.snapshot(TASK_ID)
        runner.cancel(TASK_ID, expected_revision=running.revision)
        release.set()
        thread.join(5)
        assert not thread.is_alive() and outcomes[0].status is NodeStatus.CANCELLED
        assert runner.checkpoints.list_for_task(TASK_ID) == ()
    finally:
        release.set()
        thread.join(5)
        store.close()


def test_completed_delivery_is_regated_before_ready_descendants_resume(tmp_path):
    store = SqliteEventStore(tmp_path / "delivery.sqlite3")
    graph = GraphDefinition("delivery-window", 1, (
        NodeDefinition("delivery", NodeType.DELIVERY, "delivery"),
        NodeDefinition("later", NodeType.DETERMINISTIC, "later", dependencies=("delivery",)),
    ))
    calls = []

    def later(context):
        calls.append("later")
        if context.attempt == 1:
            raise Crash()
        return ok()

    runner = GraphRunner(store, HandlerRegistry({"delivery": lambda ctx: calls.append("delivery") or ok(),
                                                "later": later}))
    try:
        with pytest.raises(Crash):
            runner.run(TASK_ID, graph)
        confirm(runner, graph)
        gated = runner.run(TASK_ID, graph)
        assert gated.status is NodeStatus.AWAITING_HUMAN
        assert calls == ["delivery", "later"]
        done = runner.approve_human(TASK_ID, graph, "delivery", expected_revision=gated.revision)
        assert done.status is NodeStatus.SUCCEEDED
        assert calls == ["delivery", "later", "later"]
    finally:
        store.close()


def test_writer_cannot_publish_changes_on_a_different_head(tmp_path):
    store, runner, graph, backend, record, _ = writer_case(tmp_path, with_changes=True)
    try:
        change = next(item for item in store.artifacts.list_for_task(TASK_ID) if item.artifact_type == "ChangeSet")
        backend.set_snapshot(replace(backend.inspect(record), head_commit="f" * 40))
        with pytest.raises(ForgePilotError, match="changed HEAD"):
            runner.recovery.record_completed(TASK_ID, graph.nodes[1], 1, ok(change.artifact_id))
        assert backend.checkpoint_count == 1
        assert runner.checkpoints.for_attempt(TASK_ID, "write", 1) is None
    finally:
        store.close()
