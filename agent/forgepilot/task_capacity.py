"""Worker-loop admission accounting shared by v1, chat and durable Missions.

Checks and reservations are synchronous: callers must perform them on the single
runtime loop before yielding. A Mission admission stays reserved until its
durable task.created record is committed (or admission cleanup has finished).
"""

from contextlib import contextmanager

from .errors import ForgePilotError


class TaskCapacity:
    def __init__(self, max_tasks=32, *, mission_count=lambda: 0):
        self.max_tasks = max_tasks
        self._mission_count = mission_count
        self._active = set()

    def active_count(self, kind=None):
        if kind is not None:
            return sum(category == kind for category, _ in self._active)
        return len(self._active) + self._mission_count()

    def check_available(self, message):
        if self.active_count() >= self.max_tasks:
            raise ForgePilotError("task_capacity", message)

    def acquire(self, kind, key, *, message):
        identity = (kind, key)
        if identity not in self._active:
            self.check_available(message)
            self._active.add(identity)

    def release(self, kind, key):
        self._active.discard((kind, key))

    @contextmanager
    def admission(self, message):
        token = object()
        self.acquire("mission_admission", token, message=message)
        try:
            yield
        finally:
            self.release("mission_admission", token)
