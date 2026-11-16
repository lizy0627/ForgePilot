"""Storage-scoped execution ownership without database scheduling state."""
from concurrent.futures import ThreadPoolExecutor
import gc
from threading import Barrier, Event, Lock
from weakref import ref

import pytest

from forgepilot.artifacts.sqlite_store import ArtifactStorageError
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import (
    FailureKind, GraphDefinition, GraphRunner, NodeDefinition, NodeResult,
    NodeStatus, NodeType,
)
from forgepilot.orchestration.execution import (
    ExecutionCoordinator, InProcessExecutionCoordinator, coordinator_for_storage,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.sqlite_store import SqliteEventStore
from test_graph_fanout import graph as analyst_graph


def single_graph():
    return GraphDefinition("single", 1, (
        NodeDefinition("work", NodeType.DETERMINISTIC, "work"),
    ))


def succeeded(context):
    return NodeResult(NodeStatus.SUCCEEDED)


class RecordingCoordinator:
    """A structural protocol implementation, deliberately not a subclass."""

    def __init__(self):
        self.delegate = InProcessExecutionCoordinator()
        self.lock = Lock()
        self.claims = []
        self.ends = []
        self.waiter_claimed = Event()

    def begin_execution(self, task_id):
        claimed = self.delegate.begin_execution(task_id)
        with self.lock:
            self.claims.append((task_id, claimed))
        if task_id == "waiter" and claimed:
            self.waiter_claimed.set()
        return claimed

    def end_execution(self, task_id):
        self.delegate.end_execution(task_id)
        with self.lock:
            self.ends.append(task_id)

    def acquire_execution_slot(self, *, blocking=True, timeout=-1):
        return self.delegate.acquire_execution_slot(blocking=blocking, timeout=timeout)

    def release_execution_slot(self):
        self.delegate.release_execution_slot()

    def assert_recovery_idle(self, task_id):
        self.delegate.assert_recovery_idle(task_id)


def assert_released(coordinator, task_id):
    coordinator.assert_recovery_idle(task_id)
    assert coordinator.begin_execution(task_id), "Interrupted batch retained its task marker"
    coordinator.end_execution(task_id)
    assert coordinator.acquire_execution_slot(blocking=False), "Interrupted batch retained its slot"
    coordinator.release_execution_slot()


def test_concurrent_runner_construction_shares_one_storage_coordinator():
    store = SqliteEventStore()
    barrier = Barrier(6)

    def construct(index):
        barrier.wait(timeout=5)
        return GraphRunner(store, HandlerRegistry({"work": succeeded}))

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            runners = list(pool.map(construct, range(6)))
        shared = coordinator_for_storage(store)
        assert all(runner.coordinator is shared and runner.recovery.coordinator is shared
                   for runner in runners)
        assert runners[0].run("shared", single_graph()).status is NodeStatus.SUCCEEDED
        assert not hasattr(store, "_graph_execution_lock")
        assert not hasattr(store, "_graph_executing")
    finally:
        store.close()


def test_default_coordinator_registry_does_not_keep_storage_alive():
    class StorageOwner:
        pass

    storage = StorageOwner()
    storage_ref = ref(storage)
    coordinator = coordinator_for_storage(storage)
    assert coordinator_for_storage(storage) is coordinator
    del storage
    gc.collect()
    assert storage_ref() is None
    assert_released(coordinator, "still-usable")


def test_independent_databases_execute_the_same_task_id_without_global_exclusion():
    first_entered, second_entered, release = Event(), Event(), Event()
    stores = [SqliteEventStore(), SqliteEventStore()]

    def hold(entered):
        def handler(context):
            entered.set()
            assert release.wait(timeout=5)
            return NodeResult(NodeStatus.SUCCEEDED)
        return handler

    try:
        first = GraphRunner(stores[0], HandlerRegistry({"work": hold(first_entered)}))
        second = GraphRunner(stores[1], HandlerRegistry({"work": hold(second_entered)}))
        assert first.coordinator is not second.coordinator
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                one = pool.submit(first.run, "same-task", single_graph())
                assert first_entered.wait(timeout=5)
                two = pool.submit(second.run, "same-task", single_graph())
                assert second_entered.wait(timeout=5), "Independent storage waited on another database"
                assert not one.done() and not two.done()
            finally:
                release.set()
            assert one.result(timeout=5).status is NodeStatus.SUCCEEDED
            assert two.result(timeout=5).status is NodeStatus.SUCCEEDED
    finally:
        release.set()
        for store in stores:
            store.close()


def test_injected_protocol_controls_reentry_and_recovery_before_a_node_is_claimed():
    store = SqliteEventStore()
    recording = RecordingCoordinator()
    coordinator: ExecutionCoordinator = recording
    calls = []
    runner = GraphRunner(store, HandlerRegistry({
        "work": lambda context: calls.append(context.attempt) or NodeResult(NodeStatus.SUCCEEDED),
    }), coordinator=coordinator)
    try:
        assert runner.coordinator is coordinator and runner.recovery.coordinator is coordinator
        assert coordinator.begin_execution("task")
        paused = runner.run("task", single_graph())
        assert paused.nodes[0].status is NodeStatus.READY
        assert paused.nodes[0].attempt == 0 and calls == []
        with pytest.raises(ForgePilotError) as failure:
            runner.recovery.prepare("task", single_graph())
        assert failure.value.code == "graph_active"
        coordinator.end_execution("task")
        assert runner.run("task", single_graph()).status is NodeStatus.SUCCEEDED
        assert calls == [1]
        assert recording.claims == [("task", True), ("task", False), ("task", True)]
        assert recording.ends == ["task", "task"]
    finally:
        store.close()


def test_handler_reentry_through_another_runner_returns_live_snapshot_without_replay():
    store = SqliteEventStore()
    coordinator = InProcessExecutionCoordinator()
    calls = []

    def work(context):
        calls.append(context.attempt)
        nested = peer.run(context.task_id, single_graph())
        assert nested.nodes[0].status is NodeStatus.RUNNING
        assert nested.nodes[0].attempt == 1
        with pytest.raises(ForgePilotError) as failure:
            peer.prepare_recovery(context.task_id, single_graph())
        assert failure.value.code == "graph_active"
        return NodeResult(NodeStatus.SUCCEEDED)

    try:
        handlers = HandlerRegistry({"work": work})
        owner = GraphRunner(store, handlers, coordinator=coordinator)
        peer = GraphRunner(store, handlers, coordinator=coordinator)
        result = owner.run("reentrant", single_graph())
        assert result.status is NodeStatus.SUCCEEDED
        assert calls == [1]
        assert len([event for event in owner.events("reentrant")
                    if event.event_type == "node_started"]) == 1
        assert_released(coordinator, "reentrant")
    finally:
        store.close()


def test_cancelled_waiter_releases_its_marker_without_releasing_another_tasks_slot():
    store = SqliteEventStore()
    coordinator = RecordingCoordinator()
    owner_entered, release = Event(), Event()
    calls = []

    def hold(context):
        calls.append(context.task_id)
        owner_entered.set()
        assert release.wait(timeout=5)
        return NodeResult(NodeStatus.SUCCEEDED)

    try:
        runner = GraphRunner(store, HandlerRegistry({"work": hold}), coordinator=coordinator)
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                owner = pool.submit(runner.run, "owner", single_graph())
                assert owner_entered.wait(timeout=5)
                waiter = pool.submit(runner.run, "waiter", single_graph())
                assert coordinator.waiter_claimed.wait(timeout=5)
                current = runner.store.snapshot("waiter")
                runner.cancel("waiter", expected_revision=current.revision)
                cancelled = waiter.result(timeout=2)
                assert cancelled.status is NodeStatus.CANCELLED
                assert cancelled.nodes[0].attempt == 0
                coordinator.assert_recovery_idle("waiter")
                assert coordinator.begin_execution("waiter")
                coordinator.end_execution("waiter")
                assert not coordinator.acquire_execution_slot(blocking=False)
                assert not owner.done() and calls == ["owner"]
            finally:
                release.set()
            assert owner.result(timeout=5).status is NodeStatus.SUCCEEDED
        assert_released(coordinator, "owner")
    finally:
        release.set()
        store.close()


class SimulatedInterrupt(BaseException):
    pass


@pytest.mark.parametrize("failure", ["handler", "artifact", "publication", "interrupt"])
def test_failed_or_interrupted_batch_releases_slot_and_marker_without_changing_durable_result(failure):
    store = SqliteEventStore()
    coordinator = InProcessExecutionCoordinator()

    def work(context):
        if context.task_id == "broken":
            if failure == "handler":
                raise ValueError("controlled handler failure")
            if failure == "artifact":
                raise ArtifactStorageError("controlled Artifact failure")
            if failure == "interrupt":
                raise SimulatedInterrupt()
        return NodeResult(NodeStatus.SUCCEEDED)

    try:
        runner = GraphRunner(store, HandlerRegistry({"work": work}), coordinator=coordinator)
        if failure == "publication":
            store._db.execute("""CREATE TRIGGER reject_finished BEFORE INSERT ON graph_events
                WHEN NEW.task_id='broken' AND NEW.event_type='node_finished'
                BEGIN SELECT RAISE(ABORT,'controlled publication failure'); END""")
        if failure == "handler":
            failed = runner.run("broken", single_graph())
            assert failed.status is NodeStatus.RETRYABLE
            assert failed.nodes[0].result.failure_kind is FailureKind.TOOL_TRANSIENT
        elif failure == "interrupt":
            with pytest.raises(SimulatedInterrupt):
                runner.run("broken", single_graph())
        else:
            with pytest.raises(ForgePilotError) as error:
                runner.run("broken", single_graph())
            assert error.value.code == ("graph_storage" if failure == "artifact" else "graph_conflict")
        if failure != "handler":
            persisted = runner.store.snapshot("broken")
            assert persisted.nodes[0].status is NodeStatus.RUNNING
            assert persisted.nodes[0].attempt == 1 and persisted.nodes[0].result is None
            assert not any(event.event_type == "node_finished" for event in runner.events("broken"))
        assert_released(coordinator, "broken")
        next_runner = GraphRunner(store, HandlerRegistry({"work": work}), coordinator=coordinator)
        assert next_runner.run("next-task", single_graph()).status is NodeStatus.SUCCEEDED
    finally:
        store.close()


def test_parallel_storage_failure_joins_live_sibling_before_releasing_batch(monkeypatch):
    store = SqliteEventStore()
    coordinator = InProcessExecutionCoordinator()
    slow_entered, branch_failed, release, slow_finished = Event(), Event(), Event(), Event()

    def analyze(context):
        if context.node.node_id == "root":
            assert slow_entered.wait(timeout=5)
            raise ArtifactStorageError("controlled parallel Artifact failure")
        slow_entered.set()
        assert release.wait(timeout=5)
        slow_finished.set()
        return NodeResult(NodeStatus.SUCCEEDED)

    try:
        runner = GraphRunner(store, HandlerRegistry({
            "analyze": analyze, "merge": lambda context: pytest.fail("Join ran after storage failure"),
        }), coordinator=coordinator)
        original = runner._execute_parallel

        def observed(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except ForgePilotError as error:
                if args[2].node_id == "root":
                    assert error.code == "graph_storage"
                    branch_failed.set()
                raise

        monkeypatch.setattr(runner, "_execute_parallel", observed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                running = pool.submit(runner.run, "parallel", analyst_graph())
                assert branch_failed.wait(timeout=5)
                assert not running.done() and not slow_finished.is_set()
                assert not coordinator.acquire_execution_slot(blocking=False)
                assert not coordinator.begin_execution("parallel")
                with pytest.raises(ForgePilotError) as failure:
                    runner.prepare_recovery("parallel", analyst_graph())
                assert failure.value.code == "graph_active"
            finally:
                release.set()
            with pytest.raises(ForgePilotError) as failure:
                running.result(timeout=5)
            assert failure.value.code == "graph_storage"
        assert slow_finished.is_set()
        assert_released(coordinator, "parallel")
        persisted = runner.store.snapshot("parallel")
        assert all(node.attempt == 1 and node.result is None for node in persisted.nodes
                   if node.node_id in {"root", "impact"})
        assert next(node for node in persisted.nodes if node.node_id == "join").attempt == 0
    finally:
        release.set()
        store.close()
