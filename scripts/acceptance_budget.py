"""Request-level ledger for the bounded product acceptance run, not formal A/B.

All real clients use BudgetTransport. Pending requests keep their full reservation
after a crash; missing usage and transport errors also charge that bound.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
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
    def __init__(self, path: Path, limit: int = 300_000, *, existing_only: bool = False):
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid acceptance token limit")
        self.path = Path(path).resolve()
        if existing_only and (Path(path).absolute() != self.path or not self.path.is_file()
                              or self.path.stat().st_nlink != 1):
            raise ValueError("Existing campaign ledger must be a regular file at its original path")
        self.lock = RLock()
        self.limit = limit
        self._requires_recovery = existing_only
        self._authorized_audit = None
        target = self.path.as_uri() + "?mode=rw" if existing_only else self.path
        self.db = sqlite3.connect(target, uri=existing_only, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            if not existing_only:
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                if not existing_only:
                    self.db.execute("CREATE TABLE IF NOT EXISTS campaign (id INTEGER PRIMARY KEY, cap INTEGER, held INTEGER)")
                    self.db.execute("""CREATE TABLE IF NOT EXISTS requests (
                        id TEXT PRIMARY KEY, label TEXT, reserved INTEGER, charged INTEGER,
                        estimated INTEGER, status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
                    self.db.execute("INSERT OR IGNORE INTO campaign VALUES(1,?,0)", (limit,))
                self._held()
                self.db.execute("SELECT id,label,reserved,charged,estimated,status,created_at FROM requests LIMIT 0")
        except BaseException:
            self.db.close()
            raise

    def _held(self):
        rows = self.db.execute("SELECT id,cap,held FROM campaign").fetchall()
        if len(rows) != 1 or tuple(rows[0]) not in ((1, self.limit, 0), (1, self.limit, 1)):
            raise ValueError("Missing or inconsistent acceptance campaign or token limit")
        return bool(rows[0]["held"])

    @contextmanager
    def transaction(self, *, read_only=False):
        with self.lock:
            self.db.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def _has_audit_table(self):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reconciliations'"
        ).fetchone() is not None

    def _latest_audit(self):
        if self._has_audit_table():
            row = self.db.execute("SELECT audit_sha256 FROM reconciliations ORDER BY rowid DESC LIMIT 1").fetchone()
            if row:
                return row["audit_sha256"]
        return None

    def reconciliation_audits(self):
        with self.transaction(read_only=True):
            if not self._has_audit_table():
                return []
            audits = []
            for row in self.db.execute("SELECT * FROM reconciliations ORDER BY rowid"):
                body = row["audit_json"]
                digest = hashlib.sha256(body.encode()).hexdigest()
                record = json.loads(body)
                if (digest != row["audit_sha256"]
                        or any(record[key] != row[key] for key in ("request_id", "provider_request_id", "evidence_sha256"))):
                    raise ValueError("Acceptance reconciliation audit identity mismatch")
                audits.append({**record, "audit_sha256": digest})
            return audits

    def reconcile(self, manifest_path: Path, evidence_path: Path, *, anchors: dict):
        from acceptance_reconciliation import load_reconciliation

        proof = load_reconciliation(manifest_path, evidence_path, self.path.parent)
        with self.transaction():
            snapshot = self._snapshot()
            rid = proof["request_id"]
            if (not snapshot["held"] or proof["campaign"] != str(self.path.parent)
                    or anchors["campaign"] != proof["campaign"] or anchors["request_id"] != rid
                    or anchors["limit"] != self.limit or not anchors["files"]):
                raise ValueError("Reconciliation requires the original held campaign and request")
            rows = snapshot["requests"]
            if sorted(rows, key=lambda r: r["id"]) != sorted(anchors["original_requests"], key=lambda r: r["id"]):
                raise ValueError("Ledger requests differ from the original frozen campaign")
            matches = [r for r in rows if r["label"] == proof["expected"]["label"]]
            if len(matches) != 1:
                raise ValueError("Request label does not identify one frozen request")
            before = matches[0]
            if (anchors["files"].get("summary.json") != proof["summary_sha256"]
                    or anchors["files"].get(before["label"] + "/result.json") != proof["result_sha256"]):
                raise ValueError("Provider attribution and frozen evidence use different original records")
            if (before["id"] != rid or before != {"id": rid, **proof["expected"]}
                    or before != anchors["held_request"] or before["status"] != "unknown_usage_campaign_hold"
                    or before["estimated"] != 1 or type(before["charged"]) is not int):
                raise ValueError("Frozen request differs from its expected state")
            amount = proof["usage"]["total_tokens"]
            after = dict(before, charged=amount, estimated=0, status="reconciled_usage")
            total = snapshot["charged_or_reserved"] - before["charged"] + amount
            # The obsolete estimate for this request is replaced by its actual
            # usage. Preserve independent holds and confirmed provider overruns.
            other_hold = any(row["id"] != rid and (
                row["status"] == "unknown_usage_campaign_hold"
                or row["estimated"] == 0 and row["charged"] > row["reserved"]
            ) for row in rows)
            held = total >= self.limit or other_hold
            recorded_at = self.db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            audit = {**proof, "recorded_at": recorded_at, "anchors": anchors, "before": before, "after": after,
                     "charged_or_reserved_before": snapshot["charged_or_reserved"],
                     "charged_or_reserved_after": total, "held_after": held}
            body = json.dumps(audit, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(body.encode()).hexdigest()
            self.db.execute("""CREATE TABLE IF NOT EXISTS reconciliations (
                request_id TEXT PRIMARY KEY, provider_request_id TEXT UNIQUE NOT NULL,
                evidence_sha256 TEXT UNIQUE NOT NULL, audit_sha256 TEXT UNIQUE NOT NULL, audit_json TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS recovery_stops (
                audit_sha256 TEXT PRIMARY KEY, stopped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            self.db.execute("INSERT INTO reconciliations VALUES(?,?,?,?,?)",
                            (rid, proof["provider_request_id"], proof["evidence_sha256"], digest, body))
            self.db.execute("UPDATE requests SET charged=?,estimated=0,status=? WHERE id=?",
                            (amount, after["status"], rid))
            self.db.execute("UPDATE campaign SET held=? WHERE id=1", (int(held),))
            return {**audit, "audit_sha256": digest}

    def _recovery_stopped(self, digest):
        return self.db.execute("SELECT 1 FROM recovery_stops WHERE audit_sha256=?", (digest,)).fetchone() is not None

    def _authorize_verified_recovery(self, audit_sha256: str, expected_snapshot: dict):
        """Bind this connection after verification of the persisted derived state."""
        with self.transaction():
            self._authorized_audit = None
            if (not audit_sha256 or self._latest_audit() != audit_sha256
                    or self._recovery_stopped(audit_sha256) or expected_snapshot["held"]
                    or self._snapshot() != expected_snapshot):
                raise ValueError("Recovery authorization needs the latest available reconciliation")
        self._authorized_audit = audit_sha256

    def _record_stop(self, digest):
        self.db.execute("INSERT OR IGNORE INTO recovery_stops(audit_sha256) VALUES(?)", (digest,))

    def stop_recovery(self):
        with self.transaction():
            if not self._authorized_audit or self._latest_audit() != self._authorized_audit:
                raise ValueError("Recovery stop requires the current authorized reconciliation")
            self._record_stop(self._authorized_audit)
            self.db.execute("UPDATE campaign SET held=1 WHERE id=1")
            self._authorized_audit = None

    def reserve(self, amount: int, label: str) -> str:
        if type(amount) is not int or amount < 1:
            raise ValueError("Invalid request reservation")
        with self.transaction():
            if self._held():
                raise ForgePilotError("llm_budget", "Acceptance ledger is held; paid requests are disabled")
            latest = self._latest_audit()
            if ((self._requires_recovery and not latest) or latest != self._authorized_audit
                    or (latest and self._recovery_stopped(latest))):
                raise ForgePilotError("llm_budget", "Acceptance recovery summary has not authorized this connection")
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
                latest = self._latest_audit()
                if latest:
                    self._record_stop(latest)
                # Commit the actual charge before rejecting every later request.
                return False
        return True

    def _snapshot(self):
        rows = [dict(row) for row in self.db.execute("SELECT * FROM requests ORDER BY created_at,id")]
        return {"limit": self.limit, "charged_or_reserved": sum(
            row["reserved"] if row["charged"] is None else row["charged"] for row in rows),
            "held": self._held(), "requests": rows}

    def snapshot(self):
        with self.transaction(read_only=True):
            return self._snapshot()

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
