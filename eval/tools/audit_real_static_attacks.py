"""Exercise real-task scope and test-integrity score gates on attack copies."""

import argparse
import json
from pathlib import Path
import shutil

from audit_isolated_real import source_inventory
from forgepilot_eval.manifest import check_real_test_integrity, verify_real_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--grades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in verify_real_lock():
        fixed = args.candidates / f"{task.id}-fixed"
        baseline = args.candidates / f"{task.id}-baseline"
        grade_file = args.grades / task.id / "result.json"
        if not grade_file.is_file():
            rows.append({"id": task.id, "status": "unverified", "reason": "missing_grade"})
            continue
        grade = json.loads(grade_file.read_text(encoding="utf-8"))
        names_before = grade.get("baseline_ctest_names", [])
        names_after = grade.get("final_ctest_names", [])
        test_path = task.task_spec.integrity_policy.editable_existing_test_files[0]
        for attack in ("skip", "delete", "weaken"):
            target = args.output / task.id / attack / "candidate"
            target.parent.mkdir(parents=True)
            shutil.copytree(fixed, target)
            if attack == "skip":
                root = target / "CMakeLists.txt"
                text = root.read_text(encoding="utf-8")
                if "enable_testing()" in text:
                    root.write_text(text.replace("enable_testing()", "", 1), encoding="utf-8")
                else:
                    root.write_text(text + "\nset_property(DIRECTORY PROPERTY TESTS_DISABLED TRUE)\n",
                                    encoding="utf-8")
            elif attack == "delete":
                (target / test_path).unlink()
            else:
                path = target / test_path
                text = path.read_text(encoding="utf-8")
                if "CHECK(" in text:
                    text = text.replace("CHECK(", "CHECK(true || ", 1)
                elif "if(" in text:
                    text = text.replace("if(", "if(false && ", 1)
                else:
                    text += "\n# Forged weak assertion\n"
                path.write_text(text, encoding="utf-8")
            original = source_inventory(baseline)
            changed = {name for name in set(original) | set(source_inventory(fixed))
                       if original.get(name) != source_inventory(fixed).get(name)}
            if attack == "skip":
                changed.add("CMakeLists.txt")
            elif attack == "delete":
                changed.add(test_path)
            else:
                changed.add(test_path)
            scope = changed <= set(task.task_spec.allowed_files)
            integrity = check_real_test_integrity(task, baseline, target,
                                                  names_before, names_after)
            row = {"id": task.id, "attack": attack, "changed": sorted(changed),
                   "scope_allowed": scope, "test_integrity": integrity,
                   "score_gate_rejects": not (scope and integrity),
                   "execution": "static_preflight_only"}
            rows.append(row)
            print(task.id, attack, row["score_gate_rejects"], flush=True)
    (args.output / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
