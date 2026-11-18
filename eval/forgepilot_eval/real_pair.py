"""Fail-closed, topology-only paired rehearsal for the five locked real tasks.

The caller supplies an offline fake gateway and the isolated Mission executor.
This module never constructs a paid provider or substitutes a failed sample.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from forgepilot.artifacts.canonical import content_sha256

from .manifest import DATA_ROOT, asset, verify_real_lock
from .real_adapter import assert_paired_arms, prepare_locked_arm, run_offline_locked_arm, validate_fair_gateway_routes


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               indent=2, allow_nan=False) + "\n", encoding="utf-8")


def pair_conditions(task, gateway, isolation, *, max_tokens_per_run=20000,
                    task_timeout=900, llm_timeout=60):
    """Capture all inputs that must stay equal while topology changes."""
    primary = validate_fair_gateway_routes(gateway.router.configuration)
    if not 1 <= max_tokens_per_run <= 1_000_000 or not 0 < task_timeout <= 7200:
        raise ValueError("Invalid shared paired budget or timeout")
    if not 0 < llm_timeout <= 600:
        raise ValueError("Invalid shared model timeout")
    routing = gateway.router.configuration
    profiles = sorted(({
        "profile_id": p.profile_id, "model": p.model, "family": p.family,
        "endpoint_sha256": hashlib.sha256(p.base_url.encode()).hexdigest(),
        "max_output_tokens": p.max_output_tokens,
        "timeout_seconds": p.timeout_seconds,
    } for p in routing.profiles), key=lambda p: p["profile_id"])
    tools = {name: {"path": str(getattr(isolation, name)),
                    "sha256": isolation.expected_sha256[name]}
             for name in ("launcher", "cmake", "ctest", "make", "compiler", "rc", "mt")}
    oracle = asset(DATA_ROOT, task.protected_oracle)
    return {
        "task_id": task.id, "base_commit": task.base_commit,
        "task_spec_sha256": content_sha256(task.to_artifact_task_spec()),
        "validation_sha256": content_sha256(task.task_spec.validation_commands),
        "oracle_sha256": hashlib.sha256(oracle.read_bytes()).hexdigest(),
        "model_profiles": profiles, "primary_profile_id": primary,
        "routing_sha256": content_sha256(routing.model_dump(mode="json")),
        "tools": tools, "max_tokens_per_run": max_tokens_per_run,
        "task_timeout": task_timeout, "llm_timeout": llm_timeout,
        "approval": "locked_manifest_exact_named_tests_then_plan_review",
        "generator": "NMake Makefiles", "oracle": task.protected_oracle,
    }


async def run_fake_pairs(output, *, gateway_factory, isolation, git="git", rg="rg",
                         max_tokens_per_run=20000, task_timeout=900, llm_timeout=60,
                         prepare=prepare_locked_arm, execute=run_offline_locked_arm):
    """Run every locked task in both arms and persist even failed/partial pairs.

    ``prepare`` and ``execute`` are injectable for deterministic harness tests;
    production uses pinned Git preparation and the isolated Mission adapter.
    """
    tasks = verify_real_lock()
    if len(tasks) != 5 or len({task.id for task in tasks}) != 5:
        raise ValueError("Real campaign requires exactly the five locked tasks")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    lock = json.loads(asset(DATA_ROOT, "real_tasks/lock.json").read_text(encoding="utf-8"))
    report = {"schema_version": 1, "evidence_label": "fake",
              "real_llm_results_produced": False, "paid_model_calls": 0,
              "task_lock_sha256": lock["sha256"],
              "started_at": datetime.now(timezone.utc).isoformat(), "pairs": []}
    _save(output / "pairs.json", report)
    try:
        for task in tasks:
            pair_dir = output / task.id
            pair_dir.mkdir()
            pair = {"task_id": task.id, "arms": {}}
            report["pairs"].append(pair)
            _save(output / "pairs.json", report)
            arms = {}
            try:
                for topology in ("single", "multi"):
                    checkout = pair_dir / topology / "checkout"
                    checkout.parent.mkdir()
                    arms[topology] = prepare(task.id, topology, checkout, git=git)
                identity = assert_paired_arms(arms["single"], arms["multi"])
                if identity["manifest_sha256"] != lock["sha256"]:
                    raise ValueError("Prepared task has a different manifest lock")
                pair["identity"] = identity
            except Exception as exc:
                pair["error"] = "preparation_error:" + type(exc).__name__ + ": " + str(exc)
                for topology in ("single", "multi"):
                    pair["arms"][topology] = {"conditions_sha256": None,
                        "result": {"id": task.id, "agent_topology": topology,
                                   "success": False, "reason": "preparation_failed"}}
                pair["conditions_equal"] = False
                _save(output / "pairs.json", report)
                continue
            _save(output / "pairs.json", report)
            condition_hash = None
            for topology in ("single", "multi"):
                arm_dir = pair_dir / topology / "evidence"
                mission_dir = arm_dir / "mission"
                gateway = None
                arm_hash = None
                try:
                    gateway = gateway_factory(task, topology)
                    provider = getattr(gateway, "_provider_factory", None)
                    if provider is None or not getattr(provider, "forgepilot_offline_fake", False):
                        raise ValueError("Paired rehearsal requires a marked fake provider")
                    conditions = pair_conditions(task, gateway, isolation,
                        max_tokens_per_run=max_tokens_per_run, task_timeout=task_timeout,
                        llm_timeout=llm_timeout)
                    digest = _digest(conditions)
                    arm_hash = digest
                    if condition_hash is not None and digest != condition_hash:
                        raise ValueError("Paired conditions differ between arms")
                    condition_hash = digest
                    arm_dir.mkdir()
                    _save(arm_dir / "conditions.json", conditions)
                    row = await execute(arms[topology], gateway=gateway,
                        isolation=isolation, output=mission_dir, git=git, rg=rg,
                        task_timeout=task_timeout, run_index=1, config_hash=digest)
                    pair["arms"][topology] = {"conditions_sha256": digest,
                        "result": row, "evidence_directory": str(arm_dir),
                        "mission_directory": str(mission_dir)}
                except Exception as exc:
                    pair["arms"][topology] = {"conditions_sha256": arm_hash,
                        "result": {"id": task.id, "agent_topology": topology,
                                   "success": False, "reason": "rehearsal_error:" + type(exc).__name__},
                        "evidence_directory": str(arm_dir),
                        "mission_directory": str(mission_dir)}
                    pair["error"] = type(exc).__name__ + ": " + str(exc)
                finally:
                    close = getattr(gateway, "aclose", None)
                    if close is not None:
                        await close()
                    _save(output / "pairs.json", report)
            pair["conditions_equal"] = (len(pair["arms"]) == 2 and
                pair["arms"]["single"]["conditions_sha256"] is not None and
                pair["arms"]["single"]["conditions_sha256"] ==
                pair["arms"]["multi"]["conditions_sha256"])
            _save(output / "pairs.json", report)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["complete"] = (len(report["pairs"]) == 5 and all(
            item.get("conditions_equal") and not item.get("error") and len(item["arms"]) == 2
            for item in report["pairs"]))
        _save(output / "pairs.json", report)
    return report
