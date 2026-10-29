"""Gate measured statement+branch coverage for the mission core.

The lists are deliberately explicit. Adding a module to a package never silently
changes the denominator, and missing coverage data is an error. Permission checks
also have an independent branch gate; coverage remains a signal, not a test goal.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


GROUPS = {
    "orchestration": (
        "forgepilot/orchestration/analysis_review.py",
        "forgepilot/orchestration/checkpoint.py",
        "forgepilot/orchestration/config.py",
        "forgepilot/orchestration/graph.py",
        "forgepilot/orchestration/models.py",
        "forgepilot/orchestration/node_handlers.py",
        "forgepilot/orchestration/policies.py",
        "forgepilot/orchestration/recovery.py",
        "forgepilot/orchestration/runner.py",
        "forgepilot/orchestration/store.py",
        "forgepilot/orchestration/workspace.py",
    ),
    "security": (
        "forgepilot/file_access.py",
        "forgepilot/integrity.py",
        "forgepilot/path_identity.py",
        "forgepilot/policy.py",
        "forgepilot/security.py",
        "forgepilot/workspace_safety.py",
    ),
    "artifacts": (
        "forgepilot/artifacts/canonical.py",
        "forgepilot/artifacts/models.py",
        "forgepilot/artifacts/schemas.py",
        "forgepilot/artifacts/sqlite_store.py",
        "forgepilot/artifacts/store.py",
        "forgepilot/artifacts/validation.py",
    ),
}
PERMISSION_BRANCHES = (
    "forgepilot/file_access.py",
    "forgepilot/path_identity.py",
    "forgepilot/policy.py",
)
CORE_MINIMUM = 85.0
PERMISSION_BRANCH_MINIMUM = 95.0


def combined(files: dict, names: tuple[str, ...]) -> tuple[float, float]:
    statement_total = statement_covered = branch_total = branch_covered = 0
    for name in names:
        if name not in files:
            raise ValueError(f"Missing coverage entry: {name}")
        data = files[name]["summary"]
        statement_total += data["num_statements"]
        statement_covered += data["covered_lines"]
        branch_total += data["num_branches"]
        branch_covered += data["covered_branches"]
    combined_total = statement_total + branch_total
    if combined_total == 0 or branch_total == 0:
        raise ValueError("Coverage data lacks statements or branch measurements")
    return (100 * (statement_covered + branch_covered) / combined_total,
            100 * branch_covered / branch_total)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_core_coverage.py COVERAGE_JSON", file=sys.stderr)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not data.get("meta", {}).get("branch_coverage"):
        raise ValueError("Branch coverage was not enabled")
    files = {name.replace("\\", "/"): value for name, value in data["files"].items()}
    failed = False
    for group, names in GROUPS.items():
        percent, branches = combined(files, names)
        print(f"{group}: combined={percent:.2f}% branches={branches:.2f}% files={len(names)} target={CORE_MINIMUM:.0f}%")
        failed |= percent < CORE_MINIMUM
    _, permission_branches = combined(files, PERMISSION_BRANCHES)
    print(f"permission-critical branches: {permission_branches:.2f}% files={len(PERMISSION_BRANCHES)} "
          f"target={PERMISSION_BRANCH_MINIMUM:.0f}%")
    failed |= permission_branches < PERMISSION_BRANCH_MINIMUM
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
