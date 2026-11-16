"""In-process Graph execution ownership, independent of persistence.

A slot covers one serial node or a joined two-Analyst batch, including checkpoint
publication. The active set also covers tasks waiting for that slot, preventing
same-task reentry. Neither lock is a database transaction or a repository lock.
"""
from __future__ import annotations

from threading import Lock
from typing import Protocol
from weakref import WeakKeyDictionary

from ..errors import ForgePilotError


class ExecutionCoordinator(Protocol):
    """Shared by all runners using the same storage owner.

    begin_execution returns False for an already active task. No method calls
    persistence or holds a coordination lock across a caller's repository read.
    """

    def begin_execution(self, task_id: str) -> bool: ...

    def end_execution(self, task_id: str) -> None: ...

    def acquire_execution_slot(self, *, blocking: bool = True, timeout: float = -1) -> bool: ...

    def release_execution_slot(self) -> None: ...

    def assert_recovery_idle(self, task_id: str) -> None: ...


class InProcessExecutionCoordinator:
    """One non-reentrant execution slot and one guarded set of active tasks."""

    def __init__(self) -> None:
        self._execution_lock = Lock()
        self._active_lock = Lock()
        self._active: set[str] = set()

    def begin_execution(self, task_id: str) -> bool:
        with self._active_lock:
            if task_id in self._active:
                return False
            self._active.add(task_id)
            return True

    def end_execution(self, task_id: str) -> None:
        with self._active_lock:
            self._active.discard(task_id)

    def acquire_execution_slot(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        return self._execution_lock.acquire(blocking=blocking, timeout=timeout)

    def release_execution_slot(self) -> None:
        self._execution_lock.release()

    def assert_recovery_idle(self, task_id: str) -> None:
        with self._active_lock:
            if task_id in self._active:
                raise ForgePilotError("graph_active", "A live node must stop before preparing recovery")


# Compatibility composition for embedded callers and startup recovery. The map
# never owns a storage object; a coordinator never refers back to its storage.
# This lock serializes lookup/creation only, never Graph execution across stores.
_coordinators: WeakKeyDictionary[object, ExecutionCoordinator] = WeakKeyDictionary()
_registry_lock = Lock()


def coordinator_for_storage(storage: object) -> ExecutionCoordinator:
    """Resolve one coordinator per storage instance, without changing storage.

    Production assembly injects this instance explicitly. Existing embedded
    GraphRunner/RecoveryManager calls may omit it and use the same resolver.
    Custom assembly must inject the same coordinator into all runners for a
    storage; use this resolver when mixing explicit and compatibility calls.
    """
    with _registry_lock:
        coordinator = _coordinators.get(storage)
        if coordinator is None:
            coordinator = InProcessExecutionCoordinator()
            _coordinators[storage] = coordinator
        return coordinator
