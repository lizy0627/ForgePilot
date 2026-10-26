"""Real Git backend integration at graph approval, writer and recovery boundaries."""
from hashlib import sha256
from pathlib import Path

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeResult, NodeStatus, NodeType, validate_graph,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.store import GraphStore
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.workspace_manager import WorkspaceManager
from test_artifact_protocol import (
    CHANGE_ID, PLAN_ID, REVIEW_ID, TASK_ID, TASK_SPEC_ID, VALIDATION_ID,
    all_contents, plan, task_spec,
)
from test_workspace_manager import committed_repo, git, manager


class ProcessDeath(BaseException):
    pass


def graph_case(manager, committed_repo, tmp_path, git_exe, *, crash=False, completed_changes=False):
    manager.create(committed_repo, TASK_ID)
    workspace = manager.record(TASK_ID)
    worktree = Path(workspace["worktree_path"])
    store_path = tmp_path / "real-graph.sqlite3"
    store = SqliteEventStore(store_path, artifact_root=tmp_path / "graph-artifacts", workspace_backend=manager)
    scope = task_spec().allowed_scope.model_copy(update={"path_patterns": ("source.txt",)})
    specification = task_spec().model_copy(update={"allowed_scope": scope})
    step = plan().steps[0].model_copy(update={
        "expected_files": ("source.txt",), "acceptance_criteria_ids": ("AC-1", "AC-2"),
        "adds_tests": False,
    })
    execution = plan().model_copy(update={"steps": (step,)})
    spec = create_artifact(task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="orchestrator.v1",
                           producer_node="spec", content=specification)
    approved_plan = create_artifact(task_id=TASK_ID, artifact_id=PLAN_ID, producer="orchestrator.v1",
                                    producer_node="gate", parent_artifact_ids=(TASK_SPEC_ID,), content=execution)
    store.artifacts.put(spec)
    store.artifacts.put(approved_plan)
    graph = GraphDefinition("real-workspace", 1, (
        NodeDefinition("gate", NodeType.HUMAN_GATE, "gate"),
        NodeDefinition("write", NodeType.AGENT, "implementer.v1", dependencies=("gate",)),
        NodeDefinition("validate", NodeType.DETERMINISTIC, "validator.v1", dependencies=("write",)),
        NodeDefinition("review", NodeType.AGENT, "reviewer.v1", dependencies=("validate",)),
    ))
    created = GraphStore(store).create_run(TASK_ID, validate_graph(graph),
                                          inputs=((PLAN_ID, approved_plan.content_sha256),))
    store.missions.save_workspace(**workspace)
    store.missions.update_graph_run(TASK_ID, previous_revision=created.revision,
                                    workspace_id=workspace["workspace_id"])
    calls = []
    leases_checked = []
    before = (worktree / "source.txt").read_bytes()
    after = b"real graph writer output\n"
    competitor = WorkspaceManager(manager.root, git_executable=git_exe)

    def writer(context):
        calls.append(context.attempt)
        observed = competitor.inspect_task(TASK_ID)
        with pytest.raises(ForgePilotError, match="lease"):
            with competitor.writer(TASK_ID, expected=observed):
                pytest.fail("Graph handler is running without repository writer ownership")
        leases_checked.append(True)
        (worktree / "source.txt").write_bytes(after)
        if completed_changes or not crash:
            original = all_contents()[3]
            changed = original.files[0].model_copy(update={
                "path": "source.txt", "before_sha256": sha256(before).hexdigest(),
                "after_sha256": sha256(after).hexdigest(),
            })
            changes = create_artifact(task_id=TASK_ID, artifact_id=CHANGE_ID, producer="implementer.v1",
                                      producer_node="write", parent_artifact_ids=(PLAN_ID,),
                                      content=original.model_copy(update={"files": (changed,)}))
            context.artifacts.put(changes)
        if crash:
            raise ProcessDeath("interrupted inside real writer")
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(CHANGE_ID,))

    def validator(context):
        artifact = create_artifact(task_id=TASK_ID, artifact_id=VALIDATION_ID, producer="validator.v1",
                                   producer_node="validate", parent_artifact_ids=(CHANGE_ID,), content=all_contents()[4])
        context.artifacts.put(artifact)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(VALIDATION_ID,))

    def reviewer(context):
        artifact = create_artifact(task_id=TASK_ID, artifact_id=REVIEW_ID, producer="reviewer.v1",
                                   producer_node="review", parent_artifact_ids=(TASK_SPEC_ID, CHANGE_ID, VALIDATION_ID),
                                   content=all_contents()[5])
        context.artifacts.put(artifact)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(REVIEW_ID,))

    def checked(handler):
        def run(context):
            try:
                return handler(context)
            except Exception as exc:
                pytest.fail(f"Real workspace handler {handler.__name__} failed: {exc!r}")
        return run

    handlers = HandlerRegistry({
        "gate": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=(PLAN_ID,)),
        "implementer.v1": checked(writer), "validator.v1": checked(validator), "reviewer.v1": checked(reviewer),
    })
    runner = GraphRunner(store, handlers)
    paused = runner.run(TASK_ID, graph)
    assert paused.status is NodeStatus.AWAITING_HUMAN
    assert manager._load(TASK_ID)["branch"] is None
    assert not git(git_exe, committed_repo, "branch", "--list", "forgepilot/*")
    return store, runner, graph, paused, workspace, calls, leases_checked, store_path


