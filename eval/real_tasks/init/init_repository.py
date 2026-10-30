"""Prepare one pinned public source checkout outside ForgePilot and without future history.

This script is an evaluator-side asset. Only the resulting shallow checkout is handed
to an Agent. No answer, patch, or protected test is copied into that checkout.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
sys.path.insert(0, str(PROJECT_ROOT / "eval"))

from forgepilot_eval.manifest import verify_real_lock  # noqa: E402


def prepare(task_id: str, destination: Path, *, git: str = "git") -> Path:
    tasks = {task.id: task for task in verify_real_lock()}
    if task_id not in tasks:
        raise ValueError("Unknown locked task")
    task = tasks[task_id]
    if destination.name in {"", ".", ".."}:
        raise ValueError("Invalid candidate destination")
    parent = destination.absolute().parent.resolve(strict=True)
    destination = parent / destination.name
    if destination.exists() or not parent.is_dir():
        raise ValueError("Destination must be a new path under an existing directory")
    project = PROJECT_ROOT.resolve(strict=True)
    if destination.is_relative_to(project) or project.is_relative_to(destination):
        raise ValueError("Candidate checkout must be outside ForgePilot")
    git_path = shutil.which(git)
    if not git_path:
        raise ValueError("Git executable unavailable")

    def run(*args):
        process = subprocess.run([git_path, *args], cwd=destination.parent, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
        if process.returncode:
            # Do not expose remote output or paths from an untrusted checkout.
            raise RuntimeError("Pinned source preparation failed")
        return process.stdout.strip()

    # Fetch only the base commit. A clone of HEAD would place the official fix
    # inside .git even if checkout later moved back to the base.
    try:
        run("init", "-q", str(destination))
        # The assertion permissions pin source bytes at the Git base commit.
        # Disable host-specific CRLF conversion before the first checkout.
        run("-C", str(destination), "config", "core.autocrlf", "false")
        run("-C", str(destination), "remote", "add", "origin", task.repository_url + ".git")
        run("-C", str(destination), "fetch", "-q", "--no-tags", "--depth=1", "origin", task.base_commit)
        run("-C", str(destination), "checkout", "-q", "--detach", "FETCH_HEAD")
        run("-C", str(destination), "remote", "remove", "origin")
        if run("-C", str(destination), "rev-parse", "HEAD") != task.base_commit:
            raise RuntimeError("Fetched commit differs from lock")
        if not (destination / task.license_file).is_file():
            raise RuntimeError("Pinned source is missing its declared license file")
    except Exception:
        # Preserve a failed partial checkout for explicit inspection; never hand it
        # to the Agent or silently treat it as a prepared task.
        if destination.is_dir():
            (destination / ".forgepilot-preparation-failed").write_text("incomplete\n", encoding="utf-8")
        raise
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a locked public benchmark source")
    parser.add_argument("task_id")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--git", default="git")
    args = parser.parse_args(argv)
    try:
        prepare(args.task_id, args.destination, git=args.git)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1
    print("Prepared locked base commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
