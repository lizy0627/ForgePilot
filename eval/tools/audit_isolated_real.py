"""Run the locked real-task public validation and original Oracle in isolation.

The output directory is evaluator-owned and must be outside the candidate tree.
No paid model is invoked. A failed task remains failed in the audit record.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval"), str(ROOT / "eval/tools")]

from audit_isolated_micro import prepare_config  # noqa: E402
from forgepilot_eval.isolation import AppContainerDomain, assert_plain_tree  # noqa: E402
from forgepilot_eval.manifest import asset, check_real_test_integrity, verify_real_lock  # noqa: E402
from forgepilot.integrity import is_test_path  # noqa: E402
from forgepilot_eval.real_oracle_adapter import RealOracleAdapter  # noqa: E402


def git_state(repository):
    return subprocess.run(["git", "-C", str(repository), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


def source_inventory(repository):
    assert_plain_tree(repository)
    names = subprocess.run(["git", "-C", str(repository), "ls-files", "-z", "--cached",
                            "--others", "--exclude-standard"], capture_output=True, check=True).stdout
    return {name.decode("utf-8"): hashlib.sha256((repository / name.decode("utf-8")).read_bytes()).hexdigest()
            for name in names.split(b"\0") if name}


def junit(path, process_ok):
    if not path.is_file():
        return {"status": "failed", "reason": "missing_junit", "count": 0}
    try:
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
        valid = bool(cases) and all(
            case.find("failure") is None and case.find("error") is None
            and case.find("skipped") is None for case in cases)
        return {"status": "passed" if valid and process_ok else "failed",
                "reason": "executed" if valid and process_ok else "failed_skipped_or_empty",
                "count": len(cases)}
    except ET.ParseError:
        return {"status": "failed", "reason": "invalid_junit", "count": 0}


def ctest_names(raw):
    data = json.loads(raw)
    tests = data.get("tests")
    if not isinstance(tests, list) or not all(isinstance(item.get("name"), str) for item in tests):
        raise ValueError("CTest listing is invalid")
    return [item["name"] for item in tests]


async def run_one(task, candidate, output, config):
    result = {"id": task.id, "base_commit": task.base_commit,
              "paid_model_calls": 0, "public": "not_run", "oracle": "not_run"}
    output.mkdir(parents=True, exist_ok=False)
    build = output / "public-build"
    build.mkdir()
    before = source_inventory(candidate)
    baseline = candidate.parent / f"{task.id}-baseline"
    original = source_inventory(baseline)
    changes = {name for name in set(original) | set(before)
               if original.get(name) != before.get(name)}
    result["scope"] = {"changed": sorted(changes),
        "allowed": changes <= set(task.task_spec.allowed_files),
        "required": set(task.task_spec.required_changed) <= changes}
    # The placeholder only selects assertion/control-flow checks. CTest names
    # need a separate baseline/final listing and are not claimed here.
    result["test_structure_preserved"] = check_real_test_integrity(
        task, baseline, candidate, ["structural_preflight"], ["structural_preflight"])
    git_before = git_state(candidate)
    result["candidate_before_sha256"] = hashlib.sha256(
        json.dumps(before, sort_keys=True).encode()).hexdigest()
    result["candidate_git_before"] = git_before.splitlines()
    if subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"],
                      capture_output=True, text=True, check=True).stdout.strip() != task.base_commit:
        raise ValueError("Candidate is not at the locked base commit")
    config.validate_for(candidate, build)
    with AppContainerDomain(config.launcher) as domain:
        config.grant_read_only(domain)
        domain.grant(candidate, writable=True)
        domain.grant(build, writable=True)
        for index, original in enumerate(task.task_spec.validation_commands):
            phase = ("configure", "build", "ctest")[index]
            argv = [str(config.cmake if index < 2 else config.ctest)] + [
                str(build) if arg == "{build_dir}" else arg for arg in original[1:]]
            if index == 0:
                argv += config.cmake_options()
            if index == 2:
                argv += ["--no-tests=error", "--output-junit", str(build / "public.xml")]
            command = await domain.run(argv, candidate, timeout=300, env=config.env)
            (output / f"{phase}.stdout").write_text(command.stdout, encoding="utf-8")
            (output / f"{phase}.stderr").write_text(command.stderr, encoding="utf-8")
            result.setdefault("commands", []).append({"phase": phase, "argv": argv,
                "returncode": command.returncode, "status": command.status,
                "duration_ms": command.duration_ms})
            if command.status != "succeeded":
                result["public"] = "failed"
                break
        else:
            result["junit"] = junit(build / "public.xml", True)
            result["public"] = result["junit"]["status"]
        if result["public"] == "passed":
            listing = await domain.run([str(config.ctest), "--test-dir", str(build),
                "--show-only=json-v1"], candidate, timeout=60, env=config.env)
            (output / "final-ctest-list.stdout").write_text(listing.stdout, encoding="utf-8")
            result["final_ctest_names"] = ctest_names(listing.stdout) if listing.status == "succeeded" else []
    baseline_build = output / "baseline-ctest-build"
    baseline_build.mkdir()
    with AppContainerDomain(config.launcher) as domain:
        config.grant_read_only(domain)
        domain.grant(baseline, writable=False)
        domain.grant(baseline_build, writable=True)
        original_configure = task.task_spec.validation_commands[0]
        baseline_argv = [str(config.cmake)] + [str(baseline_build) if arg == "{build_dir}" else arg
            for arg in original_configure[1:]] + config.cmake_options()
        baseline_argv[baseline_argv.index("-S") + 1] = str(baseline)
        listed = await domain.run(baseline_argv, baseline_build, timeout=300, env=config.env)
        (output / "baseline-ctest-configure.stdout").write_text(listed.stdout, encoding="utf-8")
        (output / "baseline-ctest-configure.stderr").write_text(listed.stderr, encoding="utf-8")
        result["baseline_ctest_configure"] = {"argv": baseline_argv,
            "status": listed.status, "returncode": listed.returncode}
        if listed.status == "succeeded":
            names = await domain.run([str(config.ctest), "--test-dir", str(baseline_build),
                "--show-only=json-v1"], baseline_build, timeout=60, env=config.env)
            (output / "baseline-ctest-list.stdout").write_text(names.stdout, encoding="utf-8")
            result["baseline_ctest_names"] = ctest_names(names.stdout) if names.status == "succeeded" else []
    result["test_integrity_with_ctest_names"] = check_real_test_integrity(task, baseline,
        candidate, result.get("baseline_ctest_names", []), result.get("final_ctest_names", []))
    result["regression_test_sensitivity"] = False
    changed_tests = sorted(path for path in changes if is_test_path(path) and path in before)
    result["changed_test_files"] = changed_tests
    if changed_tests and result["public"] == "passed" and result["baseline_ctest_configure"]["status"] == "succeeded":
        mutation_source = output / "mutation-source"
        mutation_build = output / "mutation-build"
        shutil.copytree(baseline, mutation_source)
        for name in changed_tests:
            destination = mutation_source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate / name, destination)
        mutation_build.mkdir()
        with AppContainerDomain(config.launcher) as domain:
            config.grant_read_only(domain)
            domain.grant(mutation_source, writable=True)
            domain.grant(mutation_build, writable=True)
            mutation_argv = [str(config.cmake)] + [str(mutation_build) if arg == "{build_dir}" else arg
                for arg in original_configure[1:]] + config.cmake_options()
            mutation_argv[mutation_argv.index("-S") + 1] = str(mutation_source)
            for phase, argv in (("configure", mutation_argv),
                                ("build", [str(config.cmake), "--build", str(mutation_build), "--config", "Debug"]),
                                ("ctest", [str(config.ctest), "--test-dir", str(mutation_build), "-C", "Debug",
                                    "--output-on-failure", "--no-tests=error", "--output-junit",
                                    str(mutation_build / "mutation.xml")])):
                check = await domain.run(argv, mutation_source, timeout=300, env=config.env)
                (output / f"mutation-{phase}.stdout").write_text(check.stdout, encoding="utf-8")
                (output / f"mutation-{phase}.stderr").write_text(check.stderr, encoding="utf-8")
                result.setdefault("mutation_commands", []).append({"phase": phase, "argv": argv,
                    "status": check.status, "returncode": check.returncode})
                if check.status != "succeeded":
                    # A regression test that rejects the unmodified buggy
                    # implementation is necessary before scoring the repair.
                    result["regression_test_sensitivity"] = True
                    break
    assert_plain_tree(candidate)
    after = source_inventory(candidate)
    result["candidate_unchanged_after_public"] = before == after
    result["git_unchanged_after_public"] = git_before == git_state(candidate)
    if result["public"] == "passed" and result["candidate_unchanged_after_public"]:
        adapter = RealOracleAdapter(config, asset(ROOT / "eval", task.protected_oracle),
                                    candidate, timeout=300, evidence_dir=output / "oracle-evidence")
        result["oracle_result"] = await adapter.run()
        result["oracle"] = result["oracle_result"]["status"]
    result["candidate_unchanged_final"] = before == source_inventory(candidate)
    result["git_unchanged_final"] = git_before == git_state(candidate)
    result["passed"] = (result["public"] == "passed" and result["oracle"] == "passed"
                        and result["candidate_unchanged_final"] and result["git_unchanged_final"]
                        and result["scope"]["allowed"] and result["scope"]["required"]
                        and result["test_structure_preserved"]
                        and result["test_integrity_with_ctest_names"]
                        and result["regression_test_sensitivity"])
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def audit(args):
    tasks = {task.id: task for task in verify_real_lock()}
    config = prepare_config(args.output)
    for task_id in args.task or tasks:
        task = tasks[task_id]
        candidate = args.candidates / f"{task_id}-fixed"
        try:
            result = await run_one(task, candidate, args.output / task_id, config)
            print(task_id, result["public"], result["oracle"], result["passed"], flush=True)
        except Exception as exc:
            failure = {"id": task_id, "passed": False, "error": type(exc).__name__ + ": " + str(exc)}
            destination = args.output / task_id
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "result.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
            print(task_id, failure["error"], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    asyncio.run(audit(args))


if __name__ == "__main__":
    main()
