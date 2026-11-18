import asyncio

import pytest

from forgepilot.process import wait_for_cleanup


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_shared_cleanup():
    started, release = asyncio.Event(), asyncio.Event()
    released = []

    async def close():
        started.set()
        await release.wait()
        released.append(True)

    cleanup = asyncio.create_task(close())
    cancelled = asyncio.create_task(wait_for_cleanup(cleanup))
    other = asyncio.create_task(wait_for_cleanup(cleanup))
    try:
        await started.wait()
        for _ in range(3):
            cancelled.cancel()
            await asyncio.sleep(0)
            assert not cancelled.done() and not cleanup.done() and not released
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await other
        await wait_for_cleanup(cleanup)  # Joining completed cleanup is idempotent.
        assert released == [True]
    finally:
        release.set()
        await asyncio.gather(cancelled, other, cleanup, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_cleanup_failure_propagates_to_every_waiter(error):
    async def close():
        raise error()

    cleanup = asyncio.create_task(close())
    for _ in range(2):
        with pytest.raises(error):
            await wait_for_cleanup(cleanup)
    assert cleanup.done()
