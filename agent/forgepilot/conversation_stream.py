"""Bounded, in-memory conversation answer streams.

This module deliberately knows nothing about provider chunks or task events.  A
broker accepts only the already-redacted internal ChatService event model.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
from typing import Any

from .chat_service import ChatAnswerEvent
from .errors import ForgePilotError
from .stream_metrics import StreamMetrics


MAX_STREAM_EVENTS = 256
MAX_STREAM_BYTES = 1024 * 1024
MAX_STREAM_DRAFT = 32_768
MAX_STREAM_SUBSCRIBERS = 8


@dataclass(frozen=True, slots=True)
class ConversationStreamEvent:
    request_id: str
    revision: int
    type: str
    data: dict[str, Any]

    def frame(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.request_id}:{self.revision}\nevent: {self.type}\ndata: {payload}\n\n"

    @property
    def byte_size(self) -> int:
        return len(self.frame().encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ReplayBatch:
    events: tuple[ConversationStreamEvent, ...]
    cursor: int
    terminal: bool


class ConversationStreamSubscription:
    def __init__(self, broker: "ConversationStreamBroker", cursor: int):
        self.broker = broker
        self.cursor = cursor
        self._closed = False

    def read(self) -> ReplayBatch:
        batch = self.broker.read(self.cursor)
        self.cursor = batch.cursor
        return batch

    async def wait(self, timeout: float) -> bool:
        return await self.broker.wait(self.cursor, timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.broker._unsubscribe()


class ConversationStreamBroker:
    """A request-scoped replay buffer with snapshot recovery.

    Publishing never awaits a subscriber and never retains a subscriber queue.
    Slow subscribers therefore consume only their cursor; eviction makes them
    recover from a current safe snapshot.
    """

    def __init__(self, conversation_id: str, request_id: str, *,
                 max_events: int = MAX_STREAM_EVENTS,
                 max_bytes: int = MAX_STREAM_BYTES,
                 max_draft: int = MAX_STREAM_DRAFT,
                 max_subscribers: int = MAX_STREAM_SUBSCRIBERS,
                 metrics: StreamMetrics | None = None):
        if min(max_events, max_bytes, max_draft, max_subscribers) <= 0:
            raise ValueError("conversation stream limits must be positive")
        self.conversation_id = conversation_id
        self.request_id = request_id
        self.metrics = metrics or StreamMetrics()
        self.revision = 0
        self.current_segment_id: str | None = None
        self.draft = ""
        self.status = "running"
        self.terminal = False
        self._accepting = True
        self._stop_requested = False
        self._events: deque[ConversationStreamEvent] = deque()
        self._event_bytes = 0
        self._evicted_through = 0
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._max_draft = max_draft
        self._max_subscribers = max_subscribers
        self._subscribers = 0
        self._closed = False
        self._changed = asyncio.Event()

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def event_bytes(self) -> int:
        return self._event_bytes

    @property
    def subscriber_count(self) -> int:
        return self._subscribers

    def _notify(self) -> None:
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def _append(self, event_type: str, **values: Any) -> ConversationStreamEvent:
        previous = self.revision
        revision = previous + 1
        data = {
            "type": event_type,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "revision": revision,
            **values,
        }
        if event_type == "answer_delta":
            data["previous_revision"] = previous
        event = ConversationStreamEvent(self.request_id, revision, event_type, data)
        size = event.byte_size
        if size > self._max_bytes:
            raise ForgePilotError("conversation_stream_limit", "回答流事件超过安全上限。")
        self.revision = revision
        self._events.append(event)
        self._event_bytes += size
        self.metrics.increment("conversation_event_count")
        while len(self._events) > self._max_events or self._event_bytes > self._max_bytes:
            removed = self._events.popleft()
            self._event_bytes -= removed.byte_size
            self._evicted_through = removed.revision
        self._notify()
        return event

    def publish(self, event: ChatAnswerEvent) -> bool:
        """Publish an already-redacted ChatService event without awaiting I/O."""
        if not self._accepting or self.terminal or self._closed:
            return False
        if event.type == "segment_started":
            self.current_segment_id = event.segment_id
            self.draft = ""
            self.status = "streaming"
            self._append("answer_started", segment_id=event.segment_id)
        elif event.type == "content":
            if event.segment_id != self.current_segment_id or event.text is None:
                raise ForgePilotError("conversation_stream_state", "回答流片段状态无效。")
            if len(self.draft) + len(event.text) > self._max_draft:
                raise ForgePilotError("conversation_stream_limit", "回答草稿超过安全上限。")
            self.draft += event.text
            self._append("answer_delta", segment_id=event.segment_id, text=event.text)
            self.metrics.safe_delta(len(event.text))
        elif event.type == "segment_discarded":
            if event.segment_id != self.current_segment_id:
                raise ForgePilotError("conversation_stream_state", "回答流片段状态无效。")
            self.draft = ""
            self.status = "working"
            self._append("answer_segment_discarded", segment_id=event.segment_id)
            self.metrics.increment("stream_discarded_segments")
        elif event.type == "segment_committed":
            if event.segment_id != self.current_segment_id or event.text is None:
                raise ForgePilotError("conversation_stream_state", "回答流提交内容不一致。")
            if event.text != self.draft:
                # ChatService's controlled empty-provider fallback is a safe
                # committed string but has no provider delta of its own.
                if self.draft:
                    raise ForgePilotError("conversation_stream_state", "回答流提交内容不一致。")
                self.draft = event.text
                self._append("answer_delta", segment_id=event.segment_id, text=event.text)
                self.metrics.safe_delta(len(event.text))
            self.status = "committed"
            self._append("answer_segment_committed", segment_id=event.segment_id,
                         content=self.draft)
        elif event.type in {"tool_started", "tool_finished"}:
            self.status = "working"
            activity = "started" if event.type == "tool_started" else "finished"
            values: dict[str, Any] = {
                "segment_id": event.segment_id,
                "activity": activity,
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
            }
            if event.type == "tool_finished":
                values["success"] = bool(event.success)
            self._append("tool_activity", **values)
        else:
            raise ForgePilotError("conversation_stream_state", "回答流事件类型无效。")
        return True

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def stop(self) -> bool:
        """Linearize Stop before task cancellation without publishing terminal state."""
        if self.terminal or self._closed:
            return False
        self._accepting = False
        self._stop_requested = True
        self.status = "stopping"
        self._notify()
        return True

    def validate_completion(self, content: str) -> None:
        if self.terminal or self._closed or content != self.draft or self.status != "committed":
            raise ForgePilotError("conversation_stream_state", "回答流最终内容不一致。")

    def finish(self, status: str, *, content: str | None = None,
               message_id: int | None = None, error_code: str | None = None,
               persisted: bool = True) -> bool:
        if self.terminal or self._closed:
            return False
        if status == "completed":
            self.validate_completion(content or "")
            event_type = "answer_completed"
            values: dict[str, Any] = {"segment_id": self.current_segment_id,
                                      "content": content, "message_id": message_id}
        elif status == "cancelled":
            event_type = "answer_cancelled"
            values = {"segment_id": self.current_segment_id, "content": content,
                      "message_id": message_id}
        elif status == "failed":
            event_type = "answer_failed"
            values = {"segment_id": self.current_segment_id,
                      "error_code": error_code or "chat_internal",
                      "persisted": persisted, "message_id": message_id}
            if content is not None:
                values["content"] = content
        else:
            raise ValueError("invalid terminal conversation stream status")
        if content is not None and persisted:
            if len(content) > self._max_draft:
                raise ForgePilotError("conversation_stream_limit", "回答草稿超过安全上限。")
            self.draft = content
        self.status = status
        self._append(event_type, **values)
        self._accepting = False
        self.terminal = True
        return True

    def _snapshot(self) -> ConversationStreamEvent:
        self.metrics.increment("stream_snapshot_count")
        data = {
            "type": "answer_snapshot",
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "revision": self.revision,
            "segment_id": self.current_segment_id,
            "content": self.draft,
            "status": self.status,
            "terminal": self.terminal,
        }
        return ConversationStreamEvent(self.request_id, self.revision,
                                       "answer_snapshot", data)

    def read(self, cursor: int) -> ReplayBatch:
        if cursor < 0:
            raise ForgePilotError("invalid_event_id", "Last-Event-ID 格式无效。")
        if cursor > self.revision:
            raise ForgePilotError("event_cursor_ahead", "事件游标超过当前回答版本。")
        if cursor < self._evicted_through:
            snapshot = self._snapshot()
            return ReplayBatch((snapshot,), self.revision, self.terminal or self._closed)
        events = tuple(event for event in self._events if event.revision > cursor)
        next_cursor = events[-1].revision if events else cursor
        return ReplayBatch(events, next_cursor,
                           (self.terminal or self._closed) and next_cursor >= self.revision)

    def parse_cursor(self, last_event_id: str | None) -> int:
        if last_event_id is None or last_event_id == "":
            return 0
        prefix, separator, revision = last_event_id.partition(":")
        if (separator != ":" or prefix != self.request_id or not revision.isascii()
                or not revision.isdecimal()):
            raise ForgePilotError("invalid_event_id", "Last-Event-ID 格式无效。")
        cursor = int(revision)
        if cursor > self.revision:
            raise ForgePilotError("event_cursor_ahead", "事件游标超过当前回答版本。")
        return cursor

    def subscribe(self, last_event_id: str | None) -> ConversationStreamSubscription:
        cursor = self.parse_cursor(last_event_id)
        if self._subscribers >= self._max_subscribers:
            raise ForgePilotError("conversation_stream_capacity", "回答流订阅者数量已达上限。")
        if last_event_id:
            self.metrics.increment("stream_reconnect_count")
        self._subscribers += 1
        return ConversationStreamSubscription(self, cursor)

    def _unsubscribe(self) -> None:
        self._subscribers = max(0, self._subscribers - 1)

    async def wait(self, cursor: int, timeout: float) -> bool:
        if self.revision != cursor or self.terminal or self._closed:
            return True
        changed = self._changed
        if self.revision != cursor or self.terminal or self._closed:
            return True
        try:
            await asyncio.wait_for(changed.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def shutdown(self) -> None:
        if not self._closed:
            self._closed = True
            self._notify()

    @classmethod
    def released_terminal(cls, conversation_id: str, request_id: str,
                          status: str, *, content: str | None = None,
                          message_id: int | None = None) -> "ConversationStreamBroker":
        broker = cls(conversation_id, request_id)
        broker.status = status
        broker.draft = content or ""
        event_type = {
            "completed": "answer_completed",
            "cancelled": "answer_cancelled",
            "failed": "answer_failed",
            "interrupted": "answer_failed",
        }.get(status, "answer_failed")
        values: dict[str, Any] = {"segment_id": None, "released": True,
                                  "message_id": message_id}
        if content is not None:
            values["content"] = content
        if event_type == "answer_failed":
            values["error_code"] = "request_interrupted" if status == "interrupted" else "chat_internal"
        broker._append(event_type, **values)
        broker._accepting = False
        broker.terminal = True
        return broker
