"""Recheck a saved Agent result with the protected analysis oracle, without an LLM."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from forgepilot_eval.grader import grade_analysis
from forgepilot_eval.manifest import load_tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify analysis-01 agent-result.json without executing Agent code")
    parser.add_argument("result", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.result.stat().st_size > 4_194_304:
            raise ValueError("oversized result")
        saved = json.loads(args.result.read_text(encoding="utf-8"))
        expected = next(t.success.expected for t in load_tasks() if t.id == "analysis-01")
        passed = (isinstance(saved, dict) and saved.get("state") == "completed"
                  and grade_analysis(saved.get("final_result"), expected))
    except (OSError, ValueError, TypeError):
        passed = False
    print(json.dumps({"task": "analysis-01", "passed": passed}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
