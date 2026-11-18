"""The single worker loop shared by legacy tasks, conversations and Missions.

Business admission and cancellation belong to the hosts.  This component only
owns cross-thread execution and the lifetime of the existing runtime thread.
"""

import asyncio
from threading import Event, RLock, Thread

from .errors import ForgePilotError


class HostRuntime:
    def __init__(self):
        self._ready = Event()
        self._state = RLock()
        self._loop = None
        self._thread = None
        self._stopping = False
        self._closing = False
        self._close_future = None
        self._operations = set()  # Worker-loop owned; no HTTP-loop Tasks/Futures.

    @property
    def available(self):
        return self._loop is not None and not self._loop.is_closed()

    @property
    def stopping(self):
        return self._stopping

    @property
    def thread_alive(self):
        return self._thread is not None and self._thread.is_alive()

    async def start(self):
        def work():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.run_until_complete(self._loop.shutdown_default_executor())
            finally:
                self._loop.close()

        with self._state:
            if self._closing:
                raise ForgePilotError("server_stopping", "Runtime is not available")
            if self._thread is None:
                self._thread = Thread(target=work, name="ForgePilot-Runtime", daemon=False)
                self._thread.start()
        await asyncio.to_thread(self._ready.wait)

    def stop_accepting(self):
        """Worker-loop only: stop business admission, retaining control operations."""
        self._stopping = True

    async def begin_shutdown(self):
        async def stop():
            self.stop_accepting()
        await self.submit(stop())

    async def _execute(self, operation):
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            return await operation
        finally:
            self._operations.discard(task)

    def _schedule(self, operation):
        with self._state:
            if not self.available or self._closing:
                operation.close()
                raise ForgePilotError("server_stopping", "Runtime is not available")
            # Serialize scheduling with final close: every accepted operation is
            # queued before the closing coroutine and joins its worker-owned set.
            future = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                self._execute(operation), self._loop))
        # A disconnected waiter must not leave an unobserved background failure.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return future

    async def submit(self, operation):
        """Run accepted work; cancelling the HTTP waiter never cancels the work."""
        return await asyncio.shield(self._schedule(operation))

    async def drain(self, operation):
        """Join host shutdown before propagating cancellation of its waiter."""
        return await self._join(self._schedule(operation))

    async def run_request(self, operation):
        """Cancel request-scoped work on disconnect and join its actual cleanup.

        Cancelling run_coroutine_threadsafe's Future can signal completion before
        the worker's finally blocks finish. Cancel the owned Task instead, then
        join the original Future, including after repeated waiter cancellation.
        """
        cancelled, owned = Event(), []

        async def execute():
            owned.append(asyncio.current_task())
            if cancelled.is_set():
                operation.close()
                raise asyncio.CancelledError()
            return await operation

        wrapper = execute()
        try:
            future = self._schedule(wrapper)
        except BaseException:
            operation.close()
            raise

        def cancel_owned():
            if owned and not owned[0].done() and not owned[0].cancelling():
                owned[0].cancel()

        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled.set()
            if self.available:
                self._loop.call_soon_threadsafe(cancel_owned)
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()
            raise

    @staticmethod
    async def _join(future):
        """Finish lifecycle cleanup before propagating waiter cancellation."""
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not future.cancelled():
                future.exception()
            raise asyncio.CancelledError()
        return future.result()

    async def _drain(self, drain, close_resources):
        self.stop_accepting()
        try:
            if drain is not None:
                await drain()
        finally:
            try:
                pending = tuple(self._operations)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                if close_resources is not None:
                    await close_resources()

    async def close(self, drain=None, close_resources=None):
        """Stop admission, run the host drain callback, then stop and join once.

        Hosts cancel and drain their own background tasks. The runtime joins all
        submitted controls before invoking the shared model/storage closer.
        """
        with self._state:
            if self._close_future is None:
                if not self.available:
                    return
                self._closing = True
                self._close_future = asyncio.run_coroutine_threadsafe(
                    self._drain(drain, close_resources), self._loop)
                # Let run_coroutine_threadsafe publish completion before stopping
                # the loop, so the cross-thread closing Future always settles.
                self._close_future.add_done_callback(
                    lambda _: self._loop.call_soon_threadsafe(self._loop.stop))
            future = asyncio.wrap_future(self._close_future)
        try:
            await self._join(future)
        finally:
            await self._join(asyncio.create_task(asyncio.to_thread(self._thread.join)))
