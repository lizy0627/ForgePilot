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


def configuration_helper_command(config: Path) -> list[str]:
    """Use the same packaged launcher entrypoint as the offline Qt wizard."""
    prefix = ([sys.executable] if getattr(sys, "frozen", False) else
              [sys.executable, "-I", str(ROOT / "scripts" / "desktop_launcher.py")])
    return [*prefix, "config-helper", "--config", str(config)]


def configuration_helper_smoke(directory: Path, environment: dict[str, str]) -> bool:
    """Read absent settings through the helper without saving or making requests."""
    config = directory / "unconfigured" / "config.json"
    helper_environment = dict(environment)
    helper_environment.pop("FORGEPILOT_API_KEY", None)
    try:
        result = subprocess.run(configuration_helper_command(config),
            input=b'{"action":"load"}\n', cwd=ROOT, env=helper_environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        response = json.loads(result.stdout)
        return (result.returncode == 0 and isinstance(response, dict) and response.get("ok") is True
                and response.get("configured") is False
                and response.get("has_api_key") is False
                and response.get("has_embedding_key") is False and not config.exists())
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return False



def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    port = free_port()
    environment = os.environ.copy()
    for name in (
        "FORGEPILOT_API_KEY", "FORGEPILOT_BASE_URL", "FORGEPILOT_MODEL_CONFIG",
        "FORGEPILOT_MCP_CONFIG", "FORGEPILOT_QDRANT_URL",
        "FORGEPILOT_EMBEDDING_API_KEY", "FORGEPILOT_EMBEDDING_BASE_URL",
        "FORGEPILOT_LOCAL_API_TOKEN", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "GOOGLE_API_KEY",
        "QDRANT_API_KEY", "TAVILY_API_KEY",
    ):
        environment.pop(name, None)
    environment["FORGEPILOT_API_KEY"] = "offline-release-smoke-placeholder"
    environment.update(HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                       ALL_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost,::1")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="forgepilot-release-", ignore_cleanup_errors=True) as directory:
        if not configuration_helper_smoke(Path(directory), environment):
            return 1
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
        launched_at = time.monotonic()
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
                        print("Offline configuration helper check passed.")
                        print(f"backend_ready_ms={(time.monotonic() - launched_at) * 1000:.3f}")
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
