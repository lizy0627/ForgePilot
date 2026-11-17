"""Post-repair checks must use the original ledger without replaying a paid slot."""
import json

import pytest

from test_acceptance_resume_entry import (
    acceptance, command, fixture_campaign as fixture_campaign, fixture_inputs, offline_configuration,
    single_sample_call,
)
from reconciliation_support import mock_revalidation_network


def test_old_successful_preflight_cannot_authorize_a_sample(campaign, monkeypatch):
    fixture_inputs(campaign)
    original = (campaign / "preflight.json").read_bytes()
    assert json.loads(original)["chat"] == "passed"
    offline_configuration(monkeypatch, campaign)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda path: "fixture")

    async def stop_at_new_preflight(*args, **kwargs):
        raise RuntimeError("new wire preflight reached")

    async def premature_sample(*args, **kwargs):
        pytest.fail("old preflight allowed sample entry")

    monkeypatch.setattr(acceptance, "run_revalidation", stop_at_new_preflight, raising=False)
    monkeypatch.setattr(acceptance, "run_sample", premature_sample)
    with pytest.raises(RuntimeError, match="new wire preflight reached"):
        command(monkeypatch, campaign, "--execute", "--resume")
    assert (campaign / "preflight.json").read_bytes() == original


@pytest.mark.parametrize("retest_status", ["passed", "failed"])
def test_single_retest_resumes_separately_from_twelve_slots(campaign, monkeypatch, retest_status):
    original_slot = "bug-negative-clamp-r1"
    args = fixture_inputs(campaign)
    (campaign / original_slot / "graph.json").write_text('{"original":"graph"}')
    protected = [campaign / name for name in ("summary.json", "preflight.json", "plan.json", "budget-hold-audit.json")]
    protected.extend((campaign / original_slot).iterdir())
    original_bytes = {path: path.read_bytes() for path in protected}
    command(monkeypatch, campaign, "--reconcile", *args)
    offline_configuration(monkeypatch, campaign)
    sent = mock_revalidation_network(monkeypatch)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda _: "fixture")
    visits = []

    async def sample(task, repeat, config, key, ledger, output, *, identity, retest_of=None):
        slot = task["id"] + "-r" + str(repeat)
        label = ("retest-" if retest_of else "") + slot
        visits.append(label)
        (output / label).mkdir()
        usage = await single_sample_call(config, key, ledger, label)
        result = {"run": label, "status": retest_status if retest_of else "passed", "identity": identity,
                  **usage,
                  "final_stop_reason": "validation_failed" if retest_of and retest_status == "failed" else None}
        if retest_of:
            assert retest_of == slot == original_slot
            result["retest_of"] = retest_of
        acceptance.save(output / label / "result.json", result)
        return result

    monkeypatch.setattr(acceptance, "run_sample", sample)
    paid = ("--execute", "--resume")
    with pytest.raises(ValueError, match="original failed slot"):
        command(monkeypatch, campaign, *paid, "--retest", "feature-range-clamp-r3")
    assert visits == sent == []
    command(monkeypatch, campaign, *paid, "--retest", original_slot)
    command(monkeypatch, campaign, *paid, "--retest", original_slot)
    command(monkeypatch, campaign, *paid, "--revalidate-only")
    progress = next(campaign.glob("progress-*.json"))
    partial = json.loads(progress.read_bytes())
    assert len(sent) == 5 and visits == ["retest-" + original_slot]
    assert partial["attempted_runs"] == 1 and partial["passed_runs"] == 0
    assert partial["runs"] == json.loads(original_bytes[campaign / "summary.json"])["runs"]
    assert partial["retest"]["status"] == retest_status
    command(monkeypatch, campaign, *paid)
    command(monkeypatch, campaign, *paid)
    complete = json.loads(progress.read_bytes())
    assert complete["planned_runs"] == complete["attempted_runs"] == len(complete["runs"]) == 12
    assert complete["passed_runs"] == 11 and complete["unattempted_runs"] == 0
    assert len(visits) == len(set(visits)) == 12 and original_slot not in visits and len(sent) == 16
    assert all(r["network_requests"] == len(r["wire_attempts"]) == 1
               for r in [complete["retest"], *complete["runs"][1:]])
    assert all(path.read_bytes() == raw for path, raw in original_bytes.items())


