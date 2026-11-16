"""Request-level ledger for the bounded product acceptance run, not formal A/B.

All real clients use BudgetTransport. Pending requests keep their full reservation
after a crash; missing usage and transport errors also charge that bound.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

import httpx

from forgepilot.errors import ForgePilotError
from forgepilot.llm import ToolSpec
from forgepilot.models.accounting import input_reservation
from forgepilot.models.openai import OpenAICompatibleClient


class RequestBudget:
    def __init__(self, path: Path, limit: int = 300_000):
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid acceptance token limit")
        self.lock = RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS campaign (id INTEGER PRIMARY KEY, cap INTEGER, held INTEGER)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, label TEXT, reserved INTEGER, charged INTEGER,
            estimated INTEGER, status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO campaign VALUES(1,?,0)", (limit,))
            if self.db.execute("SELECT cap FROM campaign").fetchone()[0] != limit:
                raise ValueError("Acceptance ledger already has another token limit")
        self.limit = limit

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def reserve(self, amount: int, label: str) -> str:
        if type(amount) is not int or amount < 1:
            raise ValueError("Invalid request reservation")
        with self.transaction():
            if self.db.execute("SELECT held FROM campaign").fetchone()[0]:
                raise ForgePilotError("llm_budget", "Acceptance ledger is held; paid requests are disabled")
            used = self.db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM requests").fetchone()[0]
            if used + amount > self.limit:
                raise ForgePilotError("llm_budget", "Shared acceptance token allowance cannot cover the next request")
            token = uuid4().hex
            self.db.execute("INSERT INTO requests(id,label,reserved,status) VALUES(?,?,?,'pending')",
                            (token, label, amount))
            return token

    def settle(self, token: str, usage, status: str):
        with self.transaction():
            row = self.db.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (token,)).fetchone()
            if row is None:
                raise ValueError("Unknown or already settled request reservation")
            amount, estimated = row["reserved"], True
            try:
                usage = OpenAICompatibleClient._validate_usage(usage) if usage is not None else None
            except ValueError:
                usage = None
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                known = sum(usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens"))
                if type(total) is int and total >= known:
                    amount, estimated = total, False
            self.db.execute("UPDATE requests SET charged=?,estimated=?,status=? WHERE id=?",
                            (amount, int(estimated), status, token))
            if amount > row["reserved"]:
                self.db.execute("UPDATE campaign SET held=1")
                # Commit the actual charge before rejecting every later request.
                return False
        return True

    def snapshot(self):
        with self.lock:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM requests ORDER BY created_at,id")]
            return {"limit": self.limit, "charged_or_reserved": sum(
                row["reserved"] if row["charged"] is None else row["charged"] for row in rows),
                "held": bool(self.db.execute("SELECT held FROM campaign").fetchone()[0]), "requests": rows}

    def close(self):
        self.db.close()


class BudgetTransport(httpx.AsyncBaseTransport):
    """Charge every actual HTTP attempt, including gateway recovery and embeddings.

    Buffers bounded acceptance responses to capture usage before the provider can
    retry. No key, prompt, URL, response text or embedding vector enters the ledger.
    """
    def __init__(self, ledger: RequestBudget, label, inner=None, *, total_output_parameter="max_tokens"):
        if total_output_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unverified total-output parameter")
        self.ledger, self.label = ledger, label
        self.total_output_parameter = total_output_parameter
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        payload = json.loads(request.content)
        embedding = request.url.path.endswith(("/embeddings", "/embeddings/multimodal"))
        if embedding:
            bound = len(request.content) + 64
        else:
            maximum = payload.get(self.total_output_parameter)
            if (type(maximum) is not int or maximum < 1
                    or "max_tokens" in payload and "max_completion_tokens" in payload):
                raise ForgePilotError("llm_budget", "Acceptance requests require a prospective output limit")
            tools = [ToolSpec(name=t["function"]["name"], description=t["function"].get("description", ""),
                              input_schema=t["function"]["parameters"]) for t in payload.get("tools", [])]
            bound = max(input_reservation(payload["messages"], tools),
                        len(request.content) + 16 * (len(payload["messages"]) + 1)) + maximum
        token = self.ledger.reserve(bound, self.label())
        response = None
        try:
            response = await self.inner.handle_async_request(request)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > (32 if embedding else 1) * 1_048_576:
                    raise ForgePilotError("llm_response_limit", "Acceptance response exceeds the provider boundary")
                body.extend(chunk)
            usage = None
            if payload.get("stream"):
                for line in bytes(body).splitlines():
                    if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                        item = json.loads(line[5:])
                        usage = item.get("usage") or usage
            else:
                try:
                    usage = json.loads(body).get("usage")
                except (ValueError, AttributeError):
                    pass  # No usable usage was provided; charge the entire reservation.
        except BaseException:
            self.ledger.settle(token, None, "request_error")
            raise
        finally:
            if response is not None:
                await response.aclose()
        if not self.ledger.settle(token, usage, "response_" + str(response.status_code)):
            raise ForgePilotError("llm_budget", "Provider usage exceeded its reserved bound; campaign held")
        headers = [(key, value) for key, value in response.headers.multi_items()
                   if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}]
        return httpx.Response(response.status_code, headers=headers, content=bytes(body))

    async def aclose(self):
        await self.inner.aclose()
