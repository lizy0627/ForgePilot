"""Transport-neutral trace events, sanitized before storage or display."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from .security import Redactor


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    task_id: str
    kind: str
    timestamp: str
    data: dict


class TraceSink(Protocol):
    def emit(self, task_id: str, kind: str, data: dict) -> None: ...


class MemoryTrace:
    def __init__(self, redactor: Redactor | None = None,
                 on_event: Callable[[TraceEvent], None] | None = None) -> None:
        self._redactor = redactor or Redactor()
        self._events: list[TraceEvent] = []
        self._on_event = on_event

    @property
    def events(self) -> list[TraceEvent]:
        return deepcopy(self._events)

    def emit(self, task_id: str, kind: str, data: dict) -> None:
        event = TraceEvent(len(self._events) + 1, task_id, kind,
                           datetime.now(timezone.utc).isoformat(),
                           deepcopy(self._redactor.data(data)))
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(deepcopy(event))
