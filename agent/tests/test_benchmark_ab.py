"""A/B conclusions must come from two complete saved real reports."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.ab import _sha256, compare_saved
from forgepilot_eval.campaign_budget import BudgetPlan
from forgepilot_eval.manifest import DATA_ROOT, verify_real_lock
from forgepilot_eval.metrics import write_reports
from forgepilot.security import Redactor


def make_pair(tmp_path, *, token_ratio=2.4, safety=0, campaign_cap=100_000, per_run_cap=10_000, rag="off"):
    tasks = verify_real_lock()
    lock = json.loads((DATA_ROOT / "real_tasks" / "lock.json").read_text(encoding="utf-8"))
    endpoint = "https://fixture.invalid/v1"
    profile = {"provider": "openai-compatible", "model": "fixture-model",
               "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
               "credential_ref": "env:FORGEPILOT_API_KEY", "timeout_seconds": 60,
               "temperature": "provider_default", "max_output_tokens": 4096}
    routing = {"profiles": [
        {"profile_id": "review", "model": "fixture-review", "family": "other",
         "endpoint_sha256": profile["endpoint_sha256"],
         "credential_ref": profile["credential_ref"], "temperature": "provider_default",
         "max_output_tokens": 4096, "timeout_seconds": 60, "supports_tool_calling": True},
        {"profile_id": "task", "model": "fixture-model", "family": "primary",
         "endpoint_sha256": profile["endpoint_sha256"],
         "credential_ref": profile["credential_ref"], "temperature": "provider_default",
         "max_output_tokens": 4096, "timeout_seconds": 60, "supports_tool_calling": True},
    ], "routes": [
        {"route_id": name, "role": None, "primary": "review" if name == "review" else "task",
         "fallback": None}
        for name in ("analysis", "implementation", "orchestration", "repair", "review", "task")
    ], "enforce_review_family_separation": True}
    config = {"dataset": "real", "task_lock_sha256": lock["sha256"],
              "model": "fixture-model", "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
              "model_profile": profile, "model_profile_sha256": _sha256(profile),
              "model_routing": routing, "model_routing_sha256": _sha256(routing),
              "max_total_tokens": campaign_cap,
              "shared_budget_plan_sha256": BudgetPlan.formal().sha256,
              "max_tokens_per_run": per_run_cap, "tools": {"git": "git-fixture", "cmake": "cmake-fixture"},
              "tool_versions": {"git": "fixture", "cmake": "fixture"},
              "task_timeout": 900, "llm_timeout": 60,
              "generator": "Ninja", "approval": "manual", "rag": rag,
              "runtime_revision": hashlib.sha256(b"fixture runtime source").hexdigest(),
              "evaluator_revision": hashlib.sha256(b"fixture evaluator source").hexdigest(),
              "code_commit": "a" * 40, "code_dirty": False,
              "prompt_sha256": hashlib.sha256(b"fixture prompts").hexdigest(),
              "agent_definition_sha256": hashlib.sha256(b"fixture agents").hexdigest(),
              "task_spec_sha256": hashlib.sha256(b"fixture tasks").hexdigest(),
              "toolchain_sha256": hashlib.sha256(b"fixture toolchain").hexdigest(),
              "validation": "locked_public_ctest_and_protected_oracle", "repetitions": 3}
    digest = _sha256(config)
    ids = [task.id for task in tasks]
    kinds = {task.id: task.kind for task in tasks}
    paths = []
    for topology, stable, tokens in (("single", set(ids[:2] + ids[3:4]), 100),
                                     ("multi", set(ids[:4]), 100 * token_ratio)):
        rows = []
        for task in tasks:
            for index in (1, 2, 3):
                success = task.id in stable and index <= 2
                first = success and (topology == "multi" or index == 1)
                rows.append({"id": task.id, "category": task.kind, "run_index": index,
                             "config_hash": digest, "success": success,
                             "first_pass_success": first, "repair_success": success and not first,
                             "reason": "verified" if success else "oracle_failed", "state": "completed" if success else "failed",
                             "gates": {key: success for key in ("flow", "scope", "build", "ctest", "oracle", "integrity", "test_integrity")},
                             "build_status": "passed" if success else "failed",
                             "ctest_status": "passed" if success else "failed",
                             "oracle_status": "passed" if success else "failed",
                             "integrity_status": "passed", "scope_valid": success,
                             "test_status": "passed" if success else "failed",
                             "test_count": 2 if success else 0,
                             "safety_violations": safety if topology == "multi" and task.id == ids[0] and index == 1 else 0,
                             "tool_calls": 1, "total_tokens": int(tokens), "latency_ms": 10,
                             "agent_latency_ms": 5, "repair_rounds": 0 if first else 1})
        metadata = {"run_id": topology, "mode": "real", "evidence_label": "real",
                    "evaluation_type": "real_agent", "real_llm_results_produced": True,
                    "agent_topology": topology, "repetitions": 3,
                    "selected_tasks": ids, "task_categories": kinds,
                    "model": "fixture-model", "endpoint_sha256": config["endpoint_sha256"],
                    "model_profile_sha256": config["model_profile_sha256"],
                    "model_routing_sha256": config["model_routing_sha256"],
                    "code_commit": config["code_commit"], "code_dirty": config["code_dirty"],
                    "prompt_sha256": config["prompt_sha256"],
                    "agent_definition_sha256": config["agent_definition_sha256"],
                    "task_spec_sha256": config["task_spec_sha256"],
                    "toolchain_sha256": config["toolchain_sha256"],
                    "rag": rag, "approval_mode": "manual",
                    "started_at": "fixture", "finished_at": "fixture",
                    "config_hash": digest, "real_task_lock_sha256": lock["sha256"],
                    "comparison_configuration": config}
        directory = tmp_path / topology
        write_reports(directory, metadata, rows, Redactor(()))
        paths.append(directory / "results.json")
    return paths


def test_saved_pair_computes_advantage_and_all_thresholds(tmp_path):
    single, multi = make_pair(tmp_path)
    result = compare_saved(single, multi)
    assert result["verdict"] == "pass" and all(result["gates"].values())
    assert result["single"]["stable_tasks"] == 3 and result["multi"]["stable_tasks"] == 4
    assert result["newly_stable_task_ids"] == ["real-bug-03"]
    assert result["first_pass_delta_percentage_points"] > 20
    assert result["average_token_ratio"] == pytest.approx(2.4)
    assert result["multi"]["bug_stable_successes"] == 3
    assert result["multi"]["feature_stable_successes"] == 1


@pytest.mark.parametrize("ratio,safety,failed_gate", [(2.6, 0, "token_budget_ratio"),
    (2.4, 1, "zero_safety_violations")])
def test_saved_pair_reports_failed_threshold_without_selecting_runs(tmp_path, ratio, safety, failed_gate):
    result = compare_saved(*make_pair(tmp_path, token_ratio=ratio, safety=safety))
    assert result["verdict"] == "fail" and result["gates"][failed_gate] is False


@pytest.mark.parametrize("cap,per_run,pattern", [(3000, 10000, "campaign token budget exceeded"),
    (100000, 200, "token usage must be complete")])
def test_saved_pair_rejects_observed_token_budget_overrun(tmp_path, cap, per_run, pattern):
    with pytest.raises(ValueError, match=pattern):
        compare_saved(*make_pair(tmp_path, campaign_cap=cap, per_run_cap=per_run))


def test_rag_on_requires_pinned_embedding_and_index_configuration(tmp_path):
    with pytest.raises(ValueError, match="RAG configuration is not pinned"):
        compare_saved(*make_pair(tmp_path, rag="on"))


@pytest.mark.parametrize("mutation,pattern", [
    ("offline", "real Agent evidence"),
    ("config", "configuration or task-lock hash mismatch"),
    ("metric", "saved metrics disagree"),
    ("usage", "token usage must be complete"),
    ("gate", "lacks a required validation gate"),
    ("missing", "all fifteen saved run rows"),
    ("category", "same locked five real tasks"),
    ("first_pass", "first-pass or repair outcome"),
    ("repair_unknown", "requires a recorded repair count"),
    ("unfinished", "Unfinished evaluation"),
    ("test_integrity", "lacks a required validation gate"),
    ("status", "contradicts saved validation statuses"),
    ("tool_versions", "configuration is incomplete"),
    ("version_missing", "tool version is missing"),
    ("endpoint", "saved metadata contradicts"),
    ("routing", "complete model routing"),
    ("same_family", "families overlap"),
    ("different_main", "primary model differs"),
    ("embedded_secret", "unapproved fields"),
])
def test_saved_pair_rejects_unverifiable_evidence(tmp_path, mutation, pattern):
    single, multi = make_pair(tmp_path)
    document = json.loads(multi.read_text(encoding="utf-8"))
    if mutation == "offline":
        document["evidence_label"] = "offline"
    elif mutation == "config":
        document["metadata"]["config_hash"] = "0" * 64
    elif mutation == "metric":
        document["metrics"]["Task Success Rate"]["passed"] = 5
    elif mutation == "usage":
        document["tasks"][0]["total_tokens"] = None
    elif mutation == "gate":
        document["tasks"][0]["gates"]["oracle"] = False
    elif mutation == "missing":
        document["tasks"].pop()
    elif mutation == "category":
        document["metadata"]["task_categories"]["real-bug-01"] = "feature"
    elif mutation == "first_pass":
        document["tasks"][0]["repair_rounds"] = 1
    elif mutation == "repair_unknown":
        document["tasks"][0]["repair_rounds"] = None
    elif mutation == "unfinished":
        document["metadata"]["finished_at"] = None
    elif mutation == "test_integrity":
        document["tasks"][0]["gates"].pop("test_integrity")
    elif mutation == "status":
        document["tasks"][0]["oracle_status"] = "failed"
    elif mutation == "tool_versions":
        document["metadata"]["comparison_configuration"].pop("tool_versions")
    elif mutation == "version_missing":
        document["metadata"]["comparison_configuration"]["tool_versions"].pop("git")
    elif mutation == "endpoint":
        document["metadata"]["endpoint_sha256"] = hashlib.sha256(b"different endpoint").hexdigest()
    elif mutation == "routing":
        document["metadata"]["comparison_configuration"]["model_routing"]["routes"].pop()
    elif mutation in {"same_family", "different_main", "embedded_secret"}:
        routing = document["metadata"]["comparison_configuration"]["model_routing"]
        if mutation == "same_family":
            routing["profiles"][0]["family"] = "primary"
        elif mutation == "different_main":
            routing["routes"][0]["primary"] = "review"
        else:
            routing["profiles"][0]["api_key"] = "should-never-be-saved"
        document["metadata"]["comparison_configuration"]["model_routing_sha256"] = _sha256(routing)
        document["metadata"]["model_routing_sha256"] = _sha256(routing)
        document["metadata"]["config_hash"] = _sha256(document["metadata"]["comparison_configuration"])
        for row in document["tasks"]:
            row["config_hash"] = document["metadata"]["config_hash"]
    multi.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=pattern):
        compare_saved(single, multi)
