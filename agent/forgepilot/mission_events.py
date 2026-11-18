"""V2's durable event view: one ordered SQLite log, bounded pull subscribers.

The graph log is the outbox. Writers append inside the same transaction as the
state change; readers never subscribe to an in-memory copy of uncommitted data.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from threading import RLock
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ForgePilotError


EVENT_PATTERN = r"^(task|node|artifact|approval|tool|model|validation|workspace|security)\.[a-z][a-z0-9_.]{0,95}$"
_EVENT_RE = re.compile(EVENT_PATTERN)
_TERMINAL = frozenset({"succeeded", "completed", "failed", "cancelled", "skipped"})
_PRIVATE_FIELDS = frozenset({
    "reasoning", "reasoning_content", "chain_of_thought", "hidden_thoughts", "thoughts",
    "hidden_test_source", "hidden_test_sources", "oracle_source", "environment", "env",
    "repository_contents", "repository_source", "dpapi", "dpapi_plaintext",
})


class MissionEvent(BaseModel):
    """Wire-only v2 envelope; no dependency on the v1 EventPayload model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(min_length=3, max_length=300)
    task_id: str = Field(min_length=1, max_length=256)
    task_revision: int = Field(ge=1)
    timestamp: str = Field(min_length=1, max_length=64)
    event_type: str = Field(pattern=EVENT_PATTERN)
    node_id: str | None = Field(default=None, max_length=256)
    actor: str | None = Field(default=None, max_length=256)
    data: dict[str, Any] = Field(default_factory=dict)

    def frame(self) -> str:
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {self.model_dump_json()}\n\n"


