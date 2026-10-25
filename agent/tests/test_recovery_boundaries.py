"""Integration coverage for safe-node checkpoints and complete writer manifests."""
from __future__ import annotations

from dataclasses import replace

import pytest

from forgepilot.artifacts import ChangeSet, ChangedFile, create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeResult, NodeStatus, NodeType, validate_graph,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.store import GraphStore
from forgepilot.orchestration.workspace import StrictFakeWorkspaceBackend, WorkspaceSnapshot
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import (
    HASH_A, HASH_B, PLAN_ID, TASK_ID, TASK_SPEC_ID, all_contents, plan, task_spec,
)


HASH_C = "c" * 64
HASH_D = "d" * 64


def _workspace(store, graph, files, *, inputs=()):
    durable = GraphStore(store)
    created = durable.create_run(TASK_ID, validate_graph(graph), inputs=inputs)
    record = store.missions.save_workspace(
        workspace_id="boundaries-worktree", task_id=TASK_ID,
        repository_path="D:/fake/repository", worktree_path="D:/fake/isolated/task",
        base_ref="a" * 40, status="active", head_commit="a" * 40,
    )
    store.missions.update_graph_run(
        TASK_ID, previous_revision=created.revision, workspace_id=record["workspace_id"])
    backend = StrictFakeWorkspaceBackend()
    backend.register(WorkspaceSnapshot(record["workspace_id"], TASK_ID, "a" * 40, files), record)
    return backend, record


def _success(*artifact_ids):
    return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=tuple(artifact_ids))


def test_all_seven_boundary_types_and_each_repair_round_publish_checkpoints(tmp_path):
    graph = GraphDefinition("all-safe-boundaries", 1, (
        NodeDefinition("spec", NodeType.AGENT, "spec"),
        NodeDefinition("evidence", NodeType.DETERMINISTIC, "evidence", dependencies=("spec",)),
        NodeDefinition("plan", NodeType.HUMAN_GATE, "plan", dependencies=("spec", "evidence")),
        NodeDefinition("implementation", NodeType.AGENT, "implementer.v1", dependencies=("plan",)),
        NodeDefinition("repair-1", NodeType.AGENT, "repair.v1", dependencies=("plan", "implementation")),
        NodeDefinition("repair-2", NodeType.AGENT, "repair.v1", dependencies=("plan", "repair-1")),
        NodeDefinition("validation", NodeType.DETERMINISTIC, "validator.v1", dependencies=("repair-2",)),
        NodeDefinition("review", NodeType.AGENT, "reviewer.v1", dependencies=("spec", "validation")),
    ))
    store = SqliteEventStore(tmp_path / "all-boundaries.sqlite3", artifact_root=tmp_path / "artifacts")
    backend, record = _workspace(store, graph, {"src/window.cpp": HASH_A})
    produced = {}
    previous_boundaries = []
    writer_calls = []

    def publish(context, content, *, artifact_id=None, parents=()):
        latest = runner.checkpoints.latest(TASK_ID)
        previous_boundaries.append((context.node.node_id, latest.checkpoint_type if latest else None))
        artifact = create_artifact(
            task_id=TASK_ID, artifact_id=artifact_id, producer="test.v1",
            producer_node=context.node.node_id, parent_artifact_ids=parents, content=content,
        )
        context.artifacts.put(artifact)
        produced[context.node.node_id] = artifact
        return artifact.artifact_id

    def spec(context):
        return _success(publish(context, task_spec(), artifact_id=TASK_SPEC_ID))

    def evidence(context):
        return _success(publish(context, all_contents()[1], parents=(TASK_SPEC_ID,)))

    def wait_for_plan(context):
        plan_id = publish(context, plan(), artifact_id=PLAN_ID,
                          parents=(TASK_SPEC_ID, produced["evidence"].artifact_id))
        return NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=(plan_id,))

    def writer(context):
        assert PLAN_ID in context.input_artifact_ids
        writer_calls.append((context.node.node_id, context.attempt))
        before, after = {
            "implementation": (HASH_A, HASH_B),
            "repair-1": (HASH_B, HASH_C),
            "repair-2": (HASH_C, HASH_D),
        }[context.node.node_id]
        snapshot = backend.inspect(record)
        assert snapshot.file_hashes["src/window.cpp"] == before
        backend.set_snapshot(replace(snapshot, file_hashes={"src/window.cpp": after}))
        change = all_contents()[3].model_copy(update={
            "summary": f"Complete {context.node.node_id}",
            "files": (all_contents()[3].files[0].model_copy(update={
                "before_sha256": before, "after_sha256": after,
            }),),
        })
        return _success(publish(context, change, parents=(PLAN_ID,)))

    def validator(context):
        return _success(publish(context, all_contents()[4], parents=(produced["repair-2"].artifact_id,)))

    def reviewer(context):
        return _success(publish(context, all_contents()[5], parents=(TASK_SPEC_ID, produced["validation"].artifact_id)))

    runner = GraphRunner(store, HandlerRegistry({
        "spec": spec, "evidence": evidence, "plan": wait_for_plan,
        "implementer.v1": writer, "repair.v1": writer,
        "validator.v1": validator, "reviewer.v1": reviewer,
    }), workspace_backend=backend)
    try:
        waiting = runner.run(TASK_ID, graph)
        assert waiting.status is NodeStatus.AWAITING_HUMAN
        assert [(cp.node_id, cp.checkpoint_type) for cp in runner.checkpoints.list_for_task(TASK_ID)] == [
            ("spec", "task_spec"), ("evidence", "evidence"),
        ]
        assert writer_calls == []
        assert runner.checkpoints.for_attempt(TASK_ID, "plan", 1) is None

        finished = runner.approve_human(TASK_ID, graph, "plan", expected_revision=waiting.revision)
        assert finished.status is NodeStatus.SUCCEEDED
        checkpoints = runner.checkpoints.list_for_task(TASK_ID)
        assert [(cp.node_id, cp.checkpoint_type) for cp in checkpoints] == [
            ("spec", "task_spec"), ("evidence", "evidence"), ("plan", "plan_approved"),
            ("implementation", "implementation"), ("repair-1", "repair"), ("repair-2", "repair"),
            ("validation", "validated"), ("review", "reviewed"),
        ]
        assert previous_boundaries == [
            ("spec", None), ("evidence", "task_spec"), ("plan", "evidence"),
            ("implementation", "plan_approved"), ("repair-1", "implementation"),
            ("repair-2", "repair"), ("validation", "repair"), ("review", "validated"),
        ]
        assert writer_calls == [("implementation", 1), ("repair-1", 1), ("repair-2", 1)]
        assert backend.checkpoint_count == 8
        assert len({cp.head_commit for cp in checkpoints}) == 8
        assert len({cp.checkpoint_id for cp in checkpoints}) == 8
        assert checkpoints[-1].file_hashes == {"src/window.cpp": HASH_D}
        for checkpoint in checkpoints:
            runner.checkpoints.verify(checkpoint)
            backend.verify_checkpoint(record, checkpoint.checkpoint_id,
                                      checkpoint.head_commit, checkpoint.file_hashes)
            attempt = runner.store.attempt_record(TASK_ID, checkpoint.node_id, 1)
            assert attempt["checkpoint_id"] == checkpoint.checkpoint_id
            assert checkpoint.result.output_artifact_ids == (produced[checkpoint.node_id].artifact_id,)
    finally:
        store.close()


