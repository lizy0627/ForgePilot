"""Ownership, disconnect and shutdown contracts of the shared worker runtime."""

import asyncio
import gc
import inspect
from threading import Event, current_thread, get_ident
import warnings

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.host_runtime import HostRuntime


@pytest.mark.asyncio
async def test_runtime_uses_one_worker_loop_and_joins_thread():
    runtime = HostRuntime()
    caller_loop, caller_thread = asyncio.get_running_loop(), get_ident()
    await runtime.start()
    try:
        await runtime.start()

        async def identity():
            await asyncio.sleep(0)
            return asyncio.get_running_loop(), get_ident(), current_thread().name

        results = await asyncio.gather(*(runtime.submit(identity()) for _ in range(8)))
        loop, thread, name = results[0]
        assert all(result == results[0] for result in results)
        assert loop is not caller_loop and thread != caller_thread
        assert name == "ForgePilot-Runtime" and runtime.thread_alive
    finally:
        await runtime.close()
    assert runtime.stopping and not runtime.available and not runtime.thread_alive


@pytest.mark.asyncio
async def test_disconnected_accepted_work_survives_and_late_failure_is_observed():
    runtime = HostRuntime()
    entered, completed = Event(), Event()
    signals, loop_errors = [], []
    await runtime.start()
    try:
        async def operation():
            asyncio.get_running_loop().set_exception_handler(lambda _loop, context: loop_errors.append(context))
            release = asyncio.Event()
            signals.append(release)
            entered.set()
            await release.wait()
            completed.set()
            raise RuntimeError("background fixture failure")

        waiter = asyncio.create_task(runtime.submit(operation()))
        assert await asyncio.to_thread(entered.wait, 2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not completed.is_set()

        async def release():
            signals[0].set()

        await runtime.submit(release())
        assert await asyncio.to_thread(completed.wait, 2)
    finally:
        await runtime.close()
    assert loop_errors == []


@pytest.mark.asyncio
async def test_request_cancellation_joins_cleanup_despite_repeated_cancellation():
    runtime = HostRuntime()
    entered, cleaning, cleaned = Event(), Event(), Event()
    await runtime.start()
    try:
        async def operation():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await asyncio.sleep(.05)
                cleaned.set()

        waiter = asyncio.create_task(runtime.run_request(operation()))
        assert await asyncio.to_thread(entered.wait, 2)
        waiter.cancel()
        assert await asyncio.to_thread(cleaning.wait, 2)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert cleaned.is_set()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_host_drain_joins_cleanup_before_propagating_repeated_cancellation():
    runtime = HostRuntime()
    entered, release, cleaned = Event(), Event(), Event()
    await runtime.start()

    async def cleanup():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.001)
        cleaned.set()

    waiter = asyncio.create_task(runtime.drain(cleanup()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done() and not cleaned.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await runtime.close()
    assert cleaned.is_set() and not runtime.thread_alive


@pytest.mark.asyncio
async def test_stopping_retains_controls_until_final_close_and_closes_rejected_coroutines():
    runtime = HostRuntime()
    await runtime.start()
    try:
        await runtime.begin_shutdown()
        assert runtime.stopping

        async def control():
            return "drained"

        assert await runtime.submit(control()) == "drained"
    finally:
        await runtime.close()
    rejected = control()
    with pytest.raises(ForgePilotError, match="server_stopping"):
        await runtime.submit(rejected)
    assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED
    rejected = control()
    with pytest.raises(ForgePilotError, match="server_stopping"):
        await runtime.run_request(rejected)
    assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_close_joins_submitted_operations_and_survives_cancelled_waiter():
    runtime = HostRuntime()
    entered, draining, cleaned = Event(), Event(), Event()
    signals, order = [], []
    await runtime.start()

    async def operation():
        release = asyncio.Event()
        signals.append(release)
        entered.set()
        await release.wait()
        await asyncio.sleep(.05)
        order.append("operation_closed")
        cleaned.set()

    async def drain():
        assert runtime.stopping
        order.append("drain")
        signals[0].set()
        draining.set()

    operation_waiter = asyncio.create_task(runtime.submit(operation()))
    assert await asyncio.to_thread(entered.wait, 2)
    closer = asyncio.create_task(runtime.close(drain))
    assert await asyncio.to_thread(draining.wait, 2)
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    await operation_waiter
    assert cleaned.is_set() and not runtime.thread_alive
    assert order == ["drain", "operation_closed"]
    await runtime.close(drain)
    assert order == ["drain", "operation_closed"]


@pytest.mark.asyncio
async def test_concurrent_close_runs_drain_once_and_also_joins_after_failure():
    runtime = HostRuntime()
    await runtime.start()
    calls = []

    async def drain():
        calls.append("drain")
        await asyncio.sleep(.01)
        raise ValueError("fixture close failure")

    results = await asyncio.gather(runtime.close(drain), runtime.close(drain), return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert calls == ["drain"] and not runtime.thread_alive


@pytest.mark.asyncio
async def test_shared_resources_close_after_drain_and_accepted_controls_settle():
    runtime = HostRuntime()
    entered, drained, release = Event(), Event(), Event()
    order = []
    await runtime.start()

    async def operation():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.001)
        order.append("control_closed")

    async def drain():
        order.append("hosts_drained")
        drained.set()

    async def close_resources():
        assert release.is_set()
        assert order == ["hosts_drained", "control_closed"]
        order.append("resources_closed")

    waiter = asyncio.create_task(runtime.submit(operation()))
    assert await asyncio.to_thread(entered.wait, 2)
    closer = asyncio.create_task(runtime.close(drain, close_resources))
    try:
        assert await asyncio.to_thread(drained.wait, 2)
        await asyncio.sleep(.01)
        assert order == ["hosts_drained"] and not closer.done()
    finally:
        release.set()
        await closer
        await waiter
    assert order == ["hosts_drained", "control_closed", "resources_closed"]
    assert not runtime.thread_alive


@pytest.mark.asyncio
async def test_rejected_mission_controls_leave_no_unawaited_coroutines(tmp_path):
    from forgepilot.mission_host import MissionHost
    from forgepilot.sqlite_store import SqliteEventStore
    from forgepilot.task_capacity import TaskCapacity

    runtime = HostRuntime()
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    missions = MissionHost(runtime=runtime, capacity=TaskCapacity(1), store=store,
                           mcp_service=object(), mcp_disabled=True)
    await runtime.start()
    try:
        await missions.close()
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", RuntimeWarning)
            for runtime_closed in (False, True):
                if runtime_closed:
                    await runtime.close()
                operations = (
                    missions.create(),
                    missions.create_locked(None),
                    missions.approve("task"),
                    missions.cancel("task", expected_revision=1),
                    missions.retry("task", expected_revision=1),
                    missions.resume("task", expected_revision=1),
                    missions.delivery("task", expected_revision=1),
                )
                for operation in operations:
                    with pytest.raises(ForgePilotError) as rejected:
                        await operation
                    assert rejected.value.code == "server_stopping"
                del operations, operation
                gc.collect()
        assert not [warning for warning in observed if "was never awaited" in str(warning.message)]
    finally:
        await runtime.close()
        store.close()
