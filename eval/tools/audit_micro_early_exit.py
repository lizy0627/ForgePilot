"""Offline evaluator audit: candidate static initializer exits before assertions."""

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval"), str(ROOT / "eval/tools")]

from audit_isolated_micro import prepare_config  # noqa: E402
from forgepilot_eval.grader import Grader, check_scope  # noqa: E402
from forgepilot_eval.manifest import DATA_ROOT, inventory, load_tasks  # noqa: E402


async def audit(output, selected):
    config = prepare_config(output)
    grader = Grader(DATA_ROOT, str(config.cmake), str(config.ctest), isolation=config)
    solutions = json.loads((DATA_ROOT / "offline-solutions.json").read_text(encoding="utf-8"))["solutions"]
    for task in load_tasks():
        if task.analysis or task.qt or "src/subject.cpp" not in task.allowed_files:
            continue
        if selected and task.id not in selected:
            continue
        directory = output / task.id
        candidate = directory / "candidate"
        directory.mkdir()
        shutil.copytree(DATA_ROOT / task.fixture, candidate)
        before = inventory(candidate)
        for name, content in solutions[task.id]["files"].items():
            (candidate / name).write_text(content, encoding="utf-8")
        subject = candidate / "src/subject.cpp"
        subject.write_text('#include <cstdlib>\nnamespace { struct ExitEarly { ExitEarly() { std::_Exit(0); } } exit_early; }\n'
                           + subject.read_text(encoding="utf-8"), encoding="utf-8")
        scope = check_scope(before, inventory(candidate), task)
        attack_snapshot = inventory(candidate, ignore_generated=True)
        try:
            grade = await grader.run_isolated(task, candidate, directory / "grade")
            result = {"id": task.id, "attack": "static_initializer_exit_zero", "scope": scope,
                      "grade": grade, "candidate_unchanged": inventory(candidate, ignore_generated=True) == attack_snapshot,
                      "early_exit_gate_proven": grade["public_test_status"] == "passed"
                          and grade["oracle_status"] != "passed",
                      "rejected": not (scope["valid"] and grade["public_test_status"] == "passed"
                          and grade["oracle_status"] == "passed" and grade["oracle_test_count"] > 0)}
        except Exception as exc:
            result = {"id": task.id, "attack": "static_initializer_exit_zero", "scope": scope,
                      "rejected": False, "status": "unverified", "error": type(exc).__name__ + ": " + str(exc)}
        (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(task.id, result["rejected"], result.get("grade", {}).get("test_reason"),
              result.get("error"), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    asyncio.run(audit(args.output, set(args.task or ())))


if __name__ == "__main__":
    main()
