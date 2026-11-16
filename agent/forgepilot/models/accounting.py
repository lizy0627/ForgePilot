"""Bounded content-free accounting and shared node budgets."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from time import monotonic

from ..errors import ForgePilotError


def configure_accounting_logging(path, *, max_bytes=5 * 1024 * 1024, backup_count=4):
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    close_accounting_logging()
    handler = RotatingFileHandler(destination, maxBytes=max_bytes, backupCount=backup_count,
                                  encoding="utf-8")
    handler._forgepilot_accounting = True
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("forgepilot.model_accounting")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return destination


def close_accounting_logging():
    logger = logging.getLogger("forgepilot.model_accounting")
    for handler in tuple(logger.handlers):
        if getattr(handler, "_forgepilot_accounting", False):
            logger.removeHandler(handler)
            handler.close()


def response_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def input_reservation(messages, tools) -> int:
    # Conservative UTF-8 byte bound plus message framing, not a vendor tokenizer.
    return len(json.dumps({"messages": messages, "tools": [t.model_dump() for t in tools or []]},
                          ensure_ascii=False).encode("utf-8")) + 16 * (len(messages) + 1)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    call_id: str
    profile_id: str
    model: str
    route: str
    role: str | None
    prompt_version: str | None
    attempt: str
    status: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    usage_estimated: bool
    input_hash: str
    response_hash: str | None
    retry_count: int
    fallback_count: int


class Accounting:
    def __init__(self, *, sink=None, max_records: int = 2048):
        if type(max_records) is not int or not 1 <= max_records <= 100_000:
            raise ValueError("Invalid accounting retention")
        self._records = deque(maxlen=max_records)
        self._lock = Lock()
        self.sink = sink
        self._scoped_sink = ContextVar("forgepilot_model_accounting_sink", default=None)

    @contextmanager
    def scoped_sink(self, sink):
        """Bind a durable task/node ledger without mixing concurrent requests."""
        token = self._scoped_sink.set(sink)
        try:
            yield
        finally:
            self._scoped_sink.reset(token)

    def record(self, record: AttemptRecord) -> None:
        with self._lock:
            self._records.append(record)
        scoped = self._scoped_sink.get()
        if scoped is not None:
            scoped(asdict(record))
        # Only the fixed allowlist above reaches a sink; no provider extras/exception text.
        if self.sink is not None:
            try:
                self.sink(asdict(record))
            except Exception:
                logging.getLogger("forgepilot.model_accounting").error("Model accounting sink failed")
        logging.getLogger("forgepilot.model_accounting").info(
            "%s", json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))

    def snapshot(self) -> tuple[AttemptRecord, ...]:
        with self._lock:
            return tuple(self._records)


class ModelBudget:
    """One trusted Agent/node budget shared across tools, retries and fallbacks.

    Reservations are synchronous under a lock, so concurrent calls cannot each
    spend the same remaining tokens. Missing usage keeps the full reservation.
    """
    def __init__(self, *, max_model_calls=8, max_input_tokens=100_000,
                 max_output_tokens=65_536, max_tool_calls=40, timeout_seconds=900):
        values = (max_model_calls, max_input_tokens, max_output_tokens, max_tool_calls)
        if (any(type(v) is not int or v < 0 for v in values)
                or max_model_calls < 1 or max_input_tokens < 1 or max_output_tokens < 1
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600):
            raise ValueError("Invalid model budget")
        self.max_model_calls, self.max_input_tokens, self.max_output_tokens, self.max_tool_calls = values
        self.model_calls = self.input_tokens = self.output_tokens = self.tool_calls = 0
        self.timeout_seconds = timeout_seconds
        self.deadline = monotonic() + timeout_seconds
        self._lock = Lock()

    @classmethod
    def from_limits(cls, limits):
        return cls(**limits.model_dump())

    def remaining_seconds(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ForgePilotError("llm_budget", "Agent time budget exhausted")
        return remaining

    def reserve(self, input_tokens: int, output_tokens: int) -> tuple[int, int]:
        return self._reserve(input_tokens, output_tokens, cap_output=False)

    def reserve_request(self, input_tokens: int, output_tokens: int) -> tuple[int, int]:
        """Reserve a request whose provider enforces the returned output cap.

        The configured response limit must fit the whole node budget. Later
        requests may use its remaining output allowance, atomically with all
        other reservations on this budget.
        """
        return self._reserve(input_tokens, output_tokens, cap_output=True)

    def _reserve(self, input_tokens: int, output_tokens: int, *, cap_output: bool) -> tuple[int, int]:
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("Invalid budget reservation")
        with self._lock:
            self.remaining_seconds()
            if cap_output:
                if output_tokens > self.max_output_tokens:
                    raise ForgePilotError("llm_budget", "Configured response limit exceeds the node token budget")
                output_tokens = min(output_tokens, self.max_output_tokens - self.output_tokens)
                if output_tokens <= 0:
                    raise ForgePilotError("llm_budget", "Agent output token budget exhausted")
            if (self.model_calls >= self.max_model_calls
                    or self.input_tokens + input_tokens > self.max_input_tokens
                    or self.output_tokens + output_tokens > self.max_output_tokens):
                raise ForgePilotError("llm_budget", "Agent model or token budget exhausted")
            self.model_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            return input_tokens, output_tokens

    def settle(self, reservation, usage) -> bool:
        with self._lock:
            if usage is not None:
                self.input_tokens += usage.get("prompt_tokens", reservation[0]) - reservation[0]
                self.output_tokens += usage.get("completion_tokens", reservation[1]) - reservation[1]
            return (self.input_tokens <= self.max_input_tokens
                    and self.output_tokens <= self.max_output_tokens)

    def consume_tools(self, count: int):
        with self._lock:
            self.remaining_seconds()
            if type(count) is not int or count < 0 or self.tool_calls + count > self.max_tool_calls:
                raise ForgePilotError("llm_budget", "Agent tool budget exhausted")
            self.tool_calls += count
