"""Check the experiment's real analyst boundary and its handling of failed grading."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from forgepilot_eval.skill_pairs import run_campaign
from run_skill_pairs import audit_corpus
from skill_cases import load_cases


class InspectingValidator:
    conditions = {"name": "unit-test-scoring"}

    def __init__(self):
        self.candidates = []

    async def run(self, case, repo, scratch):
        self.candidates.append(repo)
        assert (repo / "src/subject.cpp").read_bytes() == case.solution["files"]["src/subject.cpp"].encode()
        assert not (repo / "oracles").exists()
        return {"passed": False, "test_reason": "injected oracle failure"}


@pytest.mark.asyncio
async def test_actual_analyst_dispatch_and_persisted_failed_pair(tmp_path):
    case = load_cases()[0]
    validator = InspectingValidator()
    result = await run_campaign([case], validator, tmp_path / "run", repetitions=1)
    assert result == json.loads((tmp_path / "run/results.json").read_text(encoding="utf-8"))
    off, on = result["rows"]
    assert off["arm"] == "off" and on["arm"] == "on"
    assert off["conditions"] == on["conditions"]
    assert off["analyst_input_sha256"] == on["analyst_input_sha256"]
    assert set(on["analyst_input_sha256"]) == {"root_cause_analysis", "impact_test_analysis"}
    assert off["selected_skills"] == off["stage_receipts"] == []
    assert on["selected_skills"] == [result["skill"]]
    assert on["stage_receipts"] == [dict(result["skill"], node_id="root_cause_analysis", attempt=1)]
    assert off["selection_payload_sha256"] != on["selection_payload_sha256"]
    assert len(set(validator.candidates)) == 2
    audit_corpus(result, [case])
    for arm_index, key, damage in ((0, "selection_payload_sha256", "0" * 64), (1, "stage_receipts", [])):
        damaged = deepcopy(result)
        damaged["rows"][arm_index][key] = damage
        with pytest.raises(ValueError, match="skill dispatch"):
            audit_corpus(damaged, [case])
    for row in (on, off):
        assert row["success"] is row["first_pass_success"] is False
        assert row["reason"] == "validation_failed"
        assert row["verification"]["test_reason"] == "injected oracle failure"
        assert row["investigation_tool_calls"] == 2 and row["tool_calls"] == 4
        assert row["input_tokens"] is None and row["repair_rounds"] == 0
        assert row["latency_ms"] > 0


@pytest.mark.asyncio
async def test_unapproved_script_preserves_denominator_and_unobserved_validation(tmp_path):
    case = replace(load_cases()[0], solution={"files": {"tests/public_test.cpp": "invalid"}})
    validator = InspectingValidator()
    result = await run_campaign([case], validator, tmp_path / "run", repetitions=1)
    assert not validator.candidates
    for row in result["rows"]:
        assert row["success"] is False
        assert row["first_pass_success"] is row["repair_rounds"] is None
        assert row["reason"].endswith("script_outside_approved_scope")

def test_cli_audit_rejects_removed_task_even_when_both_arms_disappear():
    cases = load_cases()
    document = {"tasks": [{"id": case.task.id, "split": case.split} for case in cases[:-1]], "rows": []}
    with pytest.raises(ValueError, match="registered Qt corpus"):
        audit_corpus(document, cases)
