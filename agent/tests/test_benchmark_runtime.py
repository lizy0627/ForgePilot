"""Run admission, budget, A/B lock and interrupted report contracts."""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.manifest import load_tasks
from forgepilot_eval.runner import (Benchmark, BudgetedModel, TokenBudget, main,
    approve, comparison_lock, empty_row, parser, verify_ab_pair)
from forgepilot.security import Redactor
from test_build import build_config


def test_default_is_offline_and_paid_admission_fails_closed(monkeypatch, capsys):
    assert parser().parse_args([]).mode == "offline"
    with pytest.raises(ValueError, match="allow-paid-real"):
        Benchmark(parser().parse_args(["--mode", "real"]))
    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_API_KEY", "sk-test-never-used")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL", "fixture-model")
    import forgepilot_eval.runner as runner
    monkeypatch.setattr(runner, "OpenAICompatibleClient", lambda *_args, **_kwargs:
                        pytest.fail("real model must not be constructed"))
    assert main(["--mode", "real", "--allow-paid-real", "--max-total-tokens", "10000"]) == 2
    assert "isolation unavailable" in capsys.readouterr().err
    with pytest.raises(ValueError, match="not yet connected"):
        Benchmark(parser().parse_args(["--dataset", "real"]))
    with pytest.raises(ValueError, match="not yet connected"):
        Benchmark(parser().parse_args(["--agent-topology", "multi"]))


class _PaidStub:
    def __init__(self, usages):
        self.usages = iter(usages)
        self.calls = 0
        self.redactor = Redactor()
        self.max_output_tokens = None

    async def chat(self, messages, tools=None):
        self.calls += 1
        usage = next(self.usages)
        if isinstance(usage, BaseException):
            raise usage
        return SimpleNamespace(usage=usage)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_paid_budget_stops_after_missing_usage_or_overshoot():
    model = _PaidStub([{"total_tokens": 3}, {"total_tokens": 3}])
    campaign = TokenBudget(5)
    limited = BudgetedModel(model, campaign, 5)
    await limited.chat([])
    with pytest.raises(ValueError, match="budget exceeded"):
        await limited.chat([])
    with pytest.raises(ValueError, match="exhausted"):
        await limited.chat([])
    assert model.calls == 2 and campaign.spent == 5

    model = _PaidStub([None])
    campaign = TokenBudget(10)
    limited = BudgetedModel(model, campaign, 10)
    with pytest.raises(ValueError, match="requires provider total_tokens"):
        await limited.chat([])
    with pytest.raises(ValueError, match="exhausted"):
        await limited.chat([])
    assert model.calls == 1

    model = _PaidStub([RuntimeError("provider failed after request")])
    campaign = TokenBudget(10)
    limited = BudgetedModel(model, campaign, 10)
    with pytest.raises(RuntimeError, match="provider failed"):
        await limited.chat([])
    with pytest.raises(ValueError, match="exhausted"):
        await limited.chat([])
    assert model.calls == 1 and campaign.remaining == 0


@pytest.mark.asyncio
async def test_manual_approval_timeout_cancels_review_without_later_grant():
    task = load_tasks()[0]
    plan = SimpleNamespace(files_to_modify=[SimpleNamespace(path=task.allowed_files[0])],
                           model_dump=lambda **_: {"files_to_modify": task.allowed_files})
    pending = SimpleNamespace(plan=plan, task_id="test-task", plan_version=1)
    cancelled = []
    service = SimpleNamespace(redactor=Redactor(),
                              approve=lambda *_: pytest.fail("timeout must not approve"),
                              cancel_review=lambda task_id: cancelled.append(task_id))
    async def never():
        await asyncio.Event().wait()
    decision = await approve(service, pending, task, "manual", never, timeout_seconds=0.01)
    assert decision["reason"] == "approval_timeout" and decision["approved"] is False
    assert cancelled == ["test-task"]


def test_ab_lock_requires_identical_task_budget_tools_timeout_and_tests():
    args = parser().parse_args(["--task-timeout", "900", "--max-tokens-per-run", "20000"])
    env = {"tools": {"git": "git", "cmake": "cmake"}, "versions": {"cmake": "fixture"}}
    tasks = load_tasks()[:1]
    sources = {"fixtures/a": "source", "oracles/a": "oracle"}
    one = comparison_lock(tasks, sources, args, env, "model-a")
    pair = ({"agent_topology": "single", "config_hash": one["hash"],
             "comparison_configuration": one["configuration"]},
            {"agent_topology": "multi", "config_hash": one["hash"],
             "comparison_configuration": one["configuration"]})
    assert verify_ab_pair(*pair)
    args.agent_topology = "multi"
    assert comparison_lock(tasks, sources, args, env, "model-a")["hash"] == one["hash"]
    args.task_timeout = 901
    other = comparison_lock(tasks, sources, args, env, "model-a")
    assert other["hash"] != one["hash"]
    with pytest.raises(ValueError, match="mismatch"):
        verify_ab_pair(pair[0], {**pair[1], "config_hash": other["hash"],
                                 "comparison_configuration": other["configuration"]})
    args.task_timeout = 900
    assert comparison_lock(tasks, sources, args, env, "model-b")["hash"] != one["hash"]
    assert comparison_lock([tasks[0].model_copy(update={"request": "changed request"})],
                           sources, args, env, "model-a")["hash"] != one["hash"]
    assert comparison_lock(tasks, {**sources, "fixtures/a": "changed"}, args, env, "model-a")["hash"] != one["hash"]
    assert comparison_lock(tasks, sources, args, {**env, "tools": {"cmake": "different"}}, "model-a")["hash"] != one["hash"]
    args.max_tokens_per_run += 1
    assert comparison_lock(tasks, sources, args, env, "model-a")["hash"] != one["hash"]


