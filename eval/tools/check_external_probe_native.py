"""Offline native smoke for one process-external behavioral contract.

This uses the host toolchain to check C++ compatibility only. It is not an
AppContainer isolation or formal-score test.
"""

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forgepilot_eval.external_probe import make_challenge, verify_response, write_probe_project


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--include", required=True, type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--generator", default="Visual Studio 17 2022")
    parser.add_argument("--qt-kit", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="forgepilot-probe-smoke-") as name:
        root = Path(name)
        source = write_probe_project(args.task, root / "source",
            candidate_include=args.include.resolve(strict=True),
            candidate_library=args.library.resolve(strict=True) if args.library else None,
            candidate_source=args.source.resolve(strict=True) if args.source else None)
        build = root / "build"
        configure = [args.cmake, "-S", str(source), "-B", str(build),
                     "-G", args.generator, "-A", "x64"]
        if args.qt_kit:
            configure.append("-DQt6_DIR=" + str(args.qt_kit / "lib/cmake/Qt6"))
        for command in (configure, [args.cmake, "--build", str(build), "--config", "Debug"]):
            result = subprocess.run(command, cwd=root, capture_output=True, timeout=180, check=False)
            if result.returncode:
                print((result.stdout + result.stderr)[-3000:].decode(errors="replace"), file=sys.stderr)
                return 1
        matches = list(build.rglob("probe.exe"))
        if len(matches) != 1:
            return 2
        challenge = make_challenge(args.task, seed=73)
        result = subprocess.run([str(matches[0])], input=challenge.stdin,
                                cwd=build, capture_output=True, timeout=15, check=False)
        valid = verify_response(challenge, returncode=result.returncode, stdout=result.stdout)
        print("passed" if valid else "failed")
        return 0 if valid else 3


if __name__ == "__main__":
    raise SystemExit(main())
