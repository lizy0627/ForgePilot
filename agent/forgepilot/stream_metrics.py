"""Content-free performance metrics for one conversation answer request."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from time import monotonic


_MAX_COUNT = 1_000_000
_MAX_MS = 86_400_000
_MAX_LLM_ROUNDS = 8
_LOGGER_NAME = "forgepilot.chat_performance"
_MODE_CODES = {"chat": 1, "task": 2}
_TOOL_MODE_CODES = {"fast": 1, "web": 2, "project": 3, "auto": 4}
_SUMMARY_RESULTS = frozenset({
    "not_queued", "queued", "running", "committed", "stale", "failed",
    "cancelled", "deduplicated", "disabled", "unavailable", "not_needed",
})
_COUNT_FIELDS = frozenset({
    "provider_chunk_count", "conversation_event_count", "stream_snapshot_count",
    "stream_reconnect_count", "stream_discarded_segments", "stream_output_chars",
    "ui_model_updates",
})


def configure_metrics_logging(path: str | Path, *, max_bytes: int = 5 * 1024 * 1024,
                              backup_count: int = 4) -> Path:
    """Write one JSON object per line, retaining the active file plus backups."""
    if type(max_bytes) is not int or type(backup_count) is not int or max_bytes <= 0 or backup_count < 0:
        raise ValueError("invalid metrics retention")
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    for existing in tuple(logger.handlers):
        if getattr(existing, "_forgepilot_metrics_handler", False):
            logger.removeHandler(existing)
            existing.close()
    handler = RotatingFileHandler(destination, maxBytes=max_bytes, backupCount=backup_count,
                                  encoding="utf-8")
    handler._forgepilot_metrics_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    # Also expose the same content-free record through normal warning logging.
    logger.propagate = True
    return destination


def close_metrics_logging() -> None:
    """Flush and close only the dedicated JSONL handlers owned by this module."""
    logger = logging.getLogger(_LOGGER_NAME)
    for existing in tuple(logger.handlers):
        if getattr(existing, "_forgepilot_metrics_handler", False):
            logger.removeHandler(existing)
            existing.close()


class StreamMetrics:
    """Collect a fixed numeric schema and one stable internal error code.

    No method accepts model text, history, tool inputs/outputs, credentials,
    request identifiers, URLs, or exception strings.
    """

    def __init__(self, *, started: float | None = None, mode: str = "chat",
                 project_tools_enabled: bool = False, mcp_enabled: bool = False,
                 request_input_chars: int = 0, tool_mode: str = "auto") -> None:
        if (mode not in _MODE_CODES or tool_mode not in _TOOL_MODE_CODES
                or type(request_input_chars) is not int or request_input_chars < 0):
            raise ValueError("invalid request metric")
        self._started = monotonic() if started is None else started
        self._values: dict[str, int] = {
            "schema_version": 1,
            "request_total_ms": -1,
            "request_to_first_safe_delta_ms": -1,
            "request_prepare_ms": -1,
            "context_prepare_ms": -1,
            "summary_count": 0,
            "summary_total_ms": 0,
            "summary_queued": 0,
            "summary_started": 0,
            "summary_ms": -1,
            "repository_open_ms": -1,
            "repository_cache_hit": -1,
            "llm_round_count": 0,
            "tool_call_count": 0,
            "tool_total_ms": 0,
            "exposed_tool_count": 0,
            "request_input_chars": min(_MAX_COUNT, request_input_chars),
            "stream_output_chars": 0,
            "terminal_persist_ms": -1,
            "request_mode": _MODE_CODES[mode],
            "tool_mode": _TOOL_MODE_CODES[tool_mode],
            "project_tools_enabled": int(project_tools_enabled),
            "mcp_enabled": int(mcp_enabled),
            "provider_chunk_count": 0,
            "conversation_event_count": 0,
            "stream_snapshot_count": 0,
            "stream_reconnect_count": 0,
            "stream_discarded_segments": 0,
            "ui_model_updates": 0,
        }
        self._rounds: list[dict[str, int]] = []
        self._error_code = "none"
        self._summary_result = "not_queued"
        self._emitted = False
        self._lock = Lock()

    @staticmethod
    def _milliseconds(start: float) -> int:
        return min(_MAX_MS, max(0, round((monotonic() - start) * 1000)))

    def request_prepared(self) -> None:
        self._set_once("request_prepare_ms", self._milliseconds(self._started))

    def context_finished(self, started: float) -> None:
        self._set_once("context_prepare_ms", self._milliseconds(started))

    def queue_summary(self) -> None:
        with self._lock:
            self._values["summary_queued"] = 1
            self._summary_result = "queued"

    def start_summary(self) -> None:
        with self._lock:
            self._values["summary_queued"] = 1
            self._values["summary_started"] = 1
            self._summary_result = "running"

    def summary_finished(self, started: float, result: str = "committed") -> None:
        if result not in _SUMMARY_RESULTS or result in {"not_queued", "queued", "running"}:
            raise ValueError("invalid summary result")
        elapsed = self._milliseconds(started)
        with self._lock:
            self._values["summary_queued"] = 1
            self._values["summary_started"] = 1
            self._values["summary_ms"] = elapsed
            self._values["summary_count"] = min(_MAX_COUNT, self._values["summary_count"] + 1)
            self._values["summary_total_ms"] = min(
                _MAX_MS, self._values["summary_total_ms"] + elapsed)
            self._summary_result = result

    def summary_not_started(self, result: str) -> None:
        if result not in _SUMMARY_RESULTS or result in {"not_queued", "queued", "running", "committed"}:
            raise ValueError("invalid summary result")
        with self._lock:
            self._summary_result = result

    def repository_finished(self, started: float, *, cache_hit: bool = False) -> None:
        if type(cache_hit) is not bool:
            raise ValueError("invalid repository cache metric")
        self._set_once("repository_open_ms", self._milliseconds(started))
        self._set_once("repository_cache_hit", int(cache_hit))

    def llm_started(self) -> None:
        with self._lock:
            if len(self._rounds) >= _MAX_LLM_ROUNDS:
                return
            self._rounds.append({
                "round": len(self._rounds) + 1,
                "llm_ttft_ms": -1,
                "llm_duration_ms": -1,
            })
            self._values["llm_round_count"] = len(self._rounds)

    def model_chunk(self, model_started: float) -> None:
        elapsed = self._milliseconds(model_started)
        with self._lock:
            if self._rounds and self._rounds[-1]["llm_ttft_ms"] < 0:
                self._rounds[-1]["llm_ttft_ms"] = elapsed
            self._values["provider_chunk_count"] = min(
                _MAX_COUNT, self._values["provider_chunk_count"] + 1)

    def model_finished(self, model_started: float) -> None:
        elapsed = self._milliseconds(model_started)
        with self._lock:
            if self._rounds:
                self._rounds[-1]["llm_duration_ms"] = elapsed

    def tool_finished(self, started: float) -> None:
        elapsed = self._milliseconds(started)
        with self._lock:
            self._values["tool_call_count"] = min(_MAX_COUNT, self._values["tool_call_count"] + 1)
            self._values["tool_total_ms"] = min(_MAX_MS, self._values["tool_total_ms"] + elapsed)

    def observe_exposed_tools(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("invalid stream metric")
        with self._lock:
            self._values["exposed_tool_count"] = min(
                _MAX_COUNT, max(self._values["exposed_tool_count"], count))

    def safe_delta(self, characters: int) -> None:
        if type(characters) is not int or characters < 0:
            raise ValueError("invalid stream metric")
        with self._lock:
            if self._values["request_to_first_safe_delta_ms"] < 0:
                self._values["request_to_first_safe_delta_ms"] = self._milliseconds(self._started)
            self._values["stream_output_chars"] = min(
                _MAX_COUNT, self._values["stream_output_chars"] + characters)

    def persist_finished(self, started: float) -> None:
        self._set_once("terminal_persist_ms", self._milliseconds(started))

    def request_finished(self) -> None:
        self._set_once("request_total_ms", self._milliseconds(self._started))

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in _COUNT_FIELDS or type(amount) is not int or amount < 0:
            raise ValueError("invalid stream metric")
        with self._lock:
            self._values[name] = min(_MAX_COUNT, self._values[name] + amount)

    def _set_once(self, name: str, value: int) -> None:
        with self._lock:
            if self._values[name] < 0:
                self._values[name] = value

    def set_error(self, code: str) -> None:
        # Callers pass only stable internal codes, never exception text.
        valid = (isinstance(code, str) and code and len(code) <= 64
                 and code.isascii() and code.replace("_", "").isalnum())
        with self._lock:
            self._error_code = code if valid else "chat_internal"

    def snapshot(self) -> dict[str, int | str | list[dict[str, int]]]:
        with self._lock:
            return {**self._values,
                    "llm_rounds": [dict(round_metrics) for round_metrics in self._rounds],
                    "summary_result": self._summary_result,
                    "request_error_code": self._error_code}

    def emit(self) -> None:
        self.request_finished()
        with self._lock:
            if self._emitted:
                return
            self._emitted = True
        values = self.snapshot()
        # The message itself is JSON because uvicorn's formatter does not render
        # arbitrary LogRecord extras.
        logging.getLogger(_LOGGER_NAME).warning(
            json.dumps(values, ensure_ascii=True, separators=(",", ":")),
            extra={"stream_metrics": values})
