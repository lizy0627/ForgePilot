"""Independent retests preserve the fixed samples and separate failure from stop."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_product_acceptance as harness
from acceptance_budget import RequestBudget


def test_first_network_failure_survives_final_budget_stop():
    history = {"status": "failed", "reason": "failed", "errors": ["llm_budget"],
               "accounting": [{"status": "network"}, {"status": "budget"}]}
    assert harness.sample_failure_reasons(history) == ("network", "llm_budget")
    wire = [{"failure_reason": "ReadTimeout"}]
    assert harness.sample_failure_reasons(history, wire) == ("ReadTimeout", "llm_budget")


def test_task_failure_event_explains_failed_graph_with_unfinished_node():
    class EventPages:
        def event_page(self, task, after, limit):
            assert task == "fixture" and limit == 128
            if not after:
                return {}, [{"event_type": "model.completed", "sequence": 128}], 130
            assert after == 128
            return {}, [{"event_type": "task.failed", "sequence": 129, "payload_json": '{"error_code":"graph_storage"}'},
                        {"event_type": "diagnostics.completed", "sequence": 130}], 130

    event = harness.sample_terminal_failure(EventPages(), "fixture")
    assert event == {"event_type": "task.failed", "sequence": 129, "error_code": "graph_storage"}
    report = {"status": "failed", "reason": "failed", "errors": [], "terminal_failure_event": event}
    assert harness.sample_failure_reasons(report) == ("graph_storage", "graph_storage")
    assert harness.sample_failure_reasons(report, [{"failure_reason": "ReadTimeout"}]) == ("ReadTimeout", "graph_storage")
    report["errors"] = ["llm_budget"]
    assert harness.sample_failure_reasons(report, [{"failure_reason": "ReadTimeout"}]) == ("ReadTimeout", "llm_budget")


@pytest.mark.asyncio
async def test_retest_rejects_changed_target_or_missing_original_before_allocating(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Invalid retest reached workspace or model initialization")

    monkeypatch.setattr(harness, "fixture", forbidden)
    token = RequestBudget(tmp_path / "budget.sqlite3")
    label = harness.TASKS[0]["id"] + "-r1"
    try:
        for task, repeat, retest in ((dict(harness.TASKS[0], public="true"), 1, label),
                                     (harness.TASKS[0], 4, label), (harness.TASKS[0], 1, "arbitrary"),
                                     (harness.TASKS[0], 1, label)):
            with pytest.raises((ValueError, FileNotFoundError)):
                await harness.run_sample(task, repeat, None, "", token, tmp_path, identity={}, retest_of=retest)
        assert not (tmp_path / ("retest-" + label)).exists() and not token.snapshot()["requests"]
    finally:
        token.close()


@pytest.mark.asyncio
async def test_failed_setup_occupies_unique_retest_and_keeps_original(tmp_path, monkeypatch):
    label = "bug-negative-clamp-r1"
    original = tmp_path / label
    original.mkdir()
    raw = (json.dumps({"run": label, "status": "failed", "errors": ["llm_budget"],
                       "accounting": [{"status": "network"}, {"status": "budget"}]}) + "\n").encode()
    (original / "result.json").write_bytes(raw)
    (original / "graph.json").write_bytes(b"original graph\n")
    identity = {"original_result_sha256": hashlib.sha256(raw).hexdigest()}

    def failed_setup(task):
        raise OSError("fixture setup failed")

    monkeypatch.setattr(harness, "fixture", failed_setup)
    ledger = RequestBudget(tmp_path / "budget.sqlite3")
    try:
        async def attempt():
            return await harness.run_sample(harness.TASKS[0], 1, None, "", ledger,
                                            tmp_path, identity=identity, retest_of=label)

        result = await attempt()
        assert result["status"] == "failed"
        assert result["retest_of"] == label and result["identity"] == identity
        assert result["run"] == "retest-" + label
        assert result["first_failure_reason"] == result["final_stop_reason"] == "OSError"
        assert result["network_requests"] == result["reported_tokens"] == result["estimated_tokens"] == 0
        with pytest.raises(FileExistsError):
            await attempt()
        assert result == json.loads((tmp_path / result["run"] / "result.json").read_bytes())
        assert (original / "result.json").read_bytes() == raw
        assert (original / "graph.json").read_bytes() == b"original graph\n"
    finally:
        ledger.close()
