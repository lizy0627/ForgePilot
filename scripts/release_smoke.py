"""Offline release health check; never contacts a model or external MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    port = free_port()
    environment = os.environ.copy()
    environment["FORGEPILOT_API_KEY"] = "offline-release-smoke-placeholder"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="forgepilot-release-", ignore_cleanup_errors=True) as directory:
        database = Path(directory) / "health.sqlite3"
        command = [
            sys.executable,
            "-m",
            "forgepilot.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--model",
            "offline-health-check",
            "--database",
            str(database),
            "--disable-mcp",
        ]
        process = subprocess.Popen(
            command,
            cwd=AGENT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return 1
                try:
                    request = Request(
                        f"http://127.0.0.1:{port}/openapi.json",
                        headers={"Host": f"127.0.0.1:{port}"},
                    )
                    with urlopen(request, timeout=1) as response:
                        schema = json.load(response)
                    if schema.get("openapi") and "/api/v1/conversations" in schema.get("paths", {}):
                        print("Offline backend health check passed.")
                        return 0
                    return 1
                except OSError:
                    time.sleep(0.2)
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