def safe_event_data(redactor, value):
    """Remove prohibited observability fields before JSON serialization."""
    def clean(item, depth=0):
        if depth > 24:
            return "[REDACTED]"
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if (
                    str(key).casefold().replace("-", "_") in _PRIVATE_FIELDS
                    or "dpapi" in str(key).casefold()
                ) else clean(content, depth + 1)
                for key, content in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(content, depth + 1) for content in item]
        # A tool response may contain nested JSON encoded as a string.
        if isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(item)
                if isinstance(decoded, (dict, list)):
                    return json.dumps(clean(decoded, depth + 1), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return item
    return redactor.data(clean(value))


def append_mission_event(store, task_id, event_type, data, *, node_id=None, actor=None):
    """Append while holding the caller's state transaction; return its sequence.

    This function deliberately refuses to create its own transaction: callers
    cannot accidentally commit a status without its associated event.
    """
    db = store._db
    if not db.in_transaction:
        raise RuntimeError("Mission events must be appended inside the state transaction")
    if not isinstance(event_type, str) or not _EVENT_RE.fullmatch(event_type):
        raise ValueError("Invalid mission event type")
    row = db.execute("SELECT current_revision FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise ForgePilotError("unknown_graph", "Mission does not exist")
    payload = safe_event_data(store.redactor, dict(data))
    if actor is not None:
        payload["_actor"] = store.redactor.text(actor)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65_536:
        raise ForgePilotError("mission_event_size", "Mission event exceeds the payload limit")
    sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM graph_events WHERE task_id=?", (task_id,)).fetchone()[0]
    revision = row[0] + 1
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    db.execute("""INSERT INTO graph_events(task_id,sequence,revision,event_type,node_id,payload_json,created_at)
                  VALUES(?,?,?,?,?,?,?)""", (task_id, sequence, revision, event_type, node_id, encoded, stamp))
    db.execute("UPDATE task_graph_runs SET current_revision=?,updated_at=? WHERE task_id=?", (revision, stamp, task_id))
    return sequence


def is_v2_mission(store, task_id):
    return store._db.execute("SELECT 1 FROM graph_events WHERE task_id=? AND event_type='task.created' LIMIT 1",
                             (task_id,)).fetchone() is not None


def append_record_event(store, task_id, event_type, data, *, node_id=None, actor=None):
    """Enable richer observability only for v2 runs, preserving legacy revisions."""
    if task_id and is_v2_mission(store, task_id):
        return append_mission_event(store, task_id, event_type, data, node_id=node_id, actor=actor)
    return None


def publish_mission_event(store, task_id, event_type, data, *, node_id=None, actor=None):
    with store._transaction():
        return append_mission_event(store, task_id, event_type, data, node_id=node_id, actor=actor)


def _wire_kind(kind, state):
    if _EVENT_RE.fullmatch(kind):
        return kind
    if kind == "graph_created":
        return "task.graph_created"
    if kind == "graph_finished":
        return "task.completed" if state == "succeeded" else "task." + (state or "finished")
    if kind == "graph_cancelled":
        return "task.cancelled"
    if kind == "node_started":
        return "node.started"
    if kind == "node_finished":
        return "approval.required" if state == "awaiting_human" else "node.completed" if state == "succeeded" else "node." + (state or "finished")
    if kind.startswith("execution_") or kind.startswith("human_"):
        return "approval." + kind.removeprefix("execution_").removeprefix("human_")
    if kind.startswith("node_"):
        return "node." + kind.removeprefix("node_")
    if kind.startswith("recovery_"):
        return "task." + kind
    return "task.state_changed"


class MissionEventStream:
    def __init__(self, store, *, heartbeat_seconds=15.0, poll_seconds=0.05,
                 max_subscribers=8, max_total_subscribers=128, page_size=64):
        if not 1 <= page_size <= 256 or not 1 <= max_subscribers <= 128:
            raise ValueError("Invalid mission stream bounds")
        if heartbeat_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("Stream intervals must be positive")
        if not 1 <= max_total_subscribers <= 1024:
            raise ValueError("Invalid global subscriber bound")
        self.store = store
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.max_subscribers = max_subscribers
        self.max_total_subscribers = max_total_subscribers
        self.page_size = page_size
        self._subscribers = {}
        self._lock = RLock()

    def cursor(self, task_id, last_event_id=None):
        after = 0
        if last_event_id is not None:
            if (not isinstance(last_event_id, str) or len(last_event_id) > 300
                    or not re.fullmatch(re.escape(task_id) + r":[1-9][0-9]{0,18}", last_event_id)):
                raise ForgePilotError("invalid_event_id", "Last-Event-ID is not a mission cursor")
            after = int(last_event_id.rsplit(":", 1)[1])
        maximum = self.store.missions.event_high_water(task_id)
        if after > maximum:
            raise ForgePilotError("event_cursor_ahead", "Last-Event-ID is ahead of the committed event log")
        return after

    def subscribe(self, task_id, last_event_id=None):
        after = self.cursor(task_id, last_event_id)
        with self._lock:
            count = self._subscribers.get(task_id, 0)
            if count >= self.max_subscribers or sum(self._subscribers.values()) >= self.max_total_subscribers:
                raise ForgePilotError("mission_stream_capacity", "Mission subscriber limit reached")
            self._subscribers[task_id] = count + 1
        return MissionSubscription(self, task_id, after)

    def subscriber_count(self, task_id):
        with self._lock:
            return self._subscribers.get(task_id, 0)

    def _release(self, task_id):
        with self._lock:
            count = self._subscribers.get(task_id, 0)
            if count <= 1:
                self._subscribers.pop(task_id, None)
            else:
                self._subscribers[task_id] = count - 1


class MissionSubscription:
    def __init__(self, stream, task_id, after):
        self.stream, self.task_id, self.after = stream, task_id, after
        self.closed = False

    def read(self):
        """Peek at one bounded page without advancing the acknowledged cursor."""
        store = self.stream.store
        run, rows, maximum = store.missions.event_page(self.task_id, self.after, self.stream.page_size)
        events = []
        for row in rows:
            payload = safe_event_data(store.redactor, json.loads(row["payload_json"]))
            actor = payload.pop("_actor", None) or row["agent_id"] or "orchestrator"
            if row["state"] is not None:
                payload.setdefault("state", row["state"])
            if row["attempt"] is not None:
                payload.setdefault("attempt", row["attempt"])
            events.append(MissionEvent(
                event_id=f"{self.task_id}:{row['sequence']}", task_id=self.task_id,
                task_revision=row["revision"], timestamp=row["created_at"],
                event_type=_wire_kind(row["event_type"], row["state"]), node_id=row["node_id"],
                actor=store.redactor.text(actor), data=payload))
        end = rows[-1]["sequence"] if rows else self.after
        return tuple(events), run["status"] in _TERMINAL and end >= maximum

    async def frames(self, request=None, *, initial=None):
        try:
            yield "retry: 1000\n\n"
            heartbeat_at = monotonic() + self.stream.heartbeat_seconds
            batch = initial
            while not self.closed:
                if request is not None and await request.is_disconnected():
                    return
                events, finished = self.read() if batch is None else batch
                batch = None
                for event in events:
                    if request is not None and await request.is_disconnected():
                        return
                    yield event.frame()
                    self.after = int(event.event_id.rsplit(":", 1)[1])
                if finished:
                    return
                if events:
                    continue
                if monotonic() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = monotonic() + self.stream.heartbeat_seconds
                # No per-event queue or retained task: a slow reader holds one page.
                await asyncio.sleep(self.stream.poll_seconds)
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.stream._release(self.task_id)
