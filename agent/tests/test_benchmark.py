"""Evaluator assertions remain outside every candidate repository; real compiler/CTest integration."""
from collections import Counter
from copy import deepcopy
import csv
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.grader import check_scope, grade_analysis, read_junit
from forgepilot_eval.manifest import DATA_ROOT, DISTRIBUTION, TaskSpec, asset, inventory, load_tasks
from forgepilot_eval.metrics import summarize, write_reports
from forgepilot_eval.offline import OfflineModel
from forgepilot_eval.runner import Benchmark, approve, empty_row, parser
from forgepilot.repository import Repository
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from conftest import ScriptedLLM, reply
from test_build import build_config
from test_planning import proposal


def test_fifteen_tasks_distribution_and_reviewable_contracts():
    tasks = load_tasks()
    assert len(tasks) == 15 and Counter(t.category for t in tasks) == DISTRIBUTION
    assert len({t.id for t in tasks}) == 15
    for task in tasks:
        assert task.request and task.initial_state.description and task.validation_commands and task.success.expected_behavior
        assert task.preauthorized_files == task.allowed_files
        if not task.analysis:
            assert asset(DATA_ROOT, task.success.oracle).is_file()
            assert not asset(DATA_ROOT, task.success.oracle).is_relative_to(asset(DATA_ROOT, task.fixture))
    solutions = json.loads((DATA_ROOT / "offline-solutions.json").read_text(encoding="utf-8"))["solutions"]
    assert set(solutions) == {t.id for t in tasks}
    assert all(set(solutions[t.id]["files"]) <= set(t.allowed_files) for t in tasks)


@pytest.mark.parametrize("mutation", ["traversal", "preauthorization", "duplicate", "outside_oracle"])
def test_invalid_manifest_scope_is_rejected(mutation):
    data = load_tasks()[0].model_dump(mode="json")
    if mutation == "traversal": data["fixture"] = "fixtures/../secrets"
    if mutation == "preauthorization": data["preauthorized_files"] = ["tests/public_test.cpp"]
    if mutation == "duplicate": data["allowed_files"] *= 2
    if mutation == "outside_oracle": data["success"]["oracle"] = "fixtures/fake.cpp"
    with pytest.raises(ValueError): TaskSpec.model_validate(data)


def test_inventory_rejects_hardlinked_assets(tmp_path):
    import os
    root = tmp_path / "source"; root.mkdir()
    (root / "item").write_text("protected")
    os.link(root / "item", tmp_path / "alias")
    with pytest.raises(ValueError, match="Linked"): inventory(root)


