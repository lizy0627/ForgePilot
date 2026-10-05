"""Bounded, content-free diagnostics for one conversation answer request."""

from __future__ import annotations

import logging
from time import monotonic


_MAX_COUNT = 1_000_000
_MAX_MS = 86_400_000


class StreamMetrics:
    """Collect fixed numeric fields and emit at most one DEBUG record.

    The object deliberately has no API accepting model text, tool arguments,
    transport metadata or exception strings.
    """

    def __init__(self) -> None:
        self._started = monotonic()
        self._values = {
            "llm_stream_ttft_ms": -1,
            "first_safe_delta_ms": -1,
            "llm_stream_duration_ms": 0,
            "provider_chunk_count": 0,
            "conversation_event_count": 0,
            "stream_snapshot_count": 0,
            "stream_reconnect_count": 0,
            "stream_discarded_segments": 0,
            "stream_output_chars": 0,
            "ui_model_updates": 0,
            "terminal_persist_ms": -1,
        }
        self._error_code = "none"
        self._emitted = False

    @staticmethod
    def _milliseconds(start: float) -> int:
        return min(_MAX_MS, max(0, round((monotonic() - start) * 1000)))

    def model_chunk(self, model_started: float) -> None:
        if self._values["llm_stream_ttft_ms"] < 0:
            self._values["llm_stream_ttft_ms"] = self._milliseconds(model_started)
        self.increment("provider_chunk_count")

    def model_finished(self, model_started: float) -> None:
        elapsed = self._milliseconds(model_started)
        self._values["llm_stream_duration_ms"] = min(
            _MAX_MS, self._values["llm_stream_duration_ms"] + elapsed)

    def safe_delta(self, characters: int) -> None:
        if self._values["first_safe_delta_ms"] < 0:
            self._values["first_safe_delta_ms"] = self._milliseconds(self._started)
        self.increment("stream_output_chars", characters)

    def persist_finished(self, started: float) -> None:
        self._values["terminal_persist_ms"] = self._milliseconds(started)

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self._values or type(amount) is not int or amount < 0:
            raise ValueError("invalid stream metric")
        self._values[name] = min(_MAX_COUNT, self._values[name] + amount)

    def set_error(self, code: str) -> None:
        # Callers pass only stable internal codes, never exception text.
        self._error_code = code if code and code.replace("_", "").isalnum() else "stream_internal"

    def snapshot(self) -> dict[str, int | str]:
        return {**self._values, "stream_error_code": self._error_code}

    def emit(self) -> None:
        if self._emitted:
            return
        self._emitted = True
        logger = logging.getLogger("forgepilot.conversation_stream")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("conversation_stream_metrics", extra={"stream_metrics": self.snapshot()})
