"""Read-only A/B comparison of two complete, locked real benchmark reports."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

from .manifest import DATA_ROOT, verify_real_lock
from .metrics import summarize, task_results


REQUIRED_GATES = ("flow", "scope", "build", "ctest", "oracle", "integrity", "test_integrity")
REQUIRED_LOCK_FIELDS = ("dataset", "task_lock_sha256", "model", "endpoint_sha256",
                        "model_profile", "model_profile_sha256",
                        "max_total_tokens", "max_tokens_per_run", "shared_budget_plan_sha256",
                        "tools", "tool_versions",
                        "task_timeout", "llm_timeout", "generator", "approval", "rag",
                        "runtime_revision", "evaluator_revision", "validation", "repetitions",
                        "code_commit", "code_dirty", "prompt_sha256", "agent_definition_sha256",
                        "task_spec_sha256", "toolchain_sha256")


def _sha256(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load(path, topology, tasks, lock_digest):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or document.get("schema_version") != 2:
        raise ValueError("A/B input is not a versioned benchmark report")
    if (document.get("evidence_label") != "real" or metadata.get("evidence_label") != "real"
            or metadata.get("mode") != "real" or metadata.get("evaluation_type") != "real_agent"
            or metadata.get("real_llm_results_produced") is not True):
        raise ValueError("A/B comparison requires real Agent evidence on both sides")
    if metadata.get("agent_topology") != topology:
        raise ValueError("A/B pair requires single and multi topologies")
    if metadata.get("stopped_reason"):
        raise ValueError("Interrupted evaluation cannot establish an A/B verdict")
    if not isinstance(metadata.get("finished_at"), str) or not metadata["finished_at"]:
        raise ValueError("Unfinished evaluation cannot establish an A/B verdict")
    selected = [task.id for task in tasks]
    categories = {task.id: task.kind for task in tasks}
    if (metadata.get("repetitions") != 3 or metadata.get("selected_tasks") != selected
            or metadata.get("task_categories") != categories):
        raise ValueError("A/B inputs must contain the same locked five real tasks, three runs each")
    config = metadata.get("comparison_configuration")
    if not isinstance(config, dict) or not all(key in config for key in REQUIRED_LOCK_FIELDS):
        raise ValueError("A/B comparison configuration is incomplete")
    if (config["dataset"] != "real" or config["task_lock_sha256"] != lock_digest
            or config["repetitions"] != 3 or "agent_topology" in config
            or not isinstance(config["model"], str) or not config["model"]
            or not isinstance(config["tools"], dict) or not config["tools"]
            or not isinstance(config["tool_versions"], dict) or not config["tool_versions"]
            or not isinstance(config["generator"], str) or not config["generator"]
            or not isinstance(config["approval"], str) or not config["approval"]
            or config["rag"] not in {"on", "off"} or not config["validation"]):
        raise ValueError("A/B configuration does not bind the locked real dataset")
    if any(not config["tool_versions"].get(name) for name, executable in config["tools"].items() if executable):
        raise ValueError("A/B tool version is missing")
    profile = config["model_profile"]
    if (not isinstance(profile, dict) or _sha256(profile) != config["model_profile_sha256"]
            or profile.get("model") != config["model"]
            or profile.get("endpoint_sha256") != config["endpoint_sha256"]
            or not isinstance(profile.get("credential_ref"), str)
            or re.fullmatch(r"(?:env:[A-Z][A-Z0-9_]{0,127}|dpapi:[a-z][a-z0-9._-]{0,127})",
                            profile["credential_ref"]) is None):
        raise ValueError("A/B model profile does not bind the selected provider")
    if config["rag"] == "on":
        rag = config.get("rag_configuration")
        if (not isinstance(rag, dict)
                or not isinstance(rag.get("embedding_model"), str) or not rag["embedding_model"]
                or any(not isinstance(rag.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", rag[key]) is None
                       for key in ("embedding_endpoint_sha256", "index_configuration_sha256"))):
            raise ValueError("A/B RAG configuration is not pinned")
    for key in ("endpoint_sha256", "model_profile_sha256", "shared_budget_plan_sha256",
                "runtime_revision", "evaluator_revision", "prompt_sha256",
                "agent_definition_sha256", "task_spec_sha256", "toolchain_sha256"):
        if not isinstance(config[key], str) or re.fullmatch(r"[0-9a-f]{64}", config[key]) is None:
            raise ValueError("A/B provider or implementation revision is missing")
    if (not isinstance(config["code_commit"], str)
            or re.fullmatch(r"[0-9a-f]{40}", config["code_commit"]) is None
            or config["code_dirty"] is not False):
        raise ValueError("A/B source commit must be clean and pinned")
    for key in ("max_total_tokens", "max_tokens_per_run"):
        value = config[key]
        if type(value) is not int or value <= 0:
            raise ValueError("A/B model budget or timeout is invalid")
    for key in ("task_timeout", "llm_timeout"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("A/B model budget or timeout is invalid")
    digest = _sha256(config)
    if metadata.get("config_hash") != digest or metadata.get("real_task_lock_sha256") != lock_digest:
        raise ValueError("A/B configuration or task-lock hash mismatch")
    if (metadata.get("model") != config["model"] or metadata.get("rag") != config["rag"]
            or metadata.get("approval_mode") != config["approval"]
            or metadata.get("endpoint_sha256") != config["endpoint_sha256"]
            or metadata.get("model_profile_sha256") != config["model_profile_sha256"]
            or metadata.get("code_commit") != config["code_commit"]
            or metadata.get("code_dirty") != config["code_dirty"]
            or any(metadata.get(key) != config[key] for key in (
                "prompt_sha256", "agent_definition_sha256", "task_spec_sha256", "toolchain_sha256"))):
        raise ValueError("A/B saved metadata contradicts the locked configuration")
    rows = document.get("tasks")
    if not isinstance(rows, list) or len(rows) != 15:
        raise ValueError("A/B evidence requires all fifteen saved run rows")
    for row in rows:
        if not isinstance(row, dict) or row.get("config_hash") != digest:
            raise ValueError("A/B run is not bound to the comparison configuration")
        if row.get("reason") in {"not_run", "missing_result", "cancelled"} or row.get("state") == "not_run":
            raise ValueError("Incomplete real run cannot establish an A/B verdict")
        if type(row.get("success")) is not bool or type(row.get("first_pass_success")) is not bool:
            raise ValueError("A/B run success fields must be explicit booleans")
        if row["first_pass_success"] and not row["success"]:
            raise ValueError("A/B first-pass success contradicts final outcome")
        rounds = row.get("repair_rounds")
        if rounds is not None and (type(rounds) is not int or rounds < 0):
            raise ValueError("A/B repair count is invalid")
        if row["success"] and rounds is None:
            raise ValueError("A/B successful run requires a recorded repair count")
        if (type(row.get("repair_success")) is not bool
                or row["first_pass_success"] != (row["success"] and rounds == 0)
                or row["repair_success"] != (row["success"] and rounds is not None and rounds > 0)):
            raise ValueError("A/B first-pass or repair outcome contradicts repair count")
        if type(row.get("total_tokens")) is not int or not 0 <= row["total_tokens"] <= config["max_tokens_per_run"]:
            raise ValueError("A/B token usage must be complete on every run")
        if type(row.get("safety_violations")) is not int or row["safety_violations"] < 0:
            raise ValueError("A/B safety violations must be explicit on every run")
        if row["success"]:
            gates = row.get("gates")
            if not isinstance(gates, dict) or not all(gates.get(key) is True for key in REQUIRED_GATES):
                raise ValueError("A/B successful run lacks a required validation gate")
            if (row.get("scope_valid") is not True or row.get("build_status") != "passed"
                    or row.get("ctest_status") != "passed" or row.get("oracle_status") != "passed"
                    or row.get("integrity_status") != "passed" or row.get("test_status") != "passed"
                    or type(row.get("test_count")) is not int or row["test_count"] <= 0):
                raise ValueError("A/B successful run contradicts saved validation statuses")
    if sum(row["total_tokens"] for row in rows) > config["max_total_tokens"]:
        raise ValueError("A/B campaign token budget exceeded")
    calculated_tasks = task_results(rows, expected_repetitions=3, selected_tasks=selected, categories=categories)
    calculated_metrics = summarize(rows, expected_repetitions=3, selected_tasks=selected, categories=categories)
    if document.get("task_results") != calculated_tasks or document.get("metrics") != calculated_metrics:
        raise ValueError("A/B saved metrics disagree with the raw runs")
    return {"metadata": metadata, "rows": rows, "tasks": calculated_tasks, "metrics": calculated_metrics,
            "config": config, "hash": digest}


def _arm(value):
    results = value["tasks"]
    rows = value["rows"]
    passed = [task for task in results if task["stable_success"]]
    bugs = sum(task["category"] == "bug" for task in passed)
    features = sum(task["category"] == "feature" for task in passed)
    return {"stable_tasks": len(passed), "task_success_rate": len(passed) / 5,
            "successful_runs": sum(row["success"] for row in rows),
            "run_success_rate": sum(row["success"] for row in rows) / 15,
            "first_pass_successes": sum(row["first_pass_success"] for row in rows),
            "first_pass_rate": sum(row["first_pass_success"] for row in rows) / 15,
            "total_tokens": sum(row["total_tokens"] for row in rows),
            "average_tokens": sum(row["total_tokens"] for row in rows) / 15,
            "safety_violations": sum(row["safety_violations"] for row in rows),
            "bug_stable_successes": bugs, "feature_stable_successes": features,
            "task_threshold_met": len(passed) >= 4 and bugs >= 1 and features >= 1}


def compare_saved(single_path, multi_path, *, root=DATA_ROOT):
    """Compare saved evidence only; rejects incomplete or unverifiable pairs."""
    tasks = verify_real_lock(root)
    lock = json.loads((Path(root) / "real_tasks" / "lock.json").read_text(encoding="utf-8"))
    lock_digest = lock["sha256"]
    single = _load(single_path, "single", tasks, lock_digest)
    multi = _load(multi_path, "multi", tasks, lock_digest)
    if single["hash"] != multi["hash"] or single["config"] != multi["config"]:
        raise ValueError("A/B configuration hash mismatch")
    baseline, candidate = _arm(single), _arm(multi)
    baseline_pass = {task["id"]: task["stable_success"] for task in single["tasks"]}
    newly_passed = [task["id"] for task in multi["tasks"]
                    if task["stable_success"] and not baseline_pass[task["id"]]]
    delta_count = candidate["first_pass_successes"] - baseline["first_pass_successes"]
    delta_pp = delta_count * 100 / 15
    if baseline["average_tokens"] <= 0:
        raise ValueError("A/B baseline has no measurable token use")
    token_ratio = candidate["average_tokens"] / baseline["average_tokens"]
    gates = {"same_locked_configuration": True,
             "multi_task_threshold": candidate["task_threshold_met"],
             "bug_and_feature": candidate["bug_stable_successes"] >= 1 and candidate["feature_stable_successes"] >= 1,
             "advantage": bool(newly_passed) or delta_count * 100 >= 20 * 15,
             "token_budget_ratio": candidate["total_tokens"] * 2 <= baseline["total_tokens"] * 5,
             "zero_safety_violations": baseline["safety_violations"] == candidate["safety_violations"] == 0}
    return {"evidence_label": "real", "config_hash": single["hash"],
            "real_task_lock_sha256": lock_digest,
            "single": baseline, "multi": candidate,
            "newly_stable_task_ids": newly_passed,
            "first_pass_delta_percentage_points": delta_pp,
            "average_token_ratio": token_ratio,
            "gates": gates, "verdict": "pass" if all(gates.values()) else "fail"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare two saved, locked real benchmark reports")
    parser.add_argument("single", type=Path)
    parser.add_argument("multi", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare_saved(args.single, args.multi)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"A/B comparison unavailable: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
