"""Trusted worker entrypoints for source and PyInstaller runtimes."""

from pathlib import Path
import sys


def owned_worker_command(kind: str, job: str, *command: str) -> list[str]:
    if kind == "build":
        script, action = "_build_worker.py", "worker"
    elif kind == "stdio":
        script, action = "_lsp_worker.py", "stdio-worker"
    else:
        raise ValueError("unknown owned worker")
    if getattr(sys, "frozen", False):
        if kind == "build":
            return [sys.executable, action, "--job", job, *command]
        return [sys.executable, action, job, *command]
    return [sys.executable, "-I", str(Path(__file__).with_name(script)), job, *command]
