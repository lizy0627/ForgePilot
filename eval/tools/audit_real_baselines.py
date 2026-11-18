"""Collect isolated public and protected baseline evidence for locked real tasks."""

import argparse
import asyncio
import json
from pathlib import Path

from audit_isolated_real import prepare_config, run_one, verify_real_lock


async def audit(root, output):
    config = prepare_config(output)
    for task in verify_real_lock():
        result = await run_one(task, root / f"{task.id}-baseline", output / task.id, config)
        baseline_failed = result["public"] == "failed" or result["oracle"] == "failed"
        (output / task.id / "baseline-summary.json").write_text(json.dumps({
            "id": task.id, "baseline_failed": baseline_failed,
            "public": result["public"], "oracle": result["oracle"],
            "public_ctest_executed": result.get("junit", {}).get("count", 0) > 0,
        }, indent=2), encoding="utf-8")
        print(task.id, result["public"], result["oracle"], baseline_failed, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    asyncio.run(audit(args.candidates, args.output))


if __name__ == "__main__":
    main()
