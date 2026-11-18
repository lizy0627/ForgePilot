"""Fail-closed token accounting shared by the four formal benchmark campaigns.

The ledger does not claim to know the provider's price or whether free credits
apply.  It reserves a whole run's token allowance before the first paid call,
then records the provider's reported usage after each response.  An unknown
charge permanently stops this ledger; only a new, separately disclosed formal
campaign can proceed after the operator reconciles the provider bill.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


CAMPAIGNS = ("micro:single", "micro:multi", "real:single", "real:multi")
UNCERTAIN_REASONS = frozenset({
    "provider_request_failed", "provider_usage_missing", "provider_overrun",
    "interrupted_run", "manual_hold",
})


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BudgetPlan:
    """Precommit equal token ceilings for both arms of each dataset."""

    total_tokens: int
    micro_per_arm: int
    real_per_arm: int

    def __post_init__(self):
        values = (self.total_tokens, self.micro_per_arm, self.real_per_arm)
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("Budget limits must be positive integers")
        if 2 * (self.micro_per_arm + self.real_per_arm) > self.total_tokens:
            raise ValueError("Four equal-arm caps exceed the shared total token budget")

    @classmethod
    def formal(cls):
        """Technical plan's 1.6M ceiling: 90 micro and 30 real runs."""
        return cls(total_tokens=1_600_000, micro_per_arm=600_000, real_per_arm=200_000)

    def cap(self, campaign):
        if campaign not in CAMPAIGNS:
            raise ValueError("Unknown benchmark campaign")
        return self.micro_per_arm if campaign.startswith("micro:") else self.real_per_arm

    def payload(self):
        return {"schema": 1, "total_tokens": self.total_tokens,
                "campaign_caps": {name: self.cap(name) for name in CAMPAIGNS}}

    def capacity_report(self, *, repeats=3, micro_tasks=15, real_tasks=5,
                        max_tokens_per_run=20_000):
        """Expose whether reservations can cover every run at its full limit.

        A false result does not mean the campaign will fail: actual usage can be
        lower. It means the full locked dataset is *not guaranteed* to fit, so
        the evaluator must retain a partial report if the allowance runs out.
        """
        counts = (repeats, micro_tasks, real_tasks, max_tokens_per_run)
        if any(type(value) is not int or value <= 0 for value in counts):
            raise ValueError("Run counts and per-run limit must be positive integers")
        report = {}
        for name in CAMPAIGNS:
            run_count = repeats * (micro_tasks if name.startswith("micro:") else real_tasks)
            cap = self.cap(name)
            report[name] = {"planned_runs": run_count, "campaign_cap_tokens": cap,
                            "worst_case_tokens": run_count * max_tokens_per_run,
                            "max_average_tokens_per_run": cap // run_count,
                            "guaranteed_full_run_capacity": cap >= run_count * max_tokens_per_run}
        return report

    @property
    def sha256(self):
        data = json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(data).hexdigest()


