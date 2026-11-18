"""Offline integration checks for the shared runtime and admission boundary."""

import asyncio
import json
from threading import Event, current_thread
from uuid import uuid4

import pytest

from forgepilot.conversation_host import ConversationHost
from forgepilot.errors import ForgePilotError
from test_mission_service import ControlledWorkflow, create, settled, setup
from test_workspace_manager import committed_repo


class WaitingAnswer:
    def __init__(self):
        self.closed = False
        self.owner = None

    async def answer(self, *args, **kwargs):
        self.owner = (asyncio.get_running_loop(), current_thread().name)
        await asyncio.Future()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("occupant", ["chat", "mission"])
async def test_chat_and_mission_capacity_is_shared_in_both_directions(
        tmp_path, git_exe, committed_repo, occupant):
    tasks, missions = await setup(tmp_path, git_exe, max_tasks=1)
    answers = []

    def chat_factory():
        service = WaitingAnswer()
        answers.append(service)
        return service

    conversations = ConversationHost(tasks, chat_factory)
    cid = tasks.store.create_conversation()["conversation_id"]
    rid = str(uuid4())
    try:
        if occupant == "chat":
            await conversations.submit(cid, rid, "chat", "offline question", tool_mode="fast")
            with pytest.raises(ForgePilotError) as denied:
                await create(missions, committed_repo)
            assert denied.value.code == "task_capacity"
            with pytest.raises(ForgePilotError, match="task_capacity"):
                await tasks.create(str(committed_repo), "offline task")
            assert tasks.capacity.active_count() == 1
            assert (await conversations.stop(cid, rid))["status"] == "cancelled"
            assert answers[0].closed
            assert tasks.capacity.active_count() == 0
            mission = await settled(missions, (await create(missions, committed_repo))["task_id"])
            await missions.cancel(mission["task_id"], expected_revision=mission["current_revision"])
        else:
            mission = await settled(missions, (await create(missions, committed_repo))["task_id"])
            assert mission["status"] == "awaiting_human"
            with pytest.raises(ForgePilotError) as denied:
                await conversations.submit(cid, rid, "chat", "offline question", tool_mode="fast")
            assert denied.value.code == "task_capacity" and not answers
            with pytest.raises(ForgePilotError, match="task_capacity"):
                await tasks.create(str(committed_repo), "offline task")
            assert tasks.capacity.active_count() == 1
            await missions.cancel(mission["task_id"], expected_revision=mission["current_revision"])
            assert tasks.capacity.active_count() == 0
            await conversations.submit(cid, rid, "chat", "offline question", tool_mode="fast")
            assert (await conversations.stop(cid, rid))["status"] == "cancelled"
            assert answers[0].closed
        assert tasks.capacity.active_count() == 0
        assert answers[0].owner[1] == "ForgePilot-Runtime"
    finally:
        await missions.close()
        await conversations.close()
        await tasks.close()
        tasks.store.close()
    assert not tasks.runtime.thread_alive


@pytest.mark.asyncio
async def test_mission_background_failure_is_durable_safe_and_releases_capacity(
        tmp_path, git_exe, committed_repo):
    tasks, missions = await setup(tmp_path, git_exe, max_tasks=1)
    failures = []
    secret = "private offline exception contents"

    class FailingWorkflow(ControlledWorkflow):
        async def run(self, task_id):
            raise RuntimeError(secret)

    missions.factory = lambda events: FailingWorkflow(events, tasks.store.workspace_backend)

    async def observe_failures():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: failures.append(context))

    await tasks.runtime.submit(observe_failures())
    try:
        tid = (await create(missions, committed_repo))["task_id"]
        mission = await settled(missions, tid)
        assert mission["status"] == "failed"
        assert tasks.capacity.active_count() == 0
        assert not missions._running and tid not in missions._services
        with tasks.store._lock:
            rows = tasks.store._db.execute("SELECT payload_json FROM graph_events "
                "WHERE task_id=? AND event_type='task.failed'", (tid,)).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["error_code"] == "mission_internal"
        assert secret not in rows[0][0] and not failures
    finally:
        await missions.close()
        await tasks.close()
        tasks.store.close()
    assert not tasks.runtime.thread_alive and not failures


@pytest.mark.asyncio
async def test_shutdown_joins_accepted_mission_control_and_its_late_launch(
        tmp_path, git_exe, committed_repo, monkeypatch):
    tasks, missions = await setup(tmp_path, git_exe)
    mission = await settled(missions, (await create(missions, committed_repo))["task_id"])
    tid = mission["task_id"]
    entered, release = Event(), Event()
    order, owners = [], []

    def confirm_recovery():
        entered.set()
        assert release.wait(10), "Test did not release accepted recovery control"

    async def late_work():
        owners.append(asyncio.get_running_loop())
        order.append("late_started")
        try:
            await asyncio.Future()
        finally:
            order.append("late_closed")

    async def resume_after_confirmation(task_id, revision, token):
        owners.append(asyncio.get_running_loop())
        await asyncio.to_thread(confirm_recovery)
        order.append("control_committed")
        missions._launch(task_id, late_work())
        return {"task_id": task_id}

    monkeypatch.setattr(missions, "_resume", resume_after_confirmation)

    async def close_models():
        assert "late_closed" in order and not missions._running
        order.append("models_closed")

    async def shutdown():
        await tasks.runtime.begin_shutdown()
        await missions.close()
        await tasks.close(close_resources=close_models)

    control = asyncio.create_task(missions.resume(tid, expected_revision=mission["current_revision"]))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        closing = asyncio.create_task(shutdown())
        async with asyncio.timeout(3):
            while not missions._stopping:
                await asyncio.sleep(.01)
        assert not closing.done() and not order
        release.set()
        assert await control == {"task_id": tid}
        await asyncio.wait_for(closing, 5)
        assert order == ["control_committed", "late_started", "late_closed", "models_closed"]
        assert len(owners) == 2 and owners[0] is owners[1]
        assert not tasks.runtime.thread_alive and not missions._running
    finally:
        release.set()
        await asyncio.gather(control, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        if tasks.runtime.available:
            await missions.close()
            await tasks.close()
        tasks.store.close()
