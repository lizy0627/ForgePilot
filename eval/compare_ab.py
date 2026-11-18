"""Read-only comparison of two saved real benchmark result files."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from forgepilot_eval.ab import main


if __name__ == "__main__":
    raise SystemExit(main())
