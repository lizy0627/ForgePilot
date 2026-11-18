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


def test_configured_preflight_replaces_only_verified_reasoning_limit_stop(tmp_path):
    make_fixture(tmp_path)
    summary = tmp_path / "summary.json"
    old = json.loads(summary.read_bytes())
    model = "doubao-seed-2-1-lite-fixture"
    old["models"] = [{"profile": "profile", "model": model, "family": "doubao"}]
    summary.write_text(json.dumps(old))
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True, enforce_limit=False)

    def finish_probe(state, path, *, limit, truncated):
        label = state["revalidation_label"]
        checks = []
        for name in (["connection:profile", "chat"] if truncated else ["connection:profile", "chat", "summary"]):
            failed = truncated and name == "chat"
            completion = limit if failed else 37
            usage = {"prompt_tokens": 51, "completion_tokens": completion, "total_tokens": 51 + completion}
            attempt = {**usage, "kind": "chat", "model": model, "output_parameter": "max_completion_tokens",
                       "output_limit": limit, "status_code": 200, "failure_reason": None, "usage_issue": None,
                       "observed_usage": [{**usage, "reasoning_tokens": completion if failed else 36}]}
            rid = ledger.reserve(100 + limit, label)
            ledger.settle(rid, usage, "response_200", wire=attempt)
            checks.append({"name": name, "kind": "chat", "status": "failed" if failed else "passed",
                           "profile": "profile", "model": model, "output_limit": limit, "request_id": rid})
        wire = ledger.wire_attempts(label)
        reason = "llm_incomplete" if truncated else None
        result = {"run": label, "status": "failed" if truncated else "passed",
                  "identity": {key: state[key] for key in ("runtime_identity", "continuation_id", "protocol_sha256",
                                                          "model_configuration_sha256", "launcher_revision")},
                  "wire_attempts": wire, "network_requests": len(wire), "estimated_tokens": 0, "checks": checks,
                  "reported_tokens": sum(a["reported_tokens"] for a in wire), "duration_seconds": 1,
                  "first_failure_reason": reason, "final_stop_reason": reason, "embedding_enabled": False}
        (tmp_path / label).mkdir()
        (tmp_path / label / "result.json").write_text(json.dumps(result))
        state.update(revalidation=result, budget=ledger.snapshot())
        if truncated:
            state["stop_reason"] = reason
        path.write_text(json.dumps(state))

    try:
        runtime = runtime_identity()
        first, first_path = open_continuation(tmp_path, ledger, runtime)
        finish_probe(first, first_path, limit=1024, truncated=True)
        first_bytes = first_path.read_bytes()
        new_runtime = {**runtime, "configured": "new implementation"}
        second, second_path = open_continuation(tmp_path, ledger, new_runtime)
        assert second_path.name == "progress-continuation-configured.json"
        assert second["revalidation_label"] == "revalidation-configured"
        assert "revalidation" not in second and "stop_reason" not in second
        assert second["runs"] == first["runs"] and second["budget"] == first["budget"]
        finish_probe(second, second_path, limit=16000, truncated=False)
        restored, restored_path = open_continuation(tmp_path, ledger, new_runtime,
                                                    configured_limits=lambda: {"profile": 16000})
        assert restored == second and restored_path == second_path and first_path.read_bytes() == first_bytes
        with pytest.raises(ValueError):
            open_continuation(tmp_path, ledger, new_runtime, configured_limits={"profile": 10000})
        with first_path.open("ab") as file:
            file.write(b" ")
        with pytest.raises(ValueError, match="identity"):
            open_continuation(tmp_path, ledger, new_runtime, configured_limits={"profile": 16000})
    finally:
        ledger.close()
