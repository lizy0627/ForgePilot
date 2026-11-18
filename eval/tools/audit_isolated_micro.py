"""Offline evaluator audit of locked micro tasks in AppContainer domains.

Run from a configured MSVC developer shell. Candidate repositories are copies;
the fixed answers and protected assets stay in the evaluator process.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "eval")]

from forgepilot.build_process import build_environment  # noqa: E402
from forgepilot_eval.grader import Grader  # noqa: E402
from forgepilot_eval.isolation import IsolationConfig  # noqa: E402
from forgepilot_eval.manifest import DATA_ROOT, inventory, load_tasks  # noqa: E402


def command(argv, log):
    import subprocess
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=240, check=False)
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Tool setup failed ({result.returncode}): {log}")


def prepare_config(output):
    required = {name: shutil.which(name) for name in ("cmake", "ctest", "cl", "nmake", "rc", "mt")}
    if not all(required.values()):
        raise RuntimeError("MSVC developer shell is required")
    build = output / "launcher-build"
    build.mkdir()
    command([required["cmake"], "-S", str(ROOT / "eval/tools/appcontainer_launcher"),
             "-B", str(build), "-G", "Visual Studio 17 2022", "-A", "x64"], output / "launcher-configure.log")
    command([required["cmake"], "--build", str(build), "--config", "Debug"], output / "launcher-build.log")
    tools = output / "toolchain-copy"
    tools.mkdir()
    rc = Path(required["rc"])
    mt = Path(required["mt"])
    shutil.copy2(rc, tools / "rc.exe")
    shutil.copy2(mt, tools / "mt.exe")
    for name in ("rcdll.dll", "midlrtmd.dll"):
        shutil.copy2(rc.with_name(name), tools / name)
    sdk = rc.parents[3] / "Lib" / rc.parents[1].name
    for name in ("kernel32", "user32", "gdi32", "winspool", "shell32", "ole32",
                 "oleaut32", "uuid", "comdlg32", "advapi32", "ws2_32", "mpr", "userenv"):
        shutil.copy2(sdk / "um/x64" / (name + ".lib"), tools / (name + ".lib"))
    for name in ("ucrt.lib", "ucrtd.lib", "libucrt.lib", "libucrtd.lib"):
        shutil.copy2(sdk / "ucrt/x64" / name, tools / name)
    for name in ("ucrt", "shared", "um"):
        shutil.copytree(sdk.parents[1] / "Include" / sdk.name / name, tools / name)
    env = build_environment()
    env["PATH"] = str(tools) + ";" + env["PATH"]
    env["LIB"] = str(tools) + ";" + env.get("LIB", "")
    env["INCLUDE"] = ";".join(str(tools / name) for name in ("ucrt", "shared", "um")) + ";" + env.get("INCLUDE", "")
    pinned = {"launcher": build / "Debug/appcontainer_launcher.exe",
              "cmake": Path(required["cmake"]), "ctest": Path(required["ctest"]),
              "make": Path(required["nmake"]), "compiler": Path(required["cl"]),
              "rc": tools / "rc.exe", "mt": tools / "mt.exe"}
    config = IsolationConfig(**pinned, env=env,
        qt_kit=Path("D:/Qt/6.8.3/msvc2022_64"),
        read_only_roots=(Path(required["cmake"]).parent.parent, tools),
        expected_sha256={name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for name, path in pinned.items()})
    return config


async def audit(output, selected, timeout):
    config = prepare_config(output)
    grader = Grader(DATA_ROOT, str(config.cmake), str(config.ctest), timeout=timeout, isolation=config)
    solutions = json.loads((DATA_ROOT / "offline-solutions.json").read_text(encoding="utf-8"))["solutions"]
    dataset_before = inventory(DATA_ROOT)
    for task in load_tasks():
        if task.analysis or (selected and task.id not in selected):
            continue
        task_dir = output / task.id
        task_dir.mkdir()
        record = {"id": task.id, "paid_model_calls": 0, "baseline": "not_run", "fixed": "not_run"}
        try:
            for variant in ("baseline", "fixed"):
                repo = task_dir / variant / "candidate"
                repo.parent.mkdir()
                shutil.copytree(DATA_ROOT / task.fixture, repo)
                if variant == "fixed":
                    for name, content in solutions[task.id]["files"].items():
                        (repo / name).write_text(content, encoding="utf-8")
                before = inventory(repo)
                grade = await grader.run_isolated(task, repo, task_dir / variant / "grade")
                after = inventory(repo, ignore_generated=True)
                record[variant] = {"grade": grade, "candidate_unchanged": before == after,
                    "candidate": str(repo), "grade_directory": str(task_dir / variant / "grade")}
                (task_dir / f"{variant}.json").write_text(json.dumps(record[variant], indent=2), encoding="utf-8")
            record["baseline_failed"] = record["baseline"]["grade"]["test_status"] != "passed"
            fixed = record["fixed"]["grade"]
            record["fixed_passed"] = (fixed["test_status"] == "passed" and fixed["oracle_status"] == "passed"
                and fixed["public_test_count"] > 0 and fixed["oracle_test_count"] > 0)
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
        record["protected_unchanged"] = inventory(DATA_ROOT) == dataset_before
        (task_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(task.id, "baseline", record.get("baseline_failed"), "fixed", record.get("fixed_passed"),
              "error", record.get("error"), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", action="append")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    asyncio.run(audit(output, set(args.task or ()), args.timeout))


if __name__ == "__main__":
    main()