@pytest.mark.parametrize("explicit", [False, True], ids=["inferred", "explicit-validated"])
def test_failed_validation_report_never_creates_a_safe_checkpoint(tmp_path, explicit):
    graph = GraphDefinition("failed-validation-boundary", 1, (
        NodeDefinition("validation", NodeType.DETERMINISTIC, "validator.v1",
                       settings={"checkpoint_type": "validated"} if explicit else {}),
    ))
    store = SqliteEventStore(tmp_path / "failed-validation.sqlite3")
    produced = []

    def validator(context):
        report = all_contents()[4].model_copy(update={
            "status": "failed", "tests_failed": 1, "failure_classification": "test",
            "summary": "A regression failed.",
        })
        artifact = create_artifact(task_id=TASK_ID, producer="validator.v1",
                                   producer_node=context.node.node_id, content=report)
        context.artifacts.put(artifact)
        produced.append(artifact.artifact_id)
        return _success(artifact.artifact_id)

    runner = GraphRunner(store, HandlerRegistry({"validator.v1": validator}))
    try:
        if explicit:
            with pytest.raises(ForgePilotError, match="checkpoint_policy"):
                runner.run(TASK_ID, graph)
        else:
            runner.run(TASK_ID, graph)
        assert len(produced) == 1
        assert store.artifacts.get(produced[0]).content.status == "failed"
        assert runner.checkpoints.list_for_task(TASK_ID) == ()
        assert runner.checkpoints.intent_for_attempt(TASK_ID, "validation", 1) is None
    finally:
        store.close()


