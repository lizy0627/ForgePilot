"""Benchmark Card denominators, repetition rule and evidence provenance."""

import csv
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.metrics import summarize, task_results, write_reports
from forgepilot.security import Redactor


def row(task_id, index, *, success=False, first=False, repair=False,
        rounds=0, latency=None, tokens=None, reason="failed"):
    return {"id": task_id, "category": "C++ 逻辑 Bug", "run_index": index,
            "success": success, "first_pass_success": first,
            "repair_success": repair, "reason": reason,
            "state": "completed" if success else "failed",
            "build_status": "passed" if success else "failed",
            "ctest_status": "passed" if success else "failed",
            "oracle_status": "passed" if success else "failed",
            "integrity_status": "passed" if success else "failed",
            "test_status": "passed" if success else "failed", "test_count": 1 if success else 0,
            "tool_calls": index, "total_tokens": tokens, "latency_ms": latency,
            "repair_rounds": rounds}


def test_two_of_three_metrics_keep_missing_failures_and_unknown_usage():
    rows = [row("alpha-01", 1, success=True, first=True, latency=100, tokens=10),
            row("alpha-01", 2, success=True, repair=True, rounds=1, latency=300),
            row("beta-01", 1, success=True, first=True, latency=200, tokens=20),
            row("beta-01", 2, latency=400, tokens=30),
            row("beta-01", 3, latency=500, tokens=40)]
    selected = ["alpha-01", "beta-01"]
    categories = {task_id: "C++ 逻辑 Bug" for task_id in selected}
    result = task_results(rows, expected_repetitions=3, selected_tasks=selected, categories=categories)
    metrics = summarize(rows, expected_repetitions=3, selected_tasks=selected, categories=categories)
    assert [(task["stable_success"], task["successful_runs"], task["scheduled_runs"]) for task in result] == [(True, 2, 3), (False, 1, 3)]
    assert metrics["Task Success Rate"] == {"passed": 1, "denominator": 2, "value": .5}
    assert metrics["Run Success Rate"] == {"passed": 3, "denominator": 6, "value": .5}
    assert metrics["First-pass Success Rate"] == {"passed": 2, "denominator": 6, "value": 2/6}
    assert metrics["Post-repair Success Rate"] == {"passed": 1, "denominator": 1, "value": 1}
    assert metrics["Average Token Usage"] == {"value": 25, "known": 4, "unknown": 2, "eligible": 6}
    assert metrics["P50 Latency"] == {"value": 300, "known": 5, "unknown": 1, "eligible": 6}
    assert metrics["P95 Latency"]["value"] == 500
    assert metrics["Average Tool Calls"]["known"] == 5
    assert metrics["Average Repair Rounds"]["eligible"] == 6
    assert metrics["CTest Pass Rate"] == metrics["Oracle Pass Rate"] == {"passed": 3, "denominator": 6, "value": .5}


def test_report_is_derived_from_all_scheduled_rows_and_marks_offline(tmp_path):
    rows = [row("alpha-01", 1, success=True, first=True, latency=100, tokens=10),
            row("alpha-01", 2, reason="=formula sk-example")]
    metadata = {"run_id": "fixture", "mode": "offline", "model": "offline-fixed-script",
                "started_at": "2026-10-30T00:00:00Z", "finished_at": None,
                "repetitions": 3, "selected_tasks": ["alpha-01"],
                "task_categories": {"alpha-01": "C++ 逻辑 Bug"},
                "config_hash": "a" * 64,
                "comparison_configuration": {"dataset": "micro", "model": "offline-fixed-script",
                                             "max_total_tokens": 1000,
                                             "tools": {"cmake": "C:/cmake"},
                                             "validation": ["build", "CTest", "Oracle"]}}
    write_reports(tmp_path, metadata, rows, Redactor(("sk-example",)))
    document = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    flat = list(csv.DictReader((tmp_path / "results.csv").read_text(encoding="utf-8-sig").splitlines()))
    tasks = list(csv.DictReader((tmp_path / "task-results.csv").read_text(encoding="utf-8-sig").splitlines()))
    card = (tmp_path / "benchmark-card.md").read_text(encoding="utf-8")
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert document["schema_version"] == 2 and document["evidence_label"] == "offline"
    assert len(document["tasks"]) == len(flat) == 3
    assert document["tasks"][2]["reason"] == "missing_result"
    assert tasks[0]["stable_success"] == "False" and tasks[0]["scheduled_runs"] == "3"
    assert tasks[0]["stability_evidence"] == "two_of_three_rule"
    assert "Evidence label: **offline**" in card and "not real LLM performance" in card
    assert "Max total tokens: `1000`" in card and "Oracle" in card
    assert "P95 Latency" in card and "missing_result" in report
    assert "sk-example" not in report and "sk-example" not in flat[1]["reason"]
    assert flat[1]["reason"].startswith("'=formula")


@pytest.mark.parametrize("label,note", [("fake", "not real LLM performance"),
    ("retrieval", "not Agent success rates"),
    ("real", "formal success rates remain pending")])
def test_card_evidence_label_prevents_unearned_real_claim(tmp_path, label, note):
    metadata = {"run_id": "fixture", "mode": "real" if label == "real" else label,
                "evidence_label": label, "model": "fixture", "started_at": "fixture",
                "repetitions": 3, "selected_tasks": ["alpha-01"],
                "task_categories": {"alpha-01": "C++ 逻辑 Bug"},
                "real_llm_results_produced": False}
    write_reports(tmp_path, metadata, [], Redactor(()))
    card = (tmp_path / "benchmark-card.md").read_text(encoding="utf-8")
    assert note in card and f"Evidence label: **{label}**" in card
    assert json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))["metrics"]["Run Success Rate"]["denominator"] == 3


def test_duplicate_run_and_invalid_measurement_fail_closed():
    one = row("alpha-01", 1, latency=10)
    with pytest.raises(ValueError, match="Duplicate benchmark run"):
        summarize([one, dict(one)], expected_repetitions=3)
    with pytest.raises(ValueError, match="Invalid benchmark measurement"):
        summarize([dict(one, latency_ms=float("nan"))])


def test_real_card_distinguishes_complete_five_task_dataset_from_interruption(tmp_path):
    ids = [f"real-bug-0{index}" for index in (1, 2, 3)] + [f"real-feature-0{index}" for index in (1, 2)]
    rows = [row(task_id, index) for task_id in ids for index in (1, 2, 3)]
    metadata = {"run_id": "fixture", "mode": "real", "evidence_label": "real",
                "model": "fixture", "started_at": "fixture", "finished_at": "fixture",
                "real_llm_results_produced": True, "repetitions": 3,
                "selected_tasks": ids, "task_categories": {task_id: "C++ 逻辑 Bug" for task_id in ids},
                "comparison_configuration": {"dataset": "real"}}
    write_reports(tmp_path, metadata, rows, Redactor(()))
    card = (tmp_path / "benchmark-card.md").read_text(encoding="utf-8")
    assert "REAL Agent outcomes are observed" in card
    assert "15-task benchmark remains pending" not in card
    metadata["stopped_reason"] = "cancelled"
    write_reports(tmp_path, metadata, rows, Redactor(()))
    assert "incomplete or interrupted" in (tmp_path / "benchmark-card.md").read_text(encoding="utf-8")
    metadata.pop("stopped_reason")
    rows[-1].update(state="not_run", reason="not_run")
    write_reports(tmp_path, metadata, rows, Redactor(()))
    assert "incomplete or interrupted" in (tmp_path / "report.md").read_text(encoding="utf-8")