def test_graph_human_approval_writer_checkpoint_validation_and_review_use_real_backend(manager, committed_repo, tmp_path, git_exe):
    store, runner, graph, paused, record, calls, leases, _ = graph_case(manager, committed_repo, tmp_path, git_exe)
    try:
        finished = runner.approve_human(TASK_ID, graph, "gate", expected_revision=paused.revision)
        assert finished.status is NodeStatus.SUCCEEDED
        assert calls == [1] and leases == [True]
        checkpoints = runner.checkpoints.list_for_task(TASK_ID)
        assert [item.checkpoint_type for item in checkpoints] == ["plan_approved", "implementation", "validated", "reviewed"]
        for checkpoint in checkpoints:
            manager.verify_checkpoint(record, checkpoint.checkpoint_id,
                                       checkpoint.head_commit, checkpoint.file_hashes)
        assert manager.delivery_preview(TASK_ID, "main")["validated"]
        assert (committed_repo / "source.txt").read_bytes() == b"original source\n"
        assert (Path(record["worktree_path"]) / "source.txt").read_bytes() == b"real graph writer output\n"
    finally:
        store.close()


@pytest.mark.parametrize("completed_changes", [False, True])
def test_real_graph_restart_explicitly_rolls_back_or_adopts_owned_writer(manager, committed_repo, tmp_path, git_exe, completed_changes):
    store, runner, graph, paused, record, calls, leases, store_path = graph_case(
        manager, committed_repo, tmp_path, git_exe, crash=True, completed_changes=completed_changes)
    with pytest.raises(ProcessDeath):
        runner.approve_human(TASK_ID, graph, "gate", expected_revision=paused.revision)
    handlers = runner.handlers
    store.close()
    reopened_manager = WorkspaceManager(manager.root, git_executable=git_exe)
    reopened = SqliteEventStore(store_path, artifact_root=tmp_path / "graph-artifacts",
                                workspace_backend=reopened_manager)
    recovered_runner = GraphRunner(reopened, handlers)
    try:
        observed = reopened_manager.inspect_task(TASK_ID)
        recovery = recovered_runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason is None, recovery
        expected_action = "adopt_changes" if completed_changes else "rollback_writer"
        assert any(item["action"] == expected_action for item in recovery.actions)
        assert reopened_manager.inspect_task(TASK_ID) == observed
        with pytest.raises(ForgePilotError):
            recovered_runner.confirm_recovery(TASK_ID, graph, token="invalid", expected_revision=recovery.revision)
        confirmed = recovered_runner.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                                       expected_revision=recovery.revision)
        writer = next(node for node in confirmed.nodes if node.node_id == "write")
        assert writer.status is (NodeStatus.SUCCEEDED if completed_changes else NodeStatus.READY)
        expected = b"real graph writer output\n" if completed_changes else b"original source\n"
        assert (Path(record["worktree_path"]) / "source.txt").read_bytes() == expected
        assert calls == [1] and leases == [True]
        assert (committed_repo / "source.txt").read_bytes() == b"original source\n"
    finally:
        reopened.close()


def test_real_graph_recovers_checkpoint_head_cas_before_manager_and_sqlite_publication(manager, committed_repo, tmp_path, git_exe, monkeypatch):
    store, runner, graph, paused, record, calls, leases, store_path = graph_case(manager, committed_repo, tmp_path, git_exe)
    publish = manager._publish_checkpoint

    def interrupted_publish(state):
        if state["intent"]["message"] == "forgepilot(checkpoint): implementation":
            raise ProcessDeath("Git HEAD moved before checkpoint publication")
        return publish(state)

    monkeypatch.setattr(manager, "_publish_checkpoint", interrupted_publish)
    with pytest.raises(ProcessDeath):
        runner.approve_human(TASK_ID, graph, "gate", expected_revision=paused.revision)
    observed = manager.inspect_task(TASK_ID)
    assert runner.checkpoints.latest(TASK_ID).checkpoint_type == "plan_approved"
    handlers = runner.handlers
    store.close()
    reopened_manager = WorkspaceManager(manager.root, git_executable=git_exe)
    reopened = SqliteEventStore(store_path, artifact_root=tmp_path / "graph-artifacts", workspace_backend=reopened_manager)
    recovered_runner = GraphRunner(reopened, handlers)
    try:
        recovery = recovered_runner.prepare_recovery(TASK_ID, graph)
        assert recovery.blocked_reason is None, recovery
        assert any(action["action"] == "complete_intent" for action in recovery.actions)
        assert reopened_manager.inspect_task(TASK_ID) == observed
        assert recovered_runner.checkpoints.latest(TASK_ID).checkpoint_type == "plan_approved"
        confirmed = recovered_runner.confirm_recovery(TASK_ID, graph, token=recovery.token,
                                                       expected_revision=recovery.revision)
        assert next(node for node in confirmed.nodes if node.node_id == "write").status is NodeStatus.SUCCEEDED
        assert recovered_runner.checkpoints.latest(TASK_ID).head_commit == observed.head_commit
        assert recovered_runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
        assert calls == [1] and leases == [True]
        assert (committed_repo / "source.txt").read_bytes() == b"original source\n"
    finally:
        reopened.close()