class SharedTokenBudget:
    """SQLite-backed budget. One active run at a time prevents lost reservations.

    Each paid model response must call ``record_usage`` before another request.
    A request exception, missing usage, or an interrupted active run must call
    ``mark_uncertain`` and stop. On process crash the active run remains, so a
    restarted process cannot begin another paid run without operator review.
    """

    def __init__(self, path, plan: BudgetPlan):
        raw_path = Path(path)
        if raw_path.is_symlink():
            raise ValueError("Budget ledger cannot be a symlink")
        self.path = raw_path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.plan = plan
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        try:
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _initialize(self):
        with self._transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS budget_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1), plan_sha256 TEXT NOT NULL,
                total_tokens INTEGER NOT NULL, state TEXT NOT NULL,
                uncertain_reason TEXT, created_at TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS campaigns (
                name TEXT PRIMARY KEY, cap_tokens INTEGER NOT NULL,
                spent_tokens INTEGER NOT NULL DEFAULT 0)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(name),
                reserved_tokens INTEGER NOT NULL, spent_tokens INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT)""")
            row = self.db.execute("SELECT * FROM budget_meta WHERE id=1").fetchone()
            if row is None:
                self.db.execute("""INSERT INTO budget_meta
                    (id,plan_sha256,total_tokens,state,created_at) VALUES (1,?,?,?,?)""",
                                (self.plan.sha256, self.plan.total_tokens, "ready", _now()))
                for name in CAMPAIGNS:
                    self.db.execute("INSERT INTO campaigns(name,cap_tokens) VALUES(?,?)",
                                    (name, self.plan.cap(name)))
            elif row["plan_sha256"] != self.plan.sha256 or row["total_tokens"] != self.plan.total_tokens:
                raise ValueError("Existing benchmark budget plan differs from requested plan")
            stored = {row["name"]: row["cap_tokens"] for row in self.db.execute(
                "SELECT name,cap_tokens FROM campaigns")}
            if stored != {name: self.plan.cap(name) for name in CAMPAIGNS}:
                raise ValueError("Stored campaign caps disagree with the locked plan")

    def _require_ready(self):
        state = self.db.execute("SELECT state FROM budget_meta WHERE id=1").fetchone()[0]
        if state != "ready":
            raise ValueError("Shared benchmark budget is uncertain; no paid request is allowed")

    def begin_run(self, campaign: str, run_id: str, reserve_tokens: int):
        """Reserve the entire run allowance before any model request."""
        if campaign not in CAMPAIGNS or not isinstance(run_id, str) or not run_id:
            raise ValueError("Invalid campaign or run identifier")
        if type(reserve_tokens) is not int or reserve_tokens <= 0:
            raise ValueError("Run reservation must be a positive integer")
        with self._transaction():
            self._require_ready()
            if self.db.execute("SELECT 1 FROM runs WHERE state='active' LIMIT 1").fetchone():
                raise ValueError("An active or interrupted run already holds the shared budget")
            row = self.db.execute("SELECT cap_tokens,spent_tokens FROM campaigns WHERE name=?",
                                  (campaign,)).fetchone()
            if row["spent_tokens"] + reserve_tokens > row["cap_tokens"]:
                raise ValueError("Campaign token allowance cannot cover the next run")
            total_spent = self.db.execute("SELECT SUM(spent_tokens) FROM campaigns").fetchone()[0] or 0
            if total_spent + reserve_tokens > self.plan.total_tokens:
                raise ValueError("Shared token allowance cannot cover the next run")
            self.db.execute("""INSERT INTO runs
                (run_id,campaign,reserved_tokens,state,started_at) VALUES (?,?,?,?,?)""",
                            (run_id, campaign, reserve_tokens, "active", _now()))

    def record_usage(self, run_id: str, total_tokens):
        """Record one provider response. Unknown or excessive usage closes the ledger."""
        error = None
        with self._transaction():
            self._require_ready()
            row = self.db.execute("SELECT * FROM runs WHERE run_id=? AND state='active'",
                                  (run_id,)).fetchone()
            if row is None:
                raise ValueError("No active reservation for this model response")
            if type(total_tokens) is not int or total_tokens < 0:
                self._set_uncertain("provider_usage_missing", run_id)
                error = "Provider token usage is unknown; shared budget closed"
            else:
                new_spent = row["spent_tokens"] + total_tokens
                self.db.execute("UPDATE runs SET spent_tokens=? WHERE run_id=?", (new_spent, run_id))
                self.db.execute("UPDATE campaigns SET spent_tokens=spent_tokens+? WHERE name=?",
                                (total_tokens, row["campaign"]))
                if new_spent > row["reserved_tokens"]:
                    self._set_uncertain("provider_overrun", run_id)
                    error = "Provider usage exceeded the run reservation; shared budget closed"
        if error:
            raise ValueError(error)

    def finish_run(self, run_id: str, *, state="completed"):
        """Release unused reservation. `failed` requires known, fully charged usage."""
        if state not in {"completed", "failed"}:
            raise ValueError("Invalid run completion state")
        with self._transaction():
            self._require_ready()
            row = self.db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["state"] != "active":
                raise ValueError("Run reservation is not active")
            self.db.execute("UPDATE runs SET state=?,finished_at=? WHERE run_id=?",
                            (state, _now(), run_id))

    def _set_uncertain(self, reason, run_id=None):
        self.db.execute("UPDATE budget_meta SET state='uncertain',uncertain_reason=? WHERE id=1",
                        (reason,))
        if run_id is not None:
            self.db.execute("UPDATE runs SET state='uncertain',finished_at=? WHERE run_id=? AND state='active'",
                            (_now(), run_id))

    def mark_uncertain(self, reason: str, run_id: str | None = None):
        """Call on timeout, transport failure, unknown provider charge or interruption."""
        if reason not in UNCERTAIN_REASONS:
            raise ValueError("Unknown uncertainty classification")
        with self._transaction():
            if run_id is not None and not self.db.execute(
                    "SELECT 1 FROM runs WHERE run_id=? AND state='active'", (run_id,)).fetchone():
                raise ValueError("No active run to mark uncertain")
            self._set_uncertain(reason, run_id)

    def snapshot(self):
        meta = dict(self.db.execute("SELECT * FROM budget_meta WHERE id=1").fetchone())
        rows = [dict(row) for row in self.db.execute(
            "SELECT name,cap_tokens,spent_tokens FROM campaigns ORDER BY name")]
        runs = [dict(row) for row in self.db.execute(
            "SELECT run_id,campaign,reserved_tokens,spent_tokens,state,started_at,finished_at FROM runs ORDER BY started_at,run_id")]
        return {"plan_sha256": meta["plan_sha256"], "total_tokens": meta["total_tokens"],
                "state": meta["state"], "uncertain_reason": meta["uncertain_reason"],
                "spent_tokens": sum(row["spent_tokens"] for row in rows),
                "campaigns": rows, "runs": runs}

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
