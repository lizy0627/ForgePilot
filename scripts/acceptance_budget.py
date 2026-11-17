"""Request-level ledger for the product acceptance run, not formal A/B.

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
from time import monotonic
from uuid import uuid4

import httpx

from forgepilot.errors import ForgePilotError
from forgepilot.llm import ToolSpec
from forgepilot.models.accounting import input_reservation
from forgepilot.models.openai import OpenAICompatibleClient


class RequestBudget:
    def __init__(self, path: Path, limit: int = 300_000, *, existing_only: bool = False, enforce_limit: bool = True):
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid acceptance token limit")
        if type(enforce_limit) is not bool:
            raise ValueError("Invalid acceptance budget enforcement mode")
        self.path = Path(path).resolve()
        if existing_only and (Path(path).absolute() != self.path or not self.path.is_file()
                              or self.path.stat().st_nlink != 1):
            raise ValueError("Existing campaign ledger must be a regular file at its original path")
        self.lock = RLock()
        self.limit = limit
        self.enforce_limit = enforce_limit
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
                self._campaign()
                self.db.execute("SELECT id,label,reserved,charged,estimated,status,created_at FROM requests LIMIT 0")
        except BaseException:
            self.db.close()
            raise

    def _campaign(self):
        rows = self.db.execute("SELECT id,cap,held FROM campaign").fetchall()
        if (len(rows) != 1 or rows[0]["id"] != 1 or rows[0]["held"] not in (0, 1)
                or type(rows[0]["cap"]) is not int or rows[0]["cap"] < 1
                or self.enforce_limit and rows[0]["cap"] != self.limit):
            raise ValueError("Missing or inconsistent acceptance campaign or token limit")
        return rows[0]

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
            if self.enforce_limit:
                if self._campaign()["held"]:
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

    def wire_attempts(self, label: str):
        """Content-free evidence for verified HTTP attempts, in reservation order."""
        with self.transaction(read_only=True):
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wire_attempts'").fetchone():
                return []
            rows = self.db.execute("""SELECT r.id,r.charged,r.estimated,w.evidence FROM requests r
                JOIN wire_attempts w ON w.request_id=r.id WHERE r.label=? ORDER BY r.rowid""", (label,))
            return [{**json.loads(row["evidence"]), "request_id": row["id"],
                     "reported_tokens": 0 if row["estimated"] else row["charged"],
                     "estimated_tokens": row["charged"] if row["estimated"] else 0} for row in rows]

    def settle(self, token: str, usage, status: str, *, wire=None):
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
            if wire is not None:
                if wire["usage_issue"]:
                    # No trustworthy complete usage. Do not release the unused
                    # reservation or discard a larger provider-reported count.
                    amount = max(row["reserved"], wire["observed_charge_bound"])
                    estimated = True
                if self.enforce_limit and amount > row["reserved"] and not wire["failure_reason"]:
                    wire["failure_reason"] = "reservation_exceeded"
                self.db.execute("""CREATE TABLE IF NOT EXISTS wire_attempts (
                    request_id TEXT PRIMARY KEY, evidence TEXT NOT NULL)""")
                self.db.execute("INSERT INTO wire_attempts VALUES(?,?)", (token, json.dumps(wire)))
            self.db.execute("UPDATE requests SET charged=?,estimated=?,status=? WHERE id=?",
                            (amount, int(estimated), status, token))
            if not self.enforce_limit:
                # Usage anomalies remain visible to the probe verifier. A confirmed
                # output-limit violation also rejects this response at the transport.
                return wire is None or wire["failure_reason"] != "output_limit_exceeded"
            if amount > row["reserved"] or wire is not None and wire["failure_reason"]:
                self.db.execute("UPDATE campaign SET held=1")
                latest = self._latest_audit()
                if latest:
                    self._record_stop(latest)
                # Commit the actual charge before rejecting every later request.
                return False
        return True

    def _snapshot(self):
        rows = [dict(row) for row in self.db.execute("SELECT * FROM requests ORDER BY created_at,id")]
        campaign = self._campaign()
        snapshot = {"limit": campaign["cap"], "charged_or_reserved": sum(
            row["reserved"] if row["charged"] is None else row["charged"] for row in rows),
            "held": bool(campaign["held"]), "requests": rows}
        if not self.enforce_limit:
            snapshot.update(enforce_limit=False, historical_limit=campaign["cap"], historical_held=snapshot["held"],
                            limit=None, held=False)
        return snapshot

    def snapshot(self):
        with self.transaction(read_only=True):
            return self._snapshot()

    def close(self):
        self.db.close()


def _unique_wire_keys(pairs):
    item = dict(pairs)
    if len(item) != len(pairs):
        raise ValueError("Duplicate provider JSON field")
    return item


def _usage_records(body, stream):
    """Capture all reported usage events, including conflicting finish/tail reports."""
    if not stream:
        item = json.loads(body, object_pairs_hook=_unique_wire_keys)
        if not isinstance(item, dict):
            raise ValueError("Invalid provider response")
        yield item.get("usage")
        return
    data = []
    done = False
    for line in [*bytes(body).splitlines(), b""]:
        if line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
        elif not line and data:
            event = b"\n".join(data)
            data = []
            if done:
                yield False  # Invalid framing; still retain any later numeric usage observations.
            if event.strip() == b"[DONE]":
                done = True
            else:
                try:
                    item = json.loads(event, object_pairs_hook=_unique_wire_keys)
                except (ValueError, RecursionError):
                    yield False
                else:
                    yield item.get("usage") if isinstance(item, dict) else False
    if not done:
        raise ValueError("Incomplete provider stream")


def _wire_usage(body, stream, embedding, successful):
    observations = []
    issue = None
    try:
        for usage in _usage_records(body, stream):
            if usage is None:
                continue
            if not isinstance(usage, dict):
                issue = "invalid_usage"
                continue
            # Keep only recognized counters. Never persist arbitrary provider
            # metadata, response text or malformed string values.
            counters = {k: usage[k] for k in ("total_tokens", "prompt_tokens", "completion_tokens") if k in usage}
            details = usage.get("completion_tokens_details")
            if details is not None:
                if not isinstance(details, dict):
                    issue = "invalid_usage"
                elif "reasoning_tokens" in details:
                    counters["reasoning_tokens"] = details["reasoning_tokens"]
            if any(type(n) is not int or not 0 <= n <= 2**63 - 1 for n in counters.values()):
                issue = "invalid_usage"
            observations.append({k: n for k, n in counters.items() if type(n) is int})
    except (ValueError, RecursionError):
        issue = "invalid_usage"
    reported = observations[0] if observations else {}
    if any(item != reported for item in observations):
        issue = "conflicting_usage"
        reported = {key: value for key, value in reported.items()
                    if all(item.get(key) == value for item in observations)}
    prompt = reported.get("prompt_tokens")
    completion = reported.get("completion_tokens", 0 if embedding else None)
    total = reported.get("total_tokens")
    if (prompt is None or completion is None or total != prompt + completion
            or successful and (not prompt or not embedding and not completion)
            or embedding and completion != 0 or reported.get("reasoning_tokens", 0) > (completion or 0)):
        issue = issue or "invalid_usage"
    # On ambiguity preserve each reported counter and occupy at least the
    # greatest representable charge implied by any observation.
    bound = 0
    for item in observations:
        observed = max(item.get("total_tokens", 0),
                       max(0, item.get("prompt_tokens", 0)) +
                       max(0, item.get("completion_tokens", 0), item.get("reasoning_tokens", 0)))
        bound = max(bound, min(observed, 2**63 - 1))
    return {"prompt_tokens": prompt, "completion_tokens": reported.get("completion_tokens"), "total_tokens": total,
            "observed_usage": observations, "observed_charge_bound": bound, "usage_issue": issue}


class BudgetTransport(httpx.AsyncBaseTransport):
    """Charge every actual HTTP attempt, including gateway recovery and embeddings.

    Buffers bounded acceptance responses to capture usage before the provider can
    retry. No key, prompt, URL, response text or embedding vector enters the ledger.
    """
    def __init__(self, ledger: RequestBudget, label, inner=None, *, total_output_parameter="max_tokens",
                 verify_usage=False, expected_model=None, api_root=None, request_kind=None, output_ceiling=None):
        if total_output_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unverified total-output parameter")
        self.ledger, self.label = ledger, label
        self.total_output_parameter = total_output_parameter
        self.verify_usage = verify_usage or ledger._requires_recovery or not ledger.enforce_limit
        self.expected_model = expected_model
        self.output_ceiling = output_ceiling
        self.provider_default_output = not ledger.enforce_limit and request_kind == "chat" and output_ceiling is None
        self.allowed_urls = ()
        if ledger._requires_recovery or any(x is not None for x in (expected_model, api_root, request_kind, output_ceiling)):
            if (not expected_model or not api_root or request_kind not in ("chat", "embedding")
                    or request_kind == "chat" and not self.provider_default_output
                    and (type(output_ceiling) is not int or output_ceiling < 1)
                    or request_kind == "embedding" and output_ceiling is not None):
                raise ValueError("Incomplete acceptance transport profile")
            root = str(httpx.URL(api_root)).rstrip("/")
            operations = ("/chat/completions",) if request_kind == "chat" else ("/embeddings", "/embeddings/multimodal")
            self.allowed_urls = tuple(httpx.URL(root + operation) for operation in operations)
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        payload = json.loads(request.content, object_pairs_hook=_unique_wire_keys)
        if self.allowed_urls and (request.method != "POST" or request.url not in self.allowed_urls
                                  or payload.get("model") != self.expected_model):
            raise ForgePilotError("llm_budget", "Wire request differs from the configured acceptance model or endpoint")
        if self.verify_usage and (not isinstance(payload.get("model"), str)
                                  or not 0 < len(payload["model"]) <= 256):
            raise ForgePilotError("llm_budget", "Verified acceptance requests require a configured model")
        embedding = request.url.path.endswith(("/embeddings", "/embeddings/multimodal"))
        if embedding:
            bound = len(request.content) + 64
        else:
            maximum = payload.get(self.total_output_parameter)
            if self.provider_default_output:
                if "max_tokens" in payload or "max_completion_tokens" in payload:
                    raise ForgePilotError("llm_config", "Acceptance sample must use the provider's default output length")
            elif (type(maximum) is not int or maximum < 1
                    or self.output_ceiling is not None and maximum > self.output_ceiling
                    or "max_tokens" in payload and "max_completion_tokens" in payload):
                raise ForgePilotError("llm_budget", "Acceptance requests require a prospective output limit")
            tools = [ToolSpec(name=t["function"]["name"], description=t["function"].get("description", ""),
                              input_schema=t["function"]["parameters"]) for t in payload.get("tools", [])]
            bound = max(input_reservation(payload["messages"], tools),
                        len(request.content) + 16 * (len(payload["messages"]) + 1)) + (maximum or 0)
        token = self.ledger.reserve(bound, self.label())
        response = None
        body = bytearray()
        started = monotonic()
        try:
            try:
                response = await self.inner.handle_async_request(request)
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > (32 if embedding else 1) * 1_048_576:
                        raise ForgePilotError("llm_response_limit", "Acceptance response exceeds the provider boundary")
                    body.extend(chunk)
            finally:
                if response is not None:
                    await response.aclose()
            usage = None
            if not self.verify_usage and payload.get("stream"):
                for line in bytes(body).splitlines():
                    if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                        item = json.loads(line[5:])
                        usage = item.get("usage") or usage
            elif not self.verify_usage:
                try:
                    usage = json.loads(body).get("usage")
                except (ValueError, AttributeError):
                    pass  # No usable usage was provided; charge the entire reservation.
        except BaseException as exc:
            if self.verify_usage:
                reason = ("llm_timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                          else "llm_network" if isinstance(exc, httpx.TransportError)
                          else "llm_response_limit" if isinstance(exc, ForgePilotError) and exc.code == "llm_response_limit"
                          else "request_error")
                self._settle_wire(token, payload, embedding, body, response, started, reason,
                                  type(exc).__name__ if isinstance(exc, httpx.TransportError) else None)
                if self.ledger.enforce_limit and isinstance(exc, Exception):
                    raise ForgePilotError("llm_budget", "Verified acceptance request failed; campaign held") from None
            else:
                self.ledger.settle(token, None, "request_error")
            raise
        if self.verify_usage:
            accepted = self._settle_wire(token, payload, embedding, body, response, started)
        else:
            accepted = self.ledger.settle(token, usage, "response_" + str(response.status_code))
        if not accepted:
            if not self.ledger.enforce_limit:
                raise ForgePilotError("llm_response_limit", "Acceptance response exceeded the configured output limit")
            raise ForgePilotError("llm_budget", "Acceptance wire or budget contract failed; campaign held")
        headers = [(key, value) for key, value in response.headers.multi_items()
                   if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}]
        return httpx.Response(response.status_code, headers=headers, content=bytes(body))

    def _settle_wire(self, token, payload, embedding, body, response, started, reason=None, transport_error=None):
        code = response.status_code if response is not None else None
        success = code == 200
        # A rejected streaming request can return a JSON error document (and
        # billed usage) even though the successful response would be SSE.
        stream = bool(payload.get("stream")) and (success or not body.lstrip().startswith(b"{"))
        usage = _wire_usage(body, stream, embedding, success)
        maximum = None if embedding else payload.get(self.total_output_parameter)
        if reason is None and not success:
            reason = f"http_{code}"
        if reason is None and maximum is not None and any(
                max(item.get("completion_tokens", 0), item.get("reasoning_tokens", 0)) > maximum
                for item in usage["observed_usage"]):
            reason = "output_limit_exceeded"
        wire = {**usage, "kind": "embedding" if embedding else "chat", "model": payload.get("model"),
                "output_parameter": self.total_output_parameter if maximum is not None else None, "output_limit": maximum,
                "status_code": code, "failure_reason": reason or usage["usage_issue"],
                "transport_error": transport_error,
                "duration_ms": int((monotonic() - started) * 1000)}
        if self.provider_default_output and usage["usage_issue"]:
            # The input estimate and any observed counters cannot bound the unknown output.
            wire["actual_usage_unknown"] = True
        counters = {key: n for key, n in usage.items() if key in
                    ("total_tokens", "completion_tokens", "prompt_tokens") and n is not None}
        status = "request_error" if code is None else "response_" + str(code)
        return self.ledger.settle(token, counters, status, wire=wire)

    async def aclose(self):
        await self.inner.aclose()
