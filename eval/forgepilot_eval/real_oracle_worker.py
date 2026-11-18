"""Killable, trusted worker for locked Python Oracle orchestration."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval")]

from forgepilot_eval.isolation import IsolationConfig  # noqa: E402
from forgepilot_eval.real_oracle_adapter import RealOracleAdapter  # noqa: E402
from forgepilot.build_process import build_environment  # noqa: E402


def main():
    try:
        config_data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        config = IsolationConfig(
            launcher=Path(config_data["launcher"]), cmake=Path(config_data["cmake"]),
            ctest=Path(config_data["ctest"]), make=Path(config_data["make"]),
            compiler=Path(config_data["compiler"]), rc=Path(config_data["rc"]),
            mt=Path(config_data["mt"]), env=build_environment(),
            qt_kit=Path(config_data["qt_kit"]) if config_data["qt_kit"] else None,
            expected_sha256=config_data["expected_sha256"],
            read_only_roots=tuple(Path(p) for p in config_data["read_only_roots"]))
        adapter = RealOracleAdapter(config, Path(sys.argv[2]), Path(sys.argv[3]),
                                    timeout=float(sys.argv[4]),
                                    evidence_dir=Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None)
        result = adapter._run_sync()
    except Exception as exc:
        detail = str(exc).casefold()
        category = ("reparse_point" if "reparse point" in detail else
                    "acl_grant" if "acl grant" in detail else
                    "profile_create" if "profile creation" in detail else
                    "command_shape" if "command" in detail else "other")
        result = {"status": "failed", "reason": "execution_error", "commands": [],
                  "diagnostic_type": type(exc).__name__, "diagnostic_category": category,
                  "diagnostic_phase": adapter.phase if "adapter" in locals() else "before_adapter"}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