def test_orphan_post_repair_directory_cannot_replay(campaign, monkeypatch):
    args = fixture_inputs(campaign)
    command(monkeypatch, campaign, "--reconcile", *args)
    folder = campaign / "revalidation"
    folder.mkdir()
    old = (campaign / "preflight.json").read_bytes()
    (folder / "result.json").write_bytes(old)
    with pytest.raises(ValueError, match="incomplete post-repair"):
        command(monkeypatch, campaign, "--execute", "--resume", "--revalidate-only")
    assert (folder / "result.json").read_bytes() == old


def test_copying_old_preflight_into_recorded_progress_cannot_authorize_retest(campaign, monkeypatch):
    args = fixture_inputs(campaign)
    command(monkeypatch, campaign, "--reconcile", *args)
    offline_configuration(monkeypatch, campaign)
    sent = mock_revalidation_network(monkeypatch)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda _: "fixture")
    command(monkeypatch, campaign, "--execute", "--resume", "--revalidate-only")
    progress = next(campaign.glob("progress-*.json"))
    state = json.loads(progress.read_bytes())
    fabricated = {**json.loads((campaign / "preflight.json").read_bytes()), "run": "revalidation",
                  "identity": state["revalidation"]["identity"], "status": "passed"}
    acceptance.save(campaign / "revalidation/result.json", fabricated)
    acceptance.save(progress, {**state, "revalidation": fabricated})
    with pytest.raises(ValueError):
        command(monkeypatch, campaign, "--execute", "--resume", "--retest", "bug-negative-clamp-r1")
    assert len(sent) == 4 and not (campaign / "retest-bug-negative-clamp-r1").exists()


@pytest.mark.parametrize("retest", [False, True])
def test_task_level_budget_failure_without_dispatch_leaves_other_slots_runnable(campaign, monkeypatch, retest):
    fixture_inputs(campaign)
    offline_configuration(monkeypatch, campaign)
    sent = mock_revalidation_network(monkeypatch)
    monkeypatch.setattr(acceptance, "load_saved_key", lambda _: "fixture")
    visited = []

    async def stopped(task, repeat, config, key, ledger, output, *, identity, retest_of=None):
        label = ("retest-" if retest_of else "") + task["id"] + "-r" + str(repeat)
        visited.append(label)
        (output / label).mkdir()
        if len(visited) == 1:
            result = {"run": label, "identity": identity, "status": "not_run_budget", "reason": "failed",
                      "errors": [], "first_failure_reason": "llm_budget", "final_stop_reason": "llm_budget",
                      "tokens": 0, "estimated_tokens": 0, "reported_tokens": 0, "network_requests": 0,
                      "wire_attempts": []}
        else:
            usage = await single_sample_call(config, key, ledger, label)
            result = {"run": label, "identity": identity, "status": "passed", **usage}
        if retest_of:
            result["retest_of"] = retest_of
        acceptance.save(output / label / "result.json", result)
        return result

    monkeypatch.setattr(acceptance, "run_sample", stopped)
    mode = ("--retest", "bug-negative-clamp-r1") if retest else ()
    command(monkeypatch, campaign, "--execute", "--resume", *mode)
    command(monkeypatch, campaign, "--execute", "--resume")
    command(monkeypatch, campaign, "--execute", "--resume", *mode)
    progress = json.loads(next(campaign.glob("progress-*.json")).read_bytes())
    assert not progress["budget"]["held"] and progress["budget"]["historical_held"] and not progress.get("stop_reason")
    failure = progress["retest"] if retest else progress["runs"][1]
    assert failure["final_stop_reason"] == "llm_budget" and failure["network_requests"] == 0
    assert len(progress["runs"]) == 12 and progress["passed_runs"] == (11 if retest else 10)
    assert progress["attempted_runs"] == progress["passed_runs"] + 1
    assert len(visited) == len(set(visited)) == (12 if retest else 11)
    assert len(sent) == 4 + progress["passed_runs"]