@pytest.fixture
def quick_benchmark(tmp_path, build_config, rg_exe, git_exe):
    args = parser().parse_args(["--mode", "offline", "--task", "cpp-compile-01",
        "--cmake", build_config.cmake, "--ctest", build_config.ctest,
        "--generator", "Ninja", "--ninja", build_config.make_program,
        "--rg", rg_exe, "--git", git_exe, "--output-root", str(tmp_path / "reports")])
    benchmark = Benchmark(args)
    async def environment():
        return {"tools": {"cmake": benchmark.cmake, "ctest": benchmark.ctest,
                          "git": benchmark.git, "rg": benchmark.rg, "ninja": benchmark.ninja},
                "versions": {}}
    benchmark.environment = environment
    return benchmark


@pytest.mark.asyncio
async def test_three_runs_retain_failure_and_new_slots(quick_benchmark):
    benchmark = quick_benchmark
    async def run_task(task, output, sources, run_index):
        if run_index == 2:
            raise RuntimeError("simulated evaluator phase failure")
        row = empty_row(task, run_index)
        row.update(state="completed", success=True,
                   first_pass_success=run_index == 1, repair_success=run_index == 3,
                   reason="verified",
                   build_status="passed", ctest_status="passed", oracle_status="passed",
                   test_status="passed", test_count=2, repair_rounds=0 if run_index == 1 else 1,
                   repository=f"repo-{run_index}")
        return row
    benchmark.run_task = run_task
    output, rows, metrics = await benchmark.run()
    assert [row["run_index"] for row in rows] == [1, 2, 3]
    assert [row["repository"] for row in rows] == ["repo-1", None, "repo-3"]
    assert [row["success"] for row in rows] == [True, False, True]
    assert rows[1]["reason"] == "evaluation_error:RuntimeError"
    assert (output / "cpp-compile-01" / "run-02" / "result.json").is_file()
    assert metrics["Task Success Rate"]["value"] == 1
    data = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 3 and data["task_results"][0]["stable_success"]


@pytest.mark.asyncio
async def test_cancel_keeps_scheduled_denominator_and_partial_report(quick_benchmark):
    benchmark = quick_benchmark
    async def run_task(task, output, sources, run_index):
        if run_index == 2:
            raise asyncio.CancelledError()
        return empty_row(task, run_index)
    benchmark.run_task = run_task
    with pytest.raises(asyncio.CancelledError):
        await benchmark.run()
    reports = list(Path(benchmark.args.output_root).glob("offline-*/results.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["metadata"]["stopped_reason"] == "cancelled"
    assert len(data["tasks"]) == 3
    assert data["metrics"]["Run Success Rate"]["denominator"] == 3


@pytest.mark.asyncio
async def test_manual_denial_stops_campaign_and_keeps_future_slots(quick_benchmark):
    benchmark = quick_benchmark
    async def run_task(task, output, sources, run_index):
        assert run_index == 1
        row = empty_row(task, run_index)
        row.update(reason="human_review_denied", approval_not_granted=True, finished_at="fixture")
        return row
    benchmark.run_task = run_task
    output, rows, metrics = await benchmark.run()
    assert [row["reason"] for row in rows] == ["human_review_denied", "not_run", "not_run"]
    data = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert data["metadata"]["stopped_reason"] == "manual_approval_not_granted"
    assert metrics["Run Success Rate"]["denominator"] == 3


@pytest.mark.asyncio
async def test_unknown_paid_spend_stops_future_scheduled_runs(quick_benchmark):
    benchmark = quick_benchmark
    benchmark.token_budget = TokenBudget(1)
    async def run_task(task, output, sources, run_index):
        assert run_index == 1
        benchmark.token_budget.spent = benchmark.token_budget.limit
        row = empty_row(task, run_index)
        row.update(reason="evaluation_error:provider_unknown", finished_at="fixture")
        return row
    benchmark.run_task = run_task
    output, rows, _ = await benchmark.run()
    assert [row["reason"] for row in rows] == ["evaluation_error:provider_unknown", "not_run", "not_run"]
    data = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert data["metadata"]["stopped_reason"] == "token_budget_exhausted_or_unknown"
