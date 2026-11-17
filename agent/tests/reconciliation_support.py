"""Synthetic provider request detail and independent local evidence for budget tests."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def write_reconciliation_fixture(campaign, row, *, actual=123):
    from acceptance_reconciliation import runtime_identity
    from forgepilot.models.accounting import response_hash

    campaign = Path(campaign)
    model = "doubao-seed-2-1-lite-fixture"
    request = {"model": model, "messages": [{"role": "user", "content": "fixture only"}]}
    input_hash = response_hash({"messages": request["messages"], "tools": []})
    run = {
        "run": row["label"], "status": "failed", "network_requests": 1,
        "accounting": [{"model": model, "attempt": "primary", "status": "network",
                        "call_id": "c" * 32, "input_hash": input_hash},
                       {"model": model, "attempt": "retry", "status": "budget",
                        "call_id": "c" * 32, "input_hash": input_hash}],
    }
    (campaign / row["label"]).mkdir(parents=True)
    (campaign / row["label"] / "result.json").write_text(json.dumps(run), encoding="utf-8")
    (campaign / "summary.json").write_text(json.dumps({
        "runs": [run], "stop_reason": "shared_budget_exhausted",
        "budget": {"requests": [{**row, "charged": row["reserved"], "status": "request_error"}], "held": True}
    }), encoding="utf-8")
    evidence = {
        "kind": "provider_request_usage_v1", "provider": "ark.cn-beijing.volces.com",
        "provider_request_id": "provider-fixture-record-123",
        "started_at": row["created_at"].replace(" ", "T") + "Z", "request": request,
        "usage": {"input_tokens": actual // 3, "reasoning_tokens": actual // 3,
                  "answer_tokens": actual - 2 * (actual // 3), "total_tokens": actual},
    }
    evidence_file = campaign / "fixture-provider.json"
    evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
    manifest = {"campaign": str(campaign.resolve()), "request_id": row["id"],
                "expected": {k: v for k, v in row.items() if k != "id"},
                "operated_at": datetime.now(timezone.utc).isoformat(),
                "evidence_sha256": hashlib.sha256(evidence_file.read_bytes()).hexdigest(),
                "runtime_identity": runtime_identity()}
    manifest_file = campaign / "fixture-instruction.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_file, evidence_file


def make_fixture(campaign, actual=123):
    from acceptance_budget import RequestBudget
    from run_product_acceptance import plan

    campaign.mkdir(exist_ok=True)
    frozen_plan = plan()
    (campaign / "plan.json").write_text(json.dumps(frozen_plan))
    (campaign / "preflight.json").write_text(json.dumps({
        "connections": [{"profile": "synthetic", "ok": True}], "chat": "passed", "summary": "passed"
    }))
    ledger = RequestBudget(campaign / "budget.sqlite3")
    try:
        known = ledger.reserve(25, "preflight")
        ledger.settle(known, {"total_tokens": 17}, "response_200")
        token = ledger.reserve(300, "bug-negative-clamp-r1")
        ledger.settle(token, None, "request_error")
        with ledger.transaction():
            ledger.db.execute("UPDATE requests SET created_at=datetime(created_at,'-1 minute') WHERE id=?", (known,))
            ledger.db.execute("UPDATE campaign SET held=1")
        original_budget = ledger.snapshot()
        original_row = next(r for r in original_budget["requests"] if r["id"] == token)
        with ledger.transaction():
            ledger.db.execute("UPDATE requests SET status='unknown_usage_campaign_hold',charged=? WHERE id=?",
                              (ledger.limit - 17, token))
        row = next(r for r in ledger.snapshot()["requests"] if r["id"] == token)
        paths = write_reconciliation_fixture(campaign, row, actual=actual)
        summary = campaign / "summary.json"
        state = json.loads(summary.read_bytes())
        state.update(protocol_sha256=hashlib.sha256(json.dumps(frozen_plan, sort_keys=True).encode()).hexdigest(),
                     planned_runs=12, budget=original_budget, model_configuration_sha256="1" * 64,
                     launcher_revision="2" * 64, models=[])
        summary.write_text(json.dumps(state))
        (campaign / "budget-hold-audit.json").write_text(json.dumps({
            "original_request": original_row, "conservative_final_charge": ledger.limit - 17,
            "known_reported_tokens": 17, "no_more_paid_requests": True, "fixed_denominator": 12,
            "reason": "timeout_without_usage_and_original_max_tokens_did_not_bound_reasoning_output"
        }))
        return paths
    finally:
        ledger.close()
