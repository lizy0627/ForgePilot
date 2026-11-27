"""Run with the existing agent.venv; no package installation or second environment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from forgepilot_eval.runner import main

if __name__ == "__main__":
    if sys.argv[1:] == ["--saved-launcher-settings"]:
        from forgepilot.benchmark_launcher import run_saved_benchmark
        raise SystemExit(run_saved_benchmark(main))
    raise SystemExit(main())
