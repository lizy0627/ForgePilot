"""Shared capacity includes live requests, durable Missions and admissions."""

import asyncio

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import append_mission_event
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_capacity import TaskCapacity


def test_shared_capacity_counts_v1_chat_durable_missions_and_admissions():
    persisted = 1
    capacity = TaskCapacity(4, mission_count=lambda: persisted)
    capacity.acquire("v1", "task", message="full")
    capacity.acquire("chat", "answer", message="full")
    with capacity.admission("full"):
        assert capacity.active_count() == 4
        assert capacity.active_count("v1") == 1
        assert capacity.active_count("chat") == 1
        assert capacity.active_count("mission_admission") == 1
        with pytest.raises(ForgePilotError) as denied:
            capacity.acquire("chat", "second-answer", message="full")
        assert denied.value.code == "task_capacity"
    assert capacity.active_count() == 3
    persisted = 0
    assert capacity.active_count() == 2
    capacity.acquire("chat", "second-answer", message="full")
    assert capacity.active_count() == 3


@pytest.mark.asyncio
async def test_concurrent_mission_admission_reserves_capacity_before_first_await():
    capacity = TaskCapacity(1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        with capacity.admission("Mission capacity reached"):
            entered.set()
            await release.wait()

    pending = asyncio.create_task(first())
    try:
        await entered.wait()
        with pytest.raises(ForgePilotError) as denied:
            with capacity.admission("Mission capacity reached"):
                pytest.fail("Concurrent admission exceeded capacity")
        assert denied.value.code == "task_capacity"
        assert capacity.active_count() == 1
    finally:
        release.set()
        await pending
    assert capacity.active_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_or_cancelled_mission_admission_releases_reservation(failure):
    capacity = TaskCapacity(1)
    entered = asyncio.Event()

    async def admit():
        with capacity.admission("full"):
            entered.set()
            if failure == "error":
                raise RuntimeError("offline admission failure")
            await asyncio.Future()

    pending = asyncio.create_task(admit())
    await entered.wait()
    if failure == "cancel":
        pending.cancel()
    expected = RuntimeError if failure == "error" else asyncio.CancelledError
    with pytest.raises(expected):
        await pending
    assert capacity.active_count() == 0
    with capacity.admission("full"):
        assert capacity.active_count() == 1


def test_repeated_acquire_and_release_do_not_consume_or_create_capacity():
    capacity = TaskCapacity(1)
    capacity.acquire("v1", "task", message="full")
    capacity.acquire("v1", "task", message="full")
    assert capacity.active_count() == 1
    capacity.release("v1", "unknown")
    assert capacity.active_count() == 1
    capacity.release("v1", "task")
    capacity.release("v1", "task")
    assert capacity.active_count() == 0
    capacity.acquire("chat", "task", message="full")
    assert capacity.active_count() == 1


def _persist_mission(store, task_id, status, *, admitted=True):
    store.missions.create_graph_run(task_id=task_id, graph_id="capacity-fixture",
                                    graph_version=1, status=status)
    if admitted:
        with store._transaction():
            append_mission_event(store, task_id, "task.created", {})


def test_durable_count_excludes_terminal_and_unadmitted_graphs(tmp_path):
    store = SqliteEventStore(tmp_path / "capacity.sqlite3", artifact_root=tmp_path / "artifacts")
    try:
        _persist_mission(store, "graph-without-admission", "pending", admitted=False)
        for status in ("succeeded", "failed", "cancelled", "completed", "skipped"):
            _persist_mission(store, "terminal-" + status, status)
        assert store.missions.active_count() == 0
        for status in ("pending", "running", "awaiting_human", "retryable", "interrupted"):
            _persist_mission(store, "active-" + status, status)
        capacity = TaskCapacity(5, mission_count=store.missions.active_count)
        assert capacity.active_count() == 5
        with pytest.raises(ForgePilotError, match="full"):
            capacity.check_available("full")
        store.missions.update_graph_run("active-awaiting_human", previous_revision=2, status="cancelled")
        assert capacity.active_count() == 4
        capacity.check_available("full")
    finally:
        store.close()


def test_reopened_unfinished_mission_still_occupies_capacity(tmp_path):
    database, artifacts = tmp_path / "reopen.sqlite3", tmp_path / "artifacts"
    original = SqliteEventStore(database, artifact_root=artifacts)
    try:
        _persist_mission(original, "paused-mission", "awaiting_human")
        assert original.missions.active_count() == 1
    finally:
        original.close()
    reopened = SqliteEventStore(database, artifact_root=artifacts)
    try:
        run = reopened.missions.get_graph_run("paused-mission")
        assert run["recovery_required"] == 1
        assert reopened.missions.active_count() == 1
        capacity = TaskCapacity(1, mission_count=reopened.missions.active_count)
        with pytest.raises(ForgePilotError) as denied:
            capacity.acquire("v1", "replacement", message="full")
        assert denied.value.code == "task_capacity"
        reopened.missions.update_graph_run("paused-mission", previous_revision=run["current_revision"],
                                           status="cancelled")
        assert capacity.active_count() == 0
        capacity.acquire("chat", "replacement", message="full")
    finally:
        reopened.close()
