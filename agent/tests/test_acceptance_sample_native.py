"""Run one corrected retest through the production Mission using only in-memory model HTTP."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_product_acceptance as harness
from acceptance_budget import BudgetTransport, RequestBudget
from closed_loop_support import FIXED, FakeProvider, done, patch, review_response
from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import ExecutionPlan, TaskSpec
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from reconciliation_support import make_fixture
from test_analysis_workflow import spec_data
from test_mission_api_workflow import AnalysisProvider


@pytest.mark.asyncio
async def test_corrected_retest_exports_validated_patch_without_replacing_failure(tmp_path, monkeypatch):
    task = harness.TASKS[0]
    slot = task["id"] + "-r1"
    config = ModelConfiguration(
        profiles=tuple(ModelProfile(
            profile_id=name, base_url="https://ark.cn-beijing.volces.com/api/v3", api_key_ref="env:FORGEPILOT_API_KEY",
            model=("doubao-seed-2-1-" if name == "review" else "deepseek-v4-1-") + name,
            family="reviewer" if name == "review" else "writer", max_output_tokens=2048,
        ) for name in ("analysis", "implement", "repair", "review")),
        routes=tuple(ModelRoute(route_id=route, primary=profile) for route, profile in (
            ("task", "implement"), ("chat", "implement"), ("summary", "implement"), ("orchestration", "analysis"),
            ("analysis", "analysis"), ("implementation", "implement"), ("review", "review"), ("repair", "repair"),
        )),
    )
    spec = spec_data()
    spec.update(title="Negative clamp", objective=task["objective"], task_type=task["kind"],
                allowed_scope={"path_patterns": ["src/value.cpp"], "max_changed_files": 1},
                test_policy={"existing_tests_read_only": True, "allow_new_tests": False,
                             "modifiable_existing_test_patterns": []})
    spec["acceptance_criteria"] = [
        {"id": "AC-1", "description": "Build passes", "verification": "build"},
        {"id": "AC-2", "description": "Public CTest passes", "verification": "ctest"},
        {"id": "AC-3", "description": "Protected CTest passes", "verification": "protected_test"},
    ]
    spec_artifact = create_artifact(task_id="10000000-0000-4000-a000-000000000001", producer="orchestrator.v1",
                                    producer_node="draft_task_spec", content=TaskSpec.model_validate(spec))
    plan = ExecutionPlan.model_validate({
        "task_spec_artifact_id": spec_artifact.artifact_id, "summary": "Clamp negatives",
        "steps": [{"step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [], "expected_files": ["src/value.cpp"],
                   "action": "Clamp negatives and preserve the public API", "verification": ["build", "ctest", "protected_test"],
                   "risk": "Boundary conditions", "adds_tests": False, "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3"]}],
    })
    model_case = SimpleNamespace(spec=spec_artifact, plan=SimpleNamespace(content=plan))
    source_bytes = {}
    original_fixture = harness.fixture

    def fixture(task):
        initialized = original_fixture(task)
        source_bytes["value"] = (initialized[1] / "src/value.cpp").read_bytes()
        providers["implement"].responses = [patch(source_bytes["value"].decode(), FIXED), done()]
        return initialized

    monkeypatch.setattr(harness, "fixture", fixture)
    providers = {"analysis": AnalysisProvider(model_case), "implement": FakeProvider(), "repair": FakeProvider(),
                 "review": FakeProvider([review_response()])}
    dispatched = []

    async def respond(request, label):
        payload = json.loads(request.content)
        profile = next(p for p in config.profiles if p.model == payload["model"])
        dispatched.append(label)
        if label == "revalidation":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
                                                           "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}})
        reply = await providers[profile.profile_id].chat(payload["messages"])
        message = reply.message.model_dump(exclude_none=True)
        if profile.profile_id == "analysis":
            data = json.loads(message["content"])
            for item in data.get("items", []):
                item["file_hash"] = hashlib.sha256(source_bytes["value"]).hexdigest()
            message["content"] = json.dumps(data)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}})

    def offline_transport(ledger, label, **kwargs):
        async def dispatch(request):
            return await respond(request, label())
        return BudgetTransport(ledger, label, httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(harness, "BudgetTransport", offline_transport)
    monkeypatch.setattr(harness, "PAID_CAMPAIGN", tmp_path)
    monkeypatch.setattr(harness, "saved_configuration", lambda: (tmp_path / "fixture-config", config))
    monkeypatch.setattr(harness, "load_saved_key", lambda path: "fixture-key")
    monkeypatch.setattr(harness, "configuration_revision", lambda path: "2" * 64)

    async def local_fields(path):
        return {"config_revision": "2" * 64, "fields": {"embedding_enabled": False}}

    monkeypatch.setattr(harness, "load_configuration", local_fields)
    inputs = make_fixture(tmp_path)
    harness.save(tmp_path / slot / "graph.json", {"nodes": [{"node_id": "draft_task_spec", "status": "failed"}]})
    summary = json.loads((tmp_path / "summary.json").read_bytes())
    summary["runs"][0]["errors"] = ["llm_budget"]
    harness.save(tmp_path / slot / "result.json", summary["runs"][0])
    summary.update(model_configuration_sha256=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
                   models=[{"profile": p.profile_id, "model": p.model, "family": p.family} for p in config.profiles])
    harness.save(tmp_path / "summary.json", summary)
    frozen = [tmp_path / n for n in ("summary.json", "preflight.json", "plan.json", "budget-hold-audit.json")]
    original = {p: p.read_bytes() for p in [*frozen, *(tmp_path / slot).iterdir()]}
    harness.reconcile_offline(tmp_path, inputs)
    await harness.execute(tmp_path, retest=slot)
    progress = json.loads(next(tmp_path.glob("progress-*.json")).read_bytes())
    assert progress.get("revalidation", {}).get("status") == "passed", progress
    result = progress["retest"]
    assert result == json.loads((tmp_path / ("retest-" + slot) / "result.json").read_bytes())
    assert (progress["planned_runs"], progress["attempted_runs"], progress["passed_runs"], progress["unattempted_runs"]) == (12, 1, 0, 11)
    assert progress["runs"] == summary["runs"]
    ledger = RequestBudget(tmp_path / "budget.sqlite3", existing_only=True)
    try:
        assert result["status"] == "passed", result
        assert result["source_unchanged"] and result["original_failure"]["first_failure_reason"] == "network"
        assert result["original_failure"]["final_stop_reason"] == "llm_budget"
        assert result["run"] == "retest-" + slot and result["retest_of"] == slot
        assert result["identity"]["original_result_sha256"] == hashlib.sha256(original[tmp_path / slot / "result.json"]).hexdigest()
        assert result["first_failure_reason"] is None and result["final_stop_reason"] is None
        assert result["network_requests"] == dispatched.count("retest-" + slot) == 7
        assert dispatched.count("revalidation") == len(config.profiles) + 2 == 6
        assert result["reported_tokens"] == 280 and result["estimated_tokens"] == 0
        assert result["writer_families"] == ["writer"] and result["reviewer_families"] == ["reviewer"]
        rows = ledger.snapshot()["requests"]
        assert len(rows) == 15 and ledger.snapshot()["charged_or_reserved"] == 140 + 30 + 280
        await harness.execute(tmp_path, retest=slot)
        assert len(dispatched) == 13 and ledger.snapshot()["requests"] == rows
        assert all(path.read_bytes() == raw for path, raw in original.items())
    finally:
        ledger.close()
