"""Deterministic concurrency proofs for the two-Analyst fan-out and all-success join."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock
from time import monotonic, sleep

import pytest

from forgepilot.agents.registry import AgentRegistry
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    EdgeCondition, EdgeDefinition, FailureKind, GraphDefinition, GraphRunner,
    NodeDefinition, NodeResult, NodeStatus, NodeType,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry, NodeExecutionError
from forgepilot.sqlite_store import SqliteEventStore


def graph(*, tail=False):
    registry = AgentRegistry()
    analysts = []
    for node_id, agent_id in (("root", "root-cause-analyst.v1"),
                              ("impact", "impact-test-analyst.v1")):
        definition = registry.get(agent_id)
        analysts.append(NodeDefinition(
            node_id, NodeType.AGENT, "analyze", dependencies=("fanout",),
            agent_id=agent_id, agent_definition_hash=definition.definition_hash,
        ))
    return GraphDefinition("analysis", 1, (
        NodeDefinition("fanout", NodeType.PARALLEL_FANOUT, "boundary"),
        *analysts,
        NodeDefinition("join", NodeType.JOIN, "merge", dependencies=("root", "impact")),
        *((NodeDefinition("git", NodeType.DETERMINISTIC, "exclusive"),
           NodeDefinition("build", NodeType.DETERMINISTIC, "exclusive"),
           NodeDefinition("writer", NodeType.DETERMINISTIC, "exclusive")) if tail else ()),
    ))


def test_both_analysts_overlap_and_join_waits_before_exclusive_handlers(tmp_path):
    both_started = Barrier(2)
    lock = Lock()
    active = peak = 0
    intervals = {}
    calls = []

    def analyze(context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            intervals[context.node.node_id] = [monotonic()]
        both_started.wait(timeout=5)
        sleep(0.02 if context.node.node_id == "root" else 0.05)
        with lock:
            intervals[context.node.node_id].append(monotonic())
            active -= 1
        return NodeResult(NodeStatus.SUCCEEDED)

    def exclusive(context):
        assert active == 0
        assert len(intervals) == 2 and all(len(value) == 2 for value in intervals.values())
        calls.append(context.node.node_id)
        return NodeResult(NodeStatus.SUCCEEDED)

    store = SqliteEventStore(tmp_path / "parallel.sqlite3")
    try:
        runner = GraphRunner(store, HandlerRegistry({"analyze": analyze, "merge": exclusive,
                                                    "exclusive": exclusive}))
        original_checkpoint = runner.recovery.record_completed

        def checkpoint(*args, **kwargs):
            assert active == 0, "Git checkpoint overlapped an Analyst handler"
            return original_checkpoint(*args, **kwargs)

        runner.recovery.record_completed = checkpoint
        result = runner.run("parallel-task", graph(tail=True))
        assert result.status is NodeStatus.SUCCEEDED
        assert peak == 2
        assert max(value[0] for value in intervals.values()) < min(value[1] for value in intervals.values())
        assert calls == ["join", "git", "build", "writer"]
        events = runner.events("parallel-task")
        starts = [event.sequence for event in events
                  if event.event_type == "node_started" and event.node_id in {"root", "impact"}]
        ends = [event.sequence for event in events
                if event.event_type == "node_finished" and event.node_id in {"root", "impact"}]
        assert max(starts) < min(ends)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert [event.revision for event in events] == list(range(1, len(events) + 1))
        assert runner.run("parallel-task", graph(tail=True)) == result
    finally:
        store.close()


@pytest.mark.parametrize("failure,expected", [
    (FailureKind.MODEL_TRANSIENT, NodeStatus.RETRYABLE),
    (FailureKind.TOOL_POLICY_VIOLATION, NodeStatus.FAILED),
])
def test_one_failed_analyst_never_joins_and_retry_keeps_successful_sibling(failure, expected):
    both_started = Barrier(2)
    calls = Counter()

    def analyze(context):
        calls[context.node.node_id] += 1
        if context.attempt == 1:
            both_started.wait(timeout=5)
        if context.node.node_id == "root" and context.attempt == 1:
            raise NodeExecutionError(failure, "controlled Analyst failure")
        return NodeResult(NodeStatus.SUCCEEDED)

    store = SqliteEventStore()
    try:
        runner = GraphRunner(store, HandlerRegistry({
            "analyze": analyze, "merge": lambda context: calls.update(["join"]) or NodeResult(NodeStatus.SUCCEEDED),
        }))
        result = runner.run("failure-task", graph())
        assert result.status is expected
        assert calls == Counter(root=1, impact=1)
        assert next(node for node in result.nodes if node.node_id == "join").status is NodeStatus.PENDING
        if expected is NodeStatus.RETRYABLE:
            done = runner.retry("failure-task", graph(), "root", expected_revision=result.revision)
            assert done.status is NodeStatus.SUCCEEDED
            assert calls == Counter(root=2, impact=1, join=1)
    finally:
        store.close()


def test_cancellation_reaches_both_running_analysts_and_prevents_join():
    both_started = Barrier(3)
    cancelled = []

    def analyze(context):
        both_started.wait(timeout=5)
        deadline = monotonic() + 5
        while not context.is_cancelled() and monotonic() < deadline:
            sleep(0.005)
        assert context.is_cancelled()
        cancelled.append(context.node.node_id)
        return NodeResult(NodeStatus.SUCCEEDED)

    store = SqliteEventStore()
    try:
        runner = GraphRunner(store, HandlerRegistry({"analyze": analyze,
                                                    "merge": lambda context: pytest.fail("join ran after cancellation")}))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.run, "cancel-task", graph())
            both_started.wait(timeout=5)
            before = runner.store.snapshot("cancel-task")
            runner.cancel("cancel-task", expected_revision=before.revision)
            done = future.result(timeout=5)
        assert done.status is NodeStatus.CANCELLED
        assert set(cancelled) == {"root", "impact"}
        assert next(node for node in done.nodes if node.node_id == "join").attempt == 0
        assert all(node.result is None for node in done.nodes if node.node_id in {"root", "impact"})
    finally:
        store.close()


def test_two_runners_share_analyst_limit_and_exclude_other_task_build_and_git():
    started = Barrier(3)
    release = Event()
    active = peak = 0
    lock = Lock()
    calls = []

    def analyze(context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if context.task_id == "first":
            started.wait(timeout=5)
            assert release.wait(timeout=5)
        with lock:
            active -= 1
        return NodeResult(NodeStatus.SUCCEEDED)

    def exclusive(context):
        assert active == 0
        calls.append(context.node.node_id)
        return NodeResult(NodeStatus.SUCCEEDED)

    store = SqliteEventStore()
    try:
        handlers = HandlerRegistry({"analyze": analyze, "exclusive": exclusive})
        first = GraphRunner(store, handlers)
        second = GraphRunner(store, handlers)
        other = GraphDefinition("other", 1, (
            NodeDefinition("build", NodeType.DETERMINISTIC, "exclusive"),
            NodeDefinition("git", NodeType.DETERMINISTIC, "exclusive", dependencies=("build",)),
        ))
        with ThreadPoolExecutor(max_workers=3) as pool:
            one = pool.submit(first.run, "first", graph())
            started.wait(timeout=5)
            two = pool.submit(second.run, "second", graph())
            three = pool.submit(second.run, "third", other)
            sleep(0.03)
            assert active == 2 and calls == [] and not two.done() and not three.done()
            release.set()
            assert all(future.result(timeout=5).status is NodeStatus.SUCCEEDED
                       for future in (one, two, three))
        assert peak == 2 and calls == ["build", "git"]
    finally:
        store.close()


def test_cancellation_while_waiting_for_another_task_releases_waiter_promptly():
    entered = Event()
    release = Event()

    def hold(context):
        entered.set()
        assert release.wait(timeout=5)
        return NodeResult(NodeStatus.SUCCEEDED)

    definition = GraphDefinition("single", 1, (NodeDefinition("work", NodeType.DETERMINISTIC, "hold"),))
    store = SqliteEventStore()
    try:
        runner = GraphRunner(store, HandlerRegistry({"hold": hold}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(runner.run, "owner", definition)
            assert entered.wait(timeout=5)
            waiter = pool.submit(runner.run, "waiter", definition)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                try:
                    current = runner.store.snapshot("waiter")
                except Exception:
                    sleep(0.005)
                    continue
                if current.nodes[0].status is NodeStatus.READY:
                    break
                sleep(0.005)
            runner.cancel("waiter", expected_revision=current.revision)
            try:
                assert waiter.result(timeout=1).status is NodeStatus.CANCELLED
                assert not owner.done()
            finally:
                release.set()
            assert owner.result(timeout=5).status is NodeStatus.SUCCEEDED
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize("operation", ["approve", "prepare", "confirm"])
def test_approval_and_recovery_cannot_overlap_another_tasks_analysts(operation, monkeypatch):
    both_started = Barrier(3)
    release = Event()
    lock = Lock()
    active = 0
    effects = []

    def analyze(context):
        nonlocal active
        with lock:
            active += 1
        both_started.wait(timeout=5)
        try:
            assert release.wait(timeout=5)
        finally:
            with lock:
                active -= 1
        return NodeResult(NodeStatus.SUCCEEDED)

    target = GraphDefinition("control", 1, (
        NodeDefinition("approval", NodeType.HUMAN_GATE, "gate"),
        NodeDefinition("after", NodeType.DETERMINISTIC, "after", dependencies=("approval",)),
    ))
    store = SqliteEventStore()
    try:
        handlers = HandlerRegistry({
            "analyze": analyze,
            "gate": lambda context: NodeResult(NodeStatus.AWAITING_HUMAN),
            "after": lambda context: effects.append("next_node") or NodeResult(NodeStatus.SUCCEEDED),
        })
        owner = GraphRunner(store, handlers)
        controller = GraphRunner(store, handlers)
        before = controller.run("review-target", target)
        before_events = controller.events("review-target")
        original_record = controller.recovery.record_completed

        def checkpoint(*args, **kwargs):
            assert active == 0, "Approval checkpoint overlapped an Analyst"
            if kwargs.get("approved"):
                effects.append("checkpoint")
            return original_record(*args, **kwargs)

        def inspect(task_id, definition):
            assert active == 0, "Recovery Git inspection overlapped an Analyst"
            effects.append("inspect")
            return controller.store.snapshot(task_id)

        def restore(task_id, definition, **kwargs):
            assert active == 0, "Recovery restore/checkpoint overlapped an Analyst"
            effects.append("restore")
            return controller.store.snapshot(task_id)

        monkeypatch.setattr(controller.recovery, "record_completed", checkpoint)
        monkeypatch.setattr(controller.recovery, "prepare", inspect)
        monkeypatch.setattr(controller.recovery, "confirm", restore)

        def control():
            if operation == "approve":
                return controller.approve_human("review-target", target, "approval",
                                                expected_revision=before.revision)
            if operation == "prepare":
                return controller.prepare_recovery("review-target", target)
            return controller.confirm_recovery("review-target", target, token="fake-reviewed-token",
                                               expected_revision=before.revision)

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(owner.run, "live-analysts", graph())
            both_started.wait(timeout=5)
            try:
                assert active == 2
                with pytest.raises(ForgePilotError) as failure:
                    control()
                assert failure.value.code == "graph_active"
                assert effects == []
                assert controller.store.snapshot("review-target") == before
                assert controller.events("review-target") == before_events
            finally:
                release.set()
            assert running.result(timeout=5).status is NodeStatus.SUCCEEDED

        result = control()
        if operation == "approve":
            # The next node also acquires the lock, proving approve released it
            # before resuming run(). Real checkpoint/recovery effects are covered
            # by the existing workspace and recovery integration suites.
            assert result.status is NodeStatus.SUCCEEDED
            assert effects == ["checkpoint", "next_node"]
        else:
            assert result == before
            assert effects == ["inspect" if operation == "prepare" else "restore"]
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize("spoof", ["hash", "role", "writer", "duplicate"])
def test_untrusted_or_nonanalyst_nodes_never_acquire_parallel_execution(spoof):
    original = graph()
    nodes = list(original.nodes)
    if spoof == "hash":
        nodes[2] = replace(nodes[2], agent_definition_hash="a" * 64)
    elif spoof == "role":
        nodes[2] = replace(nodes[2], settings={"recovery_role": "deterministic"})
    elif spoof == "writer":
        nodes[2] = replace(nodes[2], agent_id="fake-writer.v1", settings={"recovery_role": "deterministic"})
    else:
        nodes[2] = replace(nodes[2], agent_id=nodes[1].agent_id,
                           agent_definition_hash=nodes[1].agent_definition_hash)
    changed = replace(original, nodes=tuple(nodes))
    calls = []

    def analyze(context):
        calls.append((context.node.node_id, "started"))
        sleep(0.01)
        calls.append((context.node.node_id, "finished"))
        return NodeResult(NodeStatus.SUCCEEDED)

    store = SqliteEventStore()
    try:
        done = GraphRunner(store, HandlerRegistry({"analyze": analyze})).run("serial-task", changed)
        assert done.status is NodeStatus.SUCCEEDED
        assert calls == [("root", "started"), ("root", "finished"),
                         ("impact", "started"), ("impact", "finished")]
    finally:
        store.close()


def test_join_rejects_skipped_branch_instead_of_silently_dropping_it():
    calls = []
    definition = GraphDefinition("conditional-join", 1, (
        NodeDefinition("choose", NodeType.CONDITIONAL, "choose"),
        NodeDefinition("left", NodeType.DETERMINISTIC, "ok"),
        NodeDefinition("right", NodeType.DETERMINISTIC, "ok"),
        NodeDefinition("join", NodeType.JOIN, "merge", dependencies=("left", "right")),
    ), (EdgeDefinition("choose", "left", EdgeCondition(route="left")),
        EdgeDefinition("choose", "right", EdgeCondition(route="right"))))
    store = SqliteEventStore()
    try:
        runner = GraphRunner(store, HandlerRegistry({
            "choose": lambda context: NodeResult(NodeStatus.SUCCEEDED, next_route="left"),
            "ok": lambda context: NodeResult(NodeStatus.SUCCEEDED),
            "merge": lambda context: calls.append("merge") or NodeResult(NodeStatus.SUCCEEDED),
        }))
        done = runner.run("missing-branch", definition)
        assert done.status is NodeStatus.FAILED
        assert calls == []
        joined = next(node for node in done.nodes if node.node_id == "join")
        assert joined.result.failure_kind is FailureKind.MODEL_CONTRACT_VIOLATION
    finally:
        store.close()
