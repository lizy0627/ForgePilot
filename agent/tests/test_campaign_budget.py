"""Four-arm formal benchmark budget is shared, durable and fail-closed."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.campaign_budget import BudgetPlan, SharedTokenBudget


def test_formal_plan_locks_equal_arms_and_global_limit():
    plan = BudgetPlan.formal()
    assert plan.total_tokens == 1_600_000
    assert plan.cap("micro:single") == plan.cap("micro:multi") == 600_000
    assert plan.cap("real:single") == plan.cap("real:multi") == 200_000
    assert 2 * (plan.micro_per_arm + plan.real_per_arm) == plan.total_tokens
    with pytest.raises(ValueError, match="exceed"):
        BudgetPlan(total_tokens=100, micro_per_arm=31, real_per_arm=20)
    with pytest.raises(ValueError, match="positive"):
        BudgetPlan(total_tokens=True, micro_per_arm=30, real_per_arm=20)
    with pytest.raises(ValueError, match="Unknown"):
        plan.cap("real:other")


def test_capacity_report_discloses_full_dataset_not_guaranteed_at_default_per_run_cap():
    plan = BudgetPlan.formal()
    report = plan.capacity_report()
    assert report["micro:single"] == report["micro:multi"]
    assert report["real:single"] == report["real:multi"]
    assert report["micro:single"]["planned_runs"] == 45
    assert report["real:single"]["planned_runs"] == 15
    assert report["micro:single"]["worst_case_tokens"] == 900_000
    assert report["real:single"]["worst_case_tokens"] == 300_000
    assert all(not item["guaranteed_full_run_capacity"] for item in report.values())
    assert all(item["guaranteed_full_run_capacity"]
               for item in plan.capacity_report(max_tokens_per_run=13_000).values())


def test_reservation_charges_reported_usage_and_releases_unused_capacity(tmp_path):
    path = tmp_path / "budget.sqlite3"
    plan = BudgetPlan(total_tokens=100, micro_per_arm=30, real_per_arm=20)
    with SharedTokenBudget(path, plan) as ledger:
        ledger.begin_run("micro:single", "first", 20)
        ledger.record_usage("first", 3)
        ledger.record_usage("first", 4)
        with pytest.raises(ValueError, match="active"):
            ledger.begin_run("micro:multi", "other", 20)
        ledger.finish_run("first")
        ledger.begin_run("micro:single", "second", 20)
        ledger.record_usage("second", 20)
        ledger.finish_run("second", state="failed")
        with pytest.raises(ValueError, match="cannot cover"):
            ledger.begin_run("micro:single", "third", 4)
        snapshot = ledger.snapshot()
        assert snapshot["spent_tokens"] == 27
        assert snapshot["state"] == "ready"
        assert snapshot["campaigns"][1]["cap_tokens"] == 30
    with SharedTokenBudget(path, plan) as reopened:
        assert reopened.snapshot()["spent_tokens"] == 27
        with pytest.raises(ValueError, match="differs"):
            SharedTokenBudget(path, BudgetPlan(102, 31, 20))


def test_unknown_or_excessive_provider_usage_closes_all_campaigns(tmp_path):
    plan = BudgetPlan(100, 30, 20)
    for usage, reason in ((None, "provider_usage_missing"), (21, "provider_overrun")):
        path = tmp_path / f"{reason}.sqlite3"
        with SharedTokenBudget(path, plan) as ledger:
            ledger.begin_run("real:single", "run", 20)
            with pytest.raises(ValueError, match="closed"):
                ledger.record_usage("run", usage)
            snapshot = ledger.snapshot()
            assert snapshot["state"] == "uncertain"
            assert snapshot["uncertain_reason"] == reason
            assert snapshot["spent_tokens"] == (usage or 0)
            with pytest.raises(ValueError, match="uncertain"):
                ledger.begin_run("real:multi", "later", 20)


def test_request_failure_or_process_exit_never_replenishes_budget(tmp_path):
    path = tmp_path / "budget.sqlite3"
    plan = BudgetPlan(100, 30, 20)
    with SharedTokenBudget(path, plan) as ledger:
        ledger.begin_run("micro:single", "interrupted", 20)
        ledger.record_usage("interrupted", 5)
    with SharedTokenBudget(path, plan) as reopened:
        with pytest.raises(ValueError, match="active"):
            reopened.begin_run("micro:multi", "later", 20)
        reopened.mark_uncertain("interrupted_run", "interrupted")
        assert reopened.snapshot()["spent_tokens"] == 5
        with pytest.raises(ValueError, match="uncertain"):
            reopened.begin_run("real:single", "later", 20)
        with pytest.raises(ValueError, match="uncertain"):
            reopened.finish_run("interrupted")


def test_invalid_usage_and_uncertainty_reason_are_not_silently_accepted(tmp_path):
    path = tmp_path / "budget.sqlite3"
    with SharedTokenBudget(path, BudgetPlan(100, 30, 20)) as ledger:
        ledger.begin_run("micro:single", "one", 20)
        with pytest.raises(ValueError, match="classification"):
            ledger.mark_uncertain("raw exception text that might contain a key", "one")
        assert ledger.snapshot()["state"] == "ready"
        with pytest.raises(ValueError, match="closed"):
            ledger.record_usage("one", True)
        assert ledger.snapshot()["uncertain_reason"] == "provider_usage_missing"


def test_two_process_connections_share_reservations_and_charges(tmp_path):
    path = tmp_path / "budget.sqlite3"
    plan = BudgetPlan(100, 30, 20)
    with SharedTokenBudget(path, plan) as first, SharedTokenBudget(path, plan) as second:
        first.begin_run("micro:single", "single", 20)
        with pytest.raises(ValueError, match="active"):
            second.begin_run("micro:multi", "multi", 20)
        first.record_usage("single", 8)
        first.finish_run("single")
        second.begin_run("micro:multi", "multi", 20)
        second.record_usage("multi", 9)
        second.finish_run("multi")
        assert first.snapshot()["spent_tokens"] == second.snapshot()["spent_tokens"] == 17
