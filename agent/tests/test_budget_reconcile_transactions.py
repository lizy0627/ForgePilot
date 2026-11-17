"""Transaction tests use synthetic receipts; input provenance has separate tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_budget import RequestBudget
from forgepilot.errors import ForgePilotError


@pytest.fixture
def held(tmp_path, monkeypatch):
    ledger = RequestBudget(tmp_path / "budget.sqlite3", 100)
    first = ledger.reserve(30, "known")
    ledger.settle(first, {"total_tokens": 20}, "response_200")
    rid = ledger.reserve(35, "unknown")
    ledger.settle(rid, None, "request_error")
    with ledger.transaction():
        ledger.db.execute("UPDATE requests SET charged=80,status='unknown_usage_campaign_hold' WHERE id=?", (rid,))
        ledger.db.execute("UPDATE campaign SET held=1")
    rows = ledger.snapshot()["requests"]
    row = next(r for r in rows if r["id"] == rid)
    anchors = {"campaign": str(tmp_path), "limit": 100, "request_id": rid, "original_requests": rows,
               "held_request": row, "files": {"summary.json": "1"*64, "unknown/result.json": "4"*64}}
    proof = {"campaign": str(tmp_path), "request_id": rid, "expected": {k:v for k,v in row.items() if k!="id"},
             "provider_request_id": "provider-42", "evidence_sha256": "2"*64, "operated_at": "2026-11-17T10:00:00Z",
             "summary_sha256": "1"*64, "result_sha256": "4"*64,
             "runtime_identity": {"transport": "3"*64},
             "usage": {"input_tokens": 5, "reasoning_tokens": 3, "answer_tokens": 2, "total_tokens": 10}}
    monkeypatch.setitem(sys.modules, "acceptance_reconciliation", SimpleNamespace(load_reconciliation=lambda *args: proof))
    try:
        yield ledger, anchors, proof
    finally:
        ledger.close()


def reconcile(ledger, anchors):
    return ledger.reconcile(Path("fixture-manifest"), Path("fixture-evidence"), anchors=anchors)


def test_audit_is_durable_once_and_publication_required_on_every_open(held, tmp_path):
    ledger, anchors, _ = held
    audit = reconcile(ledger, anchors)
    assert audit["before"] == anchors["held_request"]
    assert audit["charged_or_reserved_after"] == 30 and not audit["held_after"]
    with pytest.raises(ValueError):
        reconcile(ledger, anchors)
    assert ledger.reconciliation_audits() == [audit]
    reloaded = RequestBudget(tmp_path / "budget.sqlite3", 100, existing_only=True)
    try:
        for handle in (reloaded, ledger):
            with pytest.raises(ForgePilotError):
                handle.reserve(1, "before-recovery-summary")
        state = reloaded.snapshot()
        with pytest.raises(ValueError):
            reloaded._authorize_verified_recovery("0"*64, state)
        with pytest.raises(ValueError):
            reloaded._authorize_verified_recovery(audit["audit_sha256"], {**state, "requests": []})
        # Exercise the private CAS boundary; the public recovery entry verifies the manifest first.
        reloaded._authorize_verified_recovery(audit["audit_sha256"], state)
        reloaded.reserve(70, "after-publication")
        with pytest.raises(ForgePilotError):
            reloaded.reserve(1, "over")
        assert reloaded.snapshot()["charged_or_reserved"] == 100
    finally:
        reloaded.close()


def test_overrun_records_untruncated_usage_and_preserves_hold(held):
    ledger, anchors, proof = held
    proof["usage"].update(input_tokens=135, total_tokens=140)
    audit = reconcile(ledger, anchors)
    assert audit["after"]["charged"] == 140 and audit["after"]["estimated"] == 0
    assert ledger.snapshot()["charged_or_reserved"] == 160
    assert ledger.snapshot()["held"] and audit["held_after"]
    with pytest.raises(ValueError):
        ledger._authorize_verified_recovery(audit["audit_sha256"], ledger.snapshot())


def test_crash_between_audit_and_charge_rolls_back_every_write(held):
    ledger, anchors, _ = held
    before = ledger.snapshot()
    ledger.db.execute("""CREATE TRIGGER simulate_crash BEFORE UPDATE OF charged ON requests
        BEGIN SELECT RAISE(ABORT,'crash injection'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="crash injection"):
        reconcile(ledger, anchors)
    assert ledger.reconciliation_audits() == []
    assert ledger.snapshot() == before


def test_concurrent_connections_cannot_reserve_against_unpublished_credit(held, tmp_path):
    ledger, anchors, _ = held
    other = RequestBudget(tmp_path / "budget.sqlite3", 100, existing_only=True)
    barrier = Barrier(2)
    def adjudicate():
        barrier.wait(timeout=10)
        return reconcile(ledger, anchors)
    def charge():
        barrier.wait(timeout=10)
        with pytest.raises(ForgePilotError):
            other.reserve(70, "racing")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(adjudicate), pool.submit(charge)
            audit = a.result()
            b.result()
        assert other.snapshot()["charged_or_reserved"] == 30
        for handle in (ledger, other):
            handle._authorize_verified_recovery(audit["audit_sha256"], handle.snapshot())
        ledger.stop_recovery()
        # Deliberately corrupt only the disposable fixture's flag. Revocation survives.
        ledger.db.execute("UPDATE campaign SET held=0")
        with pytest.raises(ForgePilotError):
            other.reserve(70, "stale recovery")
        with pytest.raises(ValueError):
            other._authorize_verified_recovery(audit["audit_sha256"], other.snapshot())
    finally:
        other.close()


def test_unrelated_request_drift_prevents_reconciliation(held):
    ledger, anchors, _ = held
    ledger.db.execute("UPDATE requests SET charged=19 WHERE label='known'")
    before = ledger.snapshot()
    with pytest.raises(ValueError):
        reconcile(ledger, anchors)
    assert ledger.snapshot() == before and ledger.reconciliation_audits() == []


def test_changed_cap_after_open_does_not_release_credit(held):
    ledger, anchors, _ = held
    ledger.db.execute("UPDATE campaign SET cap=99")
    with pytest.raises(ValueError, match="token limit"):
        reconcile(ledger, anchors)
    assert ledger.reconciliation_audits() == []
    assert tuple(ledger.db.execute("SELECT cap,held FROM campaign").fetchone()) == (99, 1)


def test_existing_only_cannot_create_missing_ledger_or_repopulate_empty_campaign(tmp_path):
    path = tmp_path / "budget.sqlite3"
    with pytest.raises((sqlite3.Error, ValueError)):
        RequestBudget(path, existing_only=True)
    assert not path.exists()
    ledger = RequestBudget(path)
    other = RequestBudget(path, existing_only=True)
    try:
        with pytest.raises(ForgePilotError):
            other.reserve(1, "no recovery audit")
    finally:
        other.close()
    ledger.db.execute("DELETE FROM campaign")
    ledger.close()
    with pytest.raises(ValueError):
        RequestBudget(path, existing_only=True)
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT * FROM campaign").fetchall()