def test_metrics_success_failure_na_and_missing_usage(tmp_path):
    tasks = load_tasks()
    rows = [empty_row(tasks[0]), empty_row(tasks[1]), empty_row(next(t for t in tasks if t.analysis))]
    rows[0].update(success=True, build_status="passed", test_status="passed", test_count=1,
                   tool_calls=5, total_tokens=100, latency_ms=1000, repair_rounds=1)
    rows[1].update(success=False, build_status="passed", test_status="failed", test_count=0,
                   tool_calls=3, total_tokens=None, latency_ms=3000, repair_rounds=0)
    rows[2].update(success=True, tool_calls=1, total_tokens=200, latency_ms=2000)
    metrics = summarize(rows)
    assert metrics["Task Success Rate"] == {"passed": 2, "denominator": 3, "value": 2/3}
    assert metrics["Build Pass Rate"] == {"passed": 2, "denominator": 2, "value": 1}
    assert metrics["Test Pass Rate"] == {"passed": 1, "denominator": 2, "value": .5}
    assert metrics["Average Token Usage"] == {"value": 150, "known": 2, "unknown": 1, "eligible": 3}
    assert metrics["Average Tool Calls"]["value"] == 3 and metrics["Average Latency"]["value"] == 2000
    assert metrics["Average Repair Rounds"]["value"] == .5 and metrics["Average Repair Rounds"]["eligible"] == 2
    assert summarize([rows[2]])["Build Pass Rate"]["value"] is None
    assert summarize([empty_row(tasks[0])])["Average Token Usage"]["value"] is None
    assert summarize([])["Task Success Rate"]["value"] is None
    metadata = {"run_id": "unit", "mode": "offline", "model": "fixture", "started_at": "fixture", "finished_at": "fixture"}
    rows[1]["reason"] = "=formula sk-fixture-secret"
    write_reports(tmp_path, metadata, rows, Redactor(("sk-fixture-secret",)))
    result = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    csv_rows = list(csv.DictReader((tmp_path / "results.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert result["tasks"][1]["total_tokens"] is None and csv_rows[1]["total_tokens"] == "N/A"
    assert csv_rows[1]["reason"].startswith("'=formula") and "sk-fixture-secret" not in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "not real LLM performance" in (tmp_path / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("body,ok,count", [('<testsuite><testcase name="oracle"/></testsuite>', True, 1),
    ('<testsuite/>', False, 0), ('<testsuite><testcase name="oracle"><skipped/></testcase></testsuite>', False, 1),
    ('<testsuite><testcase name="wrong"/></testsuite>', False, 1),
    ('<testsuite><testcase name="oracle"/><testcase name="oracle"/></testsuite>', False, 2),
    ('<testsuite><testcase name="oracle"><failure/></testcase></testsuite>', False, 1), ('bad', False, 0)])
def test_zero_skipped_invalid_and_failure_tests_cannot_pass(tmp_path, body, ok, count):
    path = tmp_path / "report.xml"; path.write_text(body)
    result = read_junit(path, "oracle", True)
    assert (result["status"] == "passed") == ok and result["count"] == count
    assert read_junit(path, "oracle", False)["status"] == "failed"


def test_analysis_requires_exact_file_symbol_and_directed_relation_sets():
    task = next(t for t in load_tasks() if t.analysis)
    expected = task.success.expected
    assert grade_analysis(json.dumps(expected), expected)
    value = deepcopy(expected); value["relations"][0]["caller"] = "Counter::reset"
    assert not grade_analysis(json.dumps(value), expected)
    assert not grade_analysis("All correct! " + json.dumps(expected), expected)
    value = deepcopy(expected); value["files"].append("PROJECT.txt")
    assert not grade_analysis(json.dumps(value), expected)


@pytest.mark.parametrize("scenario", ["valid", "wrong_edge", "invalid_json"])
def test_saved_analysis_has_a_runnable_validation_command(tmp_path, scenario):
    import subprocess
    task = next(t for t in load_tasks() if t.analysis)
    answer = deepcopy(task.success.expected)
    if scenario == "wrong_edge": answer["relations"][0]["callee"] = "Counter::reset"
    path = tmp_path / "agent-result.json"
    data = json.dumps({"state": "completed", "final_result": json.dumps(answer)})
    path.write_text("broken" if scenario == "invalid_json" else data, encoding="utf-8")
    command = [sys.executable, str(DATA_ROOT / "check_analysis.py"), str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == (0 if scenario == "valid" else 1)
    assert json.loads(result.stdout) == {"task": "analysis-01", "passed": scenario == "valid"}


def test_protected_tests_and_extra_source_changes_cannot_score():
    task = load_tasks()[0]
    before = {"src/subject.cpp": "old", "tests/public_test.cpp": "protected"}
    assert check_scope(before, {**before, "src/subject.cpp": "new"}, task)["valid"]
    assert not check_scope(before, {**before, "tests/public_test.cpp": "new"}, task)["valid"]
    assert not check_scope(before, {**before, "outside.cpp": "new"}, task)["valid"]


@pytest.fixture
def benchmark_factory(tmp_path, build_config, rg_exe, git_exe):
    def create(task_id, scenario="success"):
        args = parser().parse_args(["--mode", "offline", "--approval", "manifest", "--task", task_id,
            "--cmake", build_config.cmake, "--ctest", build_config.ctest, "--generator", "Ninja", "--ninja", build_config.make_program,
            "--rg", rg_exe, "--git", git_exe, "--output-root", str(tmp_path / "reports")])
        benchmark = Benchmark(args)
        benchmark.model_factory = lambda t: OfflineModel(t, benchmark.solutions[t.id], scenario)
        return benchmark
    return create


@pytest.mark.asyncio
async def test_actual_success_isolated_copy_report_and_unknown_usage(benchmark_factory):
    benchmark = benchmark_factory("cpp-compile-01")
    before = inventory(DATA_ROOT)
    output, rows, metrics = await benchmark.run()
    row = rows[0]
    assert row["success"] and row["build_status"] == row["test_status"] == "passed" and row["test_count"] == 1
    assert row["total_tokens"] is None and metrics["Average Token Usage"]["known"] == 0
    assert row["tool_calls"] > 0 and row["latency_ms"] > 0
    repo = Path(row["repository"])
    assert not repo.is_relative_to(DATA_ROOT.parent) and repo.exists()
    assert inventory(DATA_ROOT) == before
    approval = json.loads((output / row["id"] / "approvals.json").read_text(encoding="utf-8"))[0]
    assert approval["approved"] and approval["plan_version"] == 1 and approval["task_id"] == row["agent_task_id"]
    assert json.loads((output / row["id"] / "agent-result.json").read_text(encoding="utf-8"))["approved_files"] == ["src/subject.cpp"]
    assert all((output / name).is_file() for name in ("results.json", "results.csv", "report.md"))
    repository = await Repository.open(str(repo), rg_executable=benchmark.rg, git_executable=benchmark.git)
    with pytest.raises(Exception): await repository.read_file("../final/protected/oracle.cpp")


@pytest.mark.asyncio
async def test_actual_failure_retains_artifacts_and_does_not_trust_model_summary(benchmark_factory):
    benchmark = benchmark_factory("cpp-logic-01", "no_change")
    output, rows, metrics = await benchmark.run()
    row = rows[0]
    assert not row["success"] and row["build_status"] == "passed" and row["test_status"] == "failed"
    assert row["state"] == "failed" and Path(row["repository"]).exists()
    assert (output / row["id"] / "trace.json").is_file()
    assert metrics["Task Success Rate"]["value"] == 0 and metrics["Test Pass Rate"]["value"] == 0


@pytest.mark.asyncio
async def test_actual_scope_refusal_stops_before_write_and_preserves_original(benchmark_factory):
    benchmark = benchmark_factory("cpp-compile-02", "out_of_scope")
    output, rows, _ = await benchmark.run()
    row = rows[0]
    assert not row["success"] and row["reason"] == "authorization_scope" and row["state"] == "cancelled"
    assert row["build_status"] == row["test_status"] == "not_run"
    repo = Path(row["repository"])
    assert (repo / "src/subject.cpp").read_bytes() == asset(DATA_ROOT, "fixtures/cpp-compile-02/src/subject.cpp").read_bytes()
    trace = json.loads((output / row["id"] / "trace.json").read_text(encoding="utf-8"))
    assert not any(e["kind"] == "plan_approved" or e["kind"] == "file_changed" for e in trace)


@pytest.mark.asyncio
async def test_analysis_na_and_second_run_uses_new_repository(benchmark_factory):
    benchmark = benchmark_factory("analysis-01")
    first = await benchmark.run()
    second = await benchmark.run()
    assert first[1][0]["repository"] != second[1][0]["repository"]
    row = first[1][0]
    assert row["success"] and row["test_status"] == row["build_status"] == "not_applicable"
    assert first[2]["Build Pass Rate"]["denominator"] == 0 and row["total_tokens"] is None


@pytest.mark.asyncio
async def test_early_zero_exit_cannot_fake_protected_assertion_completion(benchmark_factory, tmp_path):
    import shutil
    benchmark = benchmark_factory("cpp-logic-01")
    task = benchmark.tasks[0]
    repo = tmp_path / "candidate"
    shutil.copytree(asset(DATA_ROOT, task.fixture), repo)
    (repo / "src/subject.cpp").write_text('#include "subject.h"\n#include <cstdlib>\nint clamp_level(int value){std::exit(0);}\n', encoding="utf-8")
    result = await benchmark.grader.run(task, repo, tmp_path / "protected-evaluation")
    assert result["build_status"] == "passed" and result["test_count"] == 1
    assert result["commands"][-1]["returncode"] == 0  # CTest alone would falsely pass this.
    assert result["test_status"] == "failed" and result["test_reason"] == "protected_oracle_did_not_complete"


@pytest.mark.asyncio
async def test_revised_scope_stops_and_retains_prior_authorized_edits(benchmark_factory):
    from forgepilot_eval.offline import call, message, plan
    benchmark = benchmark_factory("cpp-compile-01")
    task = benchmark.tasks[0]
    fixed = benchmark.solutions[task.id]["files"]["src/subject.cpp"]
    model = ScriptedLLM([reply("offline analysis"), reply(json.dumps(plan(task, ["src/subject.cpp"]))),
        message(calls=[call("read_file", {"path": "src/subject.cpp"}, "read-source"),
                       call("write_file", {"path": "src/subject.cpp", "content": fixed}, "approved"),
                       call("write_file", {"path": "PROJECT.txt", "content": "forbidden"}, "extra")]),
        reply(json.dumps(plan(task, ["src/subject.cpp", "PROJECT.txt"])))])
    benchmark.model_factory = lambda _: model
    output, rows, _ = await benchmark.run()
    row = rows[0]
    assert row["reason"] == "authorization_scope" and row["state"] == "cancelled"
    decisions = json.loads((output / row["id"] / "approvals.json").read_text(encoding="utf-8"))
    assert [d["approved"] for d in decisions] == [True, False]
    assert [d["plan_version"] for d in decisions] == [1, 2]
    repo = Path(row["repository"])
    assert (repo / "src/subject.cpp").read_text(encoding="utf-8") == fixed
    assert (repo / "PROJECT.txt").read_bytes() == asset(DATA_ROOT, task.fixture + "/PROJECT.txt").read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("preauthorized,mode,answer,approved", [(None, "manifest", "", False),
    ([], "manifest", "", False), (["main.cpp"], "manifest", "", True),
    (None, "manual", "approve 1\n", True), (None, "manual", "", False)])
async def test_preapproval_and_manual_review_use_same_versioned_api(cpp_repo, rg_exe, git_exe, preauthorized, mode, answer, approved):
    task = load_tasks()[0].model_copy(update={"allowed_files": ["main.cpp"], "preauthorized_files": preauthorized})
    service = TaskService(ScriptedLLM([reply("analysis"), reply(json.dumps(proposal("main.cpp")))]),
                          rg_executable=rg_exe, git_executable=git_exe, trace=MemoryTrace())
    pending = await service.start(str(cpp_repo), "fixture")
    async def read(): return answer
    decision = await approve(service, pending, task, mode, read)
    assert decision["approved"] == approved
    assert service.snapshot().state == ("modifying" if approved else "cancelled")
    assert service.is_file_approved(pending.task_id, pending.plan_version, "main.cpp") == approved
    service.close()
