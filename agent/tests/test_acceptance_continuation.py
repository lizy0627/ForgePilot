"""Offline continuation of the original samples with their historical accounting."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from acceptance_budget import RequestBudget
from acceptance_reconciliation import runtime_identity
from acceptance_recovery import capture_stop, materialize_recovery, open_continuation, open_recovery
from reconciliation_support import make_fixture


def test_held_campaign_resumes_without_audit_and_verifies_unchanged_history(tmp_path):
    make_fixture(tmp_path)
    runtime = runtime_identity()
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True, enforce_limit=False)
    try:
        state, path = open_continuation(tmp_path, ledger, runtime)
        assert path.name == "progress-continuation.json"
        assert "stop_reason" not in state
        assert [r["run"] for r in state["runs"]] == [state["failed_slot"]]
        assert state["budget"]["historical_held"] and state["budget"]["charged_or_reserved"] == 300_000
        assert ledger.reconciliation_audits() == []
        path.write_text(json.dumps(state))
        assert open_continuation(tmp_path, ledger, runtime) == (state, path)
        with pytest.raises(ValueError, match="identity"):
            open_continuation(tmp_path, ledger, {**runtime, "changed": "code"})
        with (tmp_path / state["failed_slot"] / "result.json").open("ab") as f:
            f.write(b" ")
        with pytest.raises(ValueError, match="identity"):
            open_continuation(tmp_path, ledger, runtime)
    finally:
        ledger.close()


def test_uncheckpointed_dispatch_and_partial_directory_cannot_be_replayed(tmp_path):
    make_fixture(tmp_path)
    runtime = runtime_identity()
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True, enforce_limit=False)
    try:
        directory = tmp_path / "bug-inclusive-sum-r1"
        directory.mkdir()
        with pytest.raises(ValueError, match="incomplete run"):
            open_continuation(tmp_path, ledger, runtime)
        directory.rmdir()
        ledger.reserve(20, "bug-inclusive-sum-r1")
        with pytest.raises(ValueError, match="missing from recorded"):
            open_continuation(tmp_path, ledger, runtime)
    finally:
        ledger.close()


def test_previously_audited_progress_is_imported_with_its_own_runtime(tmp_path):
    inputs = make_fixture(tmp_path)
    old_runtime = runtime_identity()
    db = tmp_path / "budget.sqlite3"
    historical = RequestBudget(db, existing_only=True)
    try:
        historical.reconcile(*inputs, anchors=capture_stop(tmp_path))
        materialize_recovery(tmp_path, historical, old_runtime)
        state, path = open_recovery(tmp_path, historical, old_runtime)
        identity = {key: state[key] for key in (
            "protocol_sha256", "model_configuration_sha256", "launcher_revision", "recovery_audit_sha256")}
        identity["runtime_identity"] = old_runtime
        result = {"run": "revalidation", "status": "failed", "identity": identity,
                  "first_failure_reason": "credential_missing", "final_stop_reason": "credential_missing",
                  "network_requests": 0, "wire_attempts": [], "reported_tokens": 0, "estimated_tokens": 0}
        (tmp_path / "revalidation").mkdir()
        (tmp_path / "revalidation/result.json").write_text(json.dumps(result))
        path.write_text(json.dumps({**state, **identity, "stop_reason": "credential_missing", "revalidation": result}))
    finally:
        historical.close()
    active = RequestBudget(db, existing_only=True, enforce_limit=False)
    try:
        new_runtime = {**old_runtime, "new": "identity"}
        state, new_path = open_continuation(tmp_path, active, new_runtime)
        assert state["revalidation"] == result and state["stop_reason"] == "credential_missing"
        assert state["runtime_identity"] == new_runtime
        assert state["budget"]["charged_or_reserved"] == 140
        assert new_path != path
        new_path.write_text(json.dumps(state))
        assert open_continuation(tmp_path, active, new_runtime) == (state, new_path)
        state.pop("stop_reason")
        new_path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="Recorded stop"):
            open_continuation(tmp_path, active, new_runtime)
    finally:
        active.close()
