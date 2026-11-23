from copy import deepcopy
import json

import pytest

from forgepilot.artifacts.canonical import content_sha256
from forgepilot_eval.skill_pair_metrics import summarize, write_report


def sample():
    skill = {"skill_id": "reviewed-qt", "version": 1, "content_sha256": "d" * 64}
    locks = {k: "a" * 64 for k in ("task_spec_sha256", "task_sha256", "fixture_sha256", "oracle_sha256",
                                     "solution_sha256", "runtime_sha256", "evaluator_sha256")}
    locks.update(model="fixed", topology="single", task_id="model-01", validator={"name": "example"},
                 budget={"tools": 30}, skill_content_sha256=skill["content_sha256"])
    verification = {"passed": True, "protected_unchanged": True, "candidate_unchanged": True,
                    "configure_status": "passed", "build_status": "passed", "public_test_status": "passed",
                    "oracle_status": "passed", "public_test_count": 1, "oracle_test_count": 1}
    rows = []
    for index, (first, final, repair, calls, tokens) in enumerate([
        (True, True, 0, 3, 12), (False, True, 1, 6, None), (None, None, None, None, None),
    ], 1):
        for arm in ("off", "on"):
            rows.append({"id": "model-01", "run_index": index, "arm": arm,
                         "selected_skills": [skill] if arm == "on" else [],
                         "stage_receipts": [dict(skill, node_id="root_cause_analysis", attempt=1)] if arm == "on" else [],
                         "analyst_input_sha256": {"root_cause_analysis": "e" * 64, "impact_test_analysis": "f" * 64},
                         "verification": verification, "candidate_unchanged_by_grading": True, "scope": {"valid": True},
                         "conditions": deepcopy(locks), "conditions_sha256": content_sha256(locks),
                         "first_pass_success": first, "success": final, "repair_rounds": repair,
                         "tool_calls": calls, "investigation_tool_calls": calls, "input_tokens": tokens,
                         "latency_ms": 0 if index == 1 else None, "reason": "interrupted" if final is None else "passed"})
    rows[3].update(success=False, reason="oracle_failed",
                   verification=dict(verification, passed=False, oracle_status="failed"))
    for row in rows[4:]:
        for key in ("verification", "scope", "candidate_unchanged_by_grading", "analyst_input_sha256", "stage_receipts"):
            del row[key]
    return {"evidence_label": "offline", "paid_model_calls": 0, "repetitions": 3,
            "tasks": [{"id": "model-01", "split": "holdout"}],
            "skill": skill, "rows": rows}


def test_known_pair_arithmetic_preserves_both_failures_and_unknowns(tmp_path):
    doc = sample()
    summary = write_report(doc, tmp_path)
    assert summary == json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["pairs"] == 3
    off, on = (summary["arms"][arm] for arm in ("off", "on"))
    assert off["first_pass"] == {"passed": 1, "unknown": 1, "denominator": 3, "rate": 1/3}
    assert off["final_pass"]["rate"] == 2/3
    assert on["final_pass"]["rate"] == 1/3
    assert off["tool_calls"] == {"known": 2, "unknown": 1, "denominator": 3, "median": 4.5, "mean": 4.5}
    assert off["repair_rounds"]["mean"] == .5 and off["input_tokens"]["mean"] == 12
    assert off["latency_ms"]["median"] == 0 and off["latency_ms"]["unknown"] == 2
    assert summary["by_split"]["holdout"] == summary["by_task"]["model-01"] == summary["arms"]
    assert [(r["run_index"], r["arm"], r["reason"]) for r in summary["failures_or_unknowns"]] == [
        (2, "on", "oracle_failed"), (3, "off", "interrupted"), (3, "on", "interrupted")]


@pytest.mark.parametrize("fault", ["changed_budget", "stale_hash", "wrong_selection", "missing_arm",
                                   "duplicate", "real_label", "negative_calls", "bogus_pass", "no_receipt", "zero_test",
                                   "different_prompt", "zero_round_late_success"])
def test_refuse_unpaired_or_invalid_evidence(fault):
    document = sample()
    row = document["rows"][1]
    if fault in {"changed_budget", "stale_hash"}:
        row["conditions"]["budget"]["tools"] += 1
        if fault == "changed_budget":
            row["conditions_sha256"] = content_sha256(row["conditions"])
    elif fault == "wrong_selection":
        row["selected_skills"] = []
    elif fault == "missing_arm":
        document["rows"].pop()
    elif fault == "duplicate":
        document["rows"].append(row)
    elif fault == "real_label":
        document["evidence_label"] = "real"
    elif fault == "negative_calls":
        row["tool_calls"] = -1
    elif fault == "bogus_pass":
        row["success"] = False
    elif fault == "no_receipt":
        row["stage_receipts"] = []
    elif fault == "zero_test":
        row["verification"]["oracle_test_count"] = 0
    elif fault == "different_prompt":
        row["analyst_input_sha256"]["root_cause_analysis"] = "b" * 64
    elif fault == "zero_round_late_success":
        row["first_pass_success"] = False
    with pytest.raises(ValueError):
        summarize(document)