@pytest.mark.parametrize("unrelated", [None, "added", "deleted", "modified"])
def test_added_and_deleted_tombstones_match_the_complete_changeset_manifest(tmp_path, unrelated):
    graph = GraphDefinition("complete-manifest", 1, (
        NodeDefinition("spec", NodeType.AGENT, "spec"),
        NodeDefinition("writer", NodeType.AGENT, "implementer.v1", dependencies=("spec",)),
    ))
    store = SqliteEventStore(tmp_path / "manifest.sqlite3")
    spec = create_artifact(task_id=TASK_ID, artifact_id=TASK_SPEC_ID, producer="test.v1",
                           producer_node="spec", content=task_spec())
    execution = create_artifact(task_id=TASK_ID, artifact_id=PLAN_ID, producer="test.v1",
                                producer_node="plan", parent_artifact_ids=(TASK_SPEC_ID,), content=plan())
    store.artifacts.put(spec)
    store.artifacts.put(execution)
    initial = {
        "src/window.cpp": HASH_A, "src/new.cpp": None,
        "src/deleted.cpp": HASH_A, "src/untouched.cpp": HASH_A,
    }
    backend, record = _workspace(
        store, graph, initial, inputs=((PLAN_ID, execution.content_sha256),))
    calls = []

    def writer(context):
        calls.append(context.attempt)
        after = {
            "src/window.cpp": HASH_B, "src/new.cpp": HASH_C,
            "src/deleted.cpp": None, "src/untouched.cpp": HASH_A,
        }
        if unrelated == "added":
            after["src/unrelated.cpp"] = HASH_B
        elif unrelated == "deleted":
            after.pop("src/untouched.cpp")
        elif unrelated == "modified":
            after["src/untouched.cpp"] = HASH_B
        backend.set_snapshot(replace(backend.inspect(record), file_hashes=after))
        change = ChangeSet(execution_plan_artifact_id=PLAN_ID, summary="Modify, add, and remove files.", files=(
            all_contents()[3].files[0],
            ChangedFile(path="src/new.cpp", change_type="added", unified_diff="@@ -0,0 +1 @@\n+new",
                        before_sha256=None, after_sha256=HASH_C,
                        plan_step_ids=("STEP-1",), acceptance_criteria_ids=("AC-1",)),
            ChangedFile(path="src/deleted.cpp", change_type="deleted", unified_diff="@@ -1 +0,0 @@\n-old",
                        before_sha256=HASH_A, after_sha256=None,
                        plan_step_ids=("STEP-1",), acceptance_criteria_ids=("AC-1",)),
        ))
        artifact = create_artifact(task_id=TASK_ID, producer="implementer.v1", producer_node="writer",
                                   parent_artifact_ids=(PLAN_ID,), content=change)
        context.artifacts.put(artifact)
        return _success(artifact.artifact_id)

    runner = GraphRunner(store, HandlerRegistry({
        "spec": lambda context: _success(TASK_SPEC_ID), "implementer.v1": writer,
    }), workspace_backend=backend)
    try:
        if unrelated:
            with pytest.raises(ForgePilotError, match="workspace_corrupted"):
                runner.run(TASK_ID, graph)
            assert runner.checkpoints.for_attempt(TASK_ID, "writer", 1) is None
            assert backend.checkpoint_count == 1
        else:
            assert runner.run(TASK_ID, graph).status is NodeStatus.SUCCEEDED
            checkpoint = runner.checkpoints.for_attempt(TASK_ID, "writer", 1)
            assert checkpoint.checkpoint_type == "implementation"
            assert dict(checkpoint.file_hashes) == {
                "src/window.cpp": HASH_B, "src/new.cpp": HASH_C,
                "src/deleted.cpp": None, "src/untouched.cpp": HASH_A,
            }
            backend.verify_checkpoint(record, checkpoint.checkpoint_id,
                                      checkpoint.head_commit, checkpoint.file_hashes)
        assert calls == [1]
    finally:
        store.close()


@pytest.mark.parametrize("source", ["handler", "agent_id"])
@pytest.mark.parametrize("name", ["implementer.v1", "repair.v1"])
def test_inferred_writer_never_executes_a_handler_without_a_worktree(tmp_path, source, name):
    handler_name = name if source == "handler" else "generic-agent"
    graph = GraphDefinition("inferred-writer-preflight", 1, (
        NodeDefinition("write", NodeType.AGENT, handler_name,
                       agent_id=name if source == "agent_id" else None),
    ))
    store = SqliteEventStore(tmp_path / "writer-preflight.sqlite3")
    calls = []

    def writer(context):
        calls.append(context.attempt)
        return _success()

    runner = GraphRunner(store, HandlerRegistry({handler_name: writer}))
    try:
        with pytest.raises(ForgePilotError, match="workspace_corrupted"):
            runner.run(TASK_ID, graph)
        assert calls == []
        assert runner.store.snapshot(TASK_ID).nodes[0].status is NodeStatus.READY
        assert runner.store.attempt_record(TASK_ID, "write", 1) is None
        assert runner.checkpoints.list_for_task(TASK_ID) == ()
    finally:
        store.close()
