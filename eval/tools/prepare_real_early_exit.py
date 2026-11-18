"""Prepare evaluator-only attack copies without changing the locked candidates."""

import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[2] / "agent")]

from forgepilot_eval.manifest import verify_real_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in verify_real_lock():
        for variant in ("baseline", "fixed"):
            shutil.copytree(args.candidates / f"{task.id}-{variant}",
                            args.output / f"{task.id}-{variant}")
        candidate = args.output / f"{task.id}-fixed"
        if task.id == "real-bug-02":
            target = candidate / "cmake/cxxopts.cmake"
            target.write_text("return()\n" + target.read_text(encoding="utf-8"), encoding="utf-8")
            attack = "cmake_early_return"
        else:
            name = "src/pugixml.hpp" if task.id == "real-feature-02" else "include/cxxopts.hpp"
            target = candidate / name
            target.write_text(target.read_text(encoding="utf-8") +
                "\n#include <cstdlib>\nnamespace oracle_exit_attack {"
                "struct Exit { Exit() { std::_Exit(0); } }; static Exit trigger; }\n",
                encoding="utf-8")
            attack = "pre_main_exit_zero"
        rows.append({"id": task.id, "attack": attack, "target": str(target)})
    (args.output / "manifest.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
