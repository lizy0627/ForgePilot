"""Recover only from an audited fixture transaction and its immutable evidence."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from threading import Barrier

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_budget import RequestBudget
from acceptance_reconciliation import runtime_identity
from acceptance_recovery import capture_stop, materialize_recovery, open_recovery
from forgepilot.errors import ForgePilotError
from reconciliation_support import make_fixture


def test_simultaneous_reconciliation_and_last_allowance_are_each_spent_once(tmp_path):
    inputs = make_fixture(tmp_path)
    db = tmp_path / "budget.sqlite3"
    handles = [RequestBudget(db, existing_only=True) for _ in range(2)]
    anchors = capture_stop(tmp_path)
    race = Barrier(2)

    def reconcile_once(handle):
        race.wait(timeout=10)
        try:
            return handle.reconcile(*inputs, anchors=anchors)
        except ValueError:
            return None

    def reserve_rest(handle):
        race.wait(timeout=10)
        try:
            return handle.reserve(300_000 - 140, "last-reservation")
        except ForgePilotError as exc:
            assert exc.code == "llm_budget"
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(bool(value) for value in pool.map(reconcile_once, handles)) == 1
            assert len(handles[0].reconciliation_audits()) == 1
            runtime = runtime_identity()
            materialize_recovery(tmp_path, handles[0], runtime)
            for handle in handles:
                open_recovery(tmp_path, handle, runtime)
            assert sum(bool(value) for value in pool.map(reserve_rest, handles)) == 1
        assert handles[0].snapshot()["charged_or_reserved"] == 300_000
    finally:
        for handle in handles:
            handle.close()


def test_committed_reconciliation_survives_failed_publication_and_offline_rebuild(tmp_path, monkeypatch):
    instruction, evidence = make_fixture(tmp_path)
    anchors = capture_stop(tmp_path)
    runtime = runtime_identity()
    db = tmp_path / "budget.sqlite3"
    ledger = RequestBudget(db, existing_only=True)
    record = ledger.reconcile(instruction, evidence, anchors=anchors)
    try:
        def disk_failure(*args, **kwargs):
            raise OSError("simulated disk failure")
        with monkeypatch.context() as patch:
            patch.setattr("acceptance_recovery.os.fsync", disk_failure)
            with pytest.raises(OSError, match="disk failure"):
                materialize_recovery(tmp_path, ledger, runtime)
        with pytest.raises(FileNotFoundError):
            open_recovery(tmp_path, ledger, runtime)
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "after-failed-publication")
    finally:
        ledger.close()
    ledger = RequestBudget(db, existing_only=True)
    try:
        assert ledger.reconciliation_audits() == [record]
        manifest = materialize_recovery(tmp_path, ledger, runtime)
        assert materialize_recovery(tmp_path, ledger, runtime) == manifest
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "published-but-not-verified")
        previous, progress = open_recovery(tmp_path, ledger, runtime)
        assert previous["budget"]["charged_or_reserved"] == 140
        assert previous["recovery_audit_sha256"] == record["audit_sha256"]
        progress.write_text(json.dumps({**previous, "stop_reason": "shared_budget_exhausted"}))
        ledger.stop_recovery()
    finally:
        ledger.close()
    ledger = RequestBudget(db, existing_only=True)
    try:
        with pytest.raises(ValueError):
            open_recovery(tmp_path, ledger, runtime)
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "new-generation-stop")
        assert capture_stop(tmp_path) == anchors
    finally:
        ledger.close()


@pytest.mark.parametrize("change", ["manifest", "artifact", "incomplete_run", "runtime", "progress"])
def test_invalid_recovery_state_stops_before_any_reservation(tmp_path, change):
    args = make_fixture(tmp_path)
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True)
    current = runtime_identity()
    try:
        audit = ledger.reconcile(*args, anchors=capture_stop(tmp_path))
        manifest = materialize_recovery(tmp_path, ledger, current)
        if change == "manifest":
            manifest.write_text("{}")
            with pytest.raises(ValueError):
                materialize_recovery(tmp_path, ledger, current)
            assert manifest.read_bytes() == b"{}"
        elif change == "artifact":
            (tmp_path / "bug-negative-clamp-r1/unrecorded.txt").write_text("new evidence")
        elif change == "runtime":
            current = {**current, "sdk": "different"}
        elif change == "incomplete_run":
            (tmp_path / "bug-inclusive-sum-r1").mkdir()
        else:
            (tmp_path / f"progress-{audit['audit_sha256']}.json").write_text("{}")
        with pytest.raises(ValueError):
            open_recovery(tmp_path, ledger, current)
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "must-be-blocked")
        assert len(ledger.snapshot()["requests"]) == 2
    finally:
        ledger.close()


def test_real_overage_can_be_reported_but_cannot_authorize_recovery(tmp_path):
    args = make_fixture(tmp_path, actual=300_001)
    runtime = runtime_identity()
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True)
    try:
        ledger.reconcile(*args, anchors=capture_stop(tmp_path))
        manifest = json.loads(materialize_recovery(tmp_path, ledger, runtime).read_bytes())
        assert manifest["budget_after_reconciliation"]["held"] is True
        assert manifest["budget_after_reconciliation"]["charged_or_reserved"] == 300_018
        with pytest.raises(ValueError, match="held"):
            open_recovery(tmp_path, ledger, runtime)
        with pytest.raises(ForgePilotError):
            ledger.reserve(1, "after-real-overage")
    finally:
        ledger.close()


def test_valid_progress_resumes_only_with_matching_run_evidence_and_requests(tmp_path):
    args = make_fixture(tmp_path)
    runtime = runtime_identity()
    db = tmp_path / "budget.sqlite3"
    ledger = RequestBudget(db, existing_only=True)
    try:
        ledger.reconcile(*args, anchors=capture_stop(tmp_path))
        materialize_recovery(tmp_path, ledger, runtime)
        state, path = open_recovery(tmp_path, ledger, runtime)
        label = "bug-inclusive-sum-r1"
        ledger.settle(ledger.reserve(80, label), {"total_tokens": 61}, "response_200")
        result = {"run": label, "status": "passed", "network_requests": 1}
        folder = tmp_path / label
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(result))
        state["runs"].append(result)
        state.update(runtime_identity=runtime, budget=ledger.snapshot())
        path.write_text(json.dumps(state))
    finally:
        ledger.close()
    reopened = RequestBudget(db, existing_only=True)
    try:
        restored, restored_path = open_recovery(tmp_path, reopened, runtime)
        assert restored == state and restored_path == path
    finally:
        reopened.close()
    # Even coordinated edits to progress and result cannot change the number
    # of calls attributed to this run in the SQLite ledger.
    result["network_requests"] = 0
    path.write_text(json.dumps(state))
    (folder / "result.json").write_text(json.dumps(result))
    reopened = RequestBudget(db, existing_only=True)
    try:
        with pytest.raises(ValueError):
            open_recovery(tmp_path, reopened, runtime)
        with pytest.raises(ForgePilotError):
            reopened.reserve(1, "unauthorized")
    finally:
        reopened.close()
