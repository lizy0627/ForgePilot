"""Offline extracted ZIP acceptance using only Windows tools on the child PATH.

The host Python runs this verifier; the extracted app, its configuration helper,
backend, owned workers and Qt client must run without Python or uv on their PATH.
All model configuration points at a counting loopback fixture and must remain
unused. The temporary Windows DPAPI credential is a disposable placeholder.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import tempfile
from threading import Thread
import time
from urllib.request import urlopen
import zipfile


SECRET = "offline-frozen-release-disposable-placeholder"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FORBIDDEN = re.compile(
    r"(^|/)(config\.json|[^/]*\.dpapi|[^/]*\.env(?:\.[^/]*)?|logs?|__pycache__|"
    r"\.pytest_cache|\.ruff_cache|\.mypy_cache|\.coverage[^/]*|uv\.exe|pythonw?\.exe)(/|$)"
    r"|\.(py|db|sqlite3?|log|pyc|pdb|obj|lib|exp)$", re.IGNORECASE)


def run(command, root, environment, *, payload=None, timeout=60):
    result = subprocess.run(command, cwd=root, env=environment, input=payload,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"release command failed: {Path(command[0]).name} ({result.returncode})")
    return result


def alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def verify(archive: Path):
    requests = []

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(503)
            self.end_headers()

        def log_message(self, *_):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    fixture_thread = Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    checks = {}
    try:
        with tempfile.TemporaryDirectory(prefix="fpr-") as temporary:
            directory = Path(temporary).resolve()
            assert directory.parent == Path(tempfile.gettempdir()).resolve()
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                roots = {PurePosixPath(name).parts[0] for name in names}
                if len(roots) != 1 or any(PurePosixPath(name).is_absolute() or "\\" in name
                        or ".." in PurePosixPath(name).parts or ":" in name for name in names):
                    raise RuntimeError("invalid release archive layout")
                bad = [name for name in names if FORBIDDEN.search(name)]
                if bad:
                    raise RuntimeError("release archive contains runtime data, credentials or build files")
                bundle.extractall(directory)
            root = directory / roots.pop()
            checks["archive_files"] = len(names)
            checks["archive_forbidden_files"] = 0
            windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            environment = dict(os.environ)
            for name in list(environment):
                if name.startswith("FORGEPILOT_") or name in {
                        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
                        "GOOGLE_API_KEY", "QDRANT_API_KEY", "TAVILY_API_KEY"}:
                    environment.pop(name)
            environment.pop("QT_QPA_PLATFORM", None)
            environment.update(PATH=str(windows / "System32") + ";" + str(windows / "System32/WindowsPowerShell/v1.0"),
                LOCALAPPDATA=str(directory / "local-app-data"), HTTP_PROXY="http://127.0.0.1:9",
                HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9",
                NO_PROXY="127.0.0.1,localhost,::1")
            assert not shutil.which("python.exe", path=environment["PATH"])
            assert not shutil.which("uv.exe", path=environment["PATH"])
            checks["python_and_uv_absent_from_path"] = True
            executable = root / "bin/ForgePilotAgent.exe"
            shell = str(windows / "System32/cmd.exe")

            def entry(name):
                return run([shell, "/d", "/c", str(root / name)], root, environment)

            # Check-Release covers real Qt/QML, OpenAPI, build and stdio workers.
            entry("scripts/Check-Release.cmd")
            checks["offline_preflight"] = "passed"
            identity = hashlib.sha256(str(root).lower().encode("utf-8")).hexdigest()[:16].upper()
            config = Path(environment["LOCALAPPDATA"]) / "ForgePilot/launcher" / identity / "config.json"

            def helper(request):
                result = run([str(executable), "config-helper", "--config", str(config)], root,
                             environment, payload=json.dumps(request).encode(), timeout=30)
                response = json.loads(result.stdout)
                if response.get("ok") is not True:
                    raise RuntimeError("frozen configuration helper failed")
                return response

            loaded = helper({"action": "load"})
            assert loaded["configured"] is False
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fields = {**loaded["fields"], "port": port,
                "base_url": f"http://127.0.0.1:{fixture.server_port}/v1",
                "chat_model": "offline-chat", "task_model": "offline-task",
                "review_model": "offline-review", "summary_model": "offline-summary",
                "task_model_family": "writer", "review_model_family": "reviewer"}
            saved = helper({"action": "save", "fields": fields, "api_key": SECRET,
                            "config_revision": loaded["config_revision"]})
            assert saved["configured"] and saved["has_api_key"]
            data = json.loads(config.read_text(encoding="utf-8"))
            models = Path(data["server_args"][data["server_args"].index("--model-config") + 1])
            assert {route["route_id"] for route in json.loads(models.read_text(encoding="utf-8"))["routes"]} == {
                "task", "chat", "summary", "orchestration", "analysis", "implementation", "review", "repair"}
            checks["frozen_helper_save_and_eight_routes"] = "passed"
            owned = []
            try:
                entry("Start-ForgePilot.cmd")
                state = json.loads(config.with_name("runtime.json").read_text(encoding="utf-8"))
                assert state["status"] == "running" and state["client_status"] == "running"
                owned = [state[key] for key in ("supervisor_pid", "backend_pid", "client_worker_pid", "client_pid")]
                assert all(alive(pid) for pid in owned)
                with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=5) as response:
                    assert "/api/v2/missions" in json.load(response)["paths"]
                checks["start_entry_backend_and_visible_qt_client"] = "passed"
            finally:
                entry("Stop-ForgePilot.cmd")
            deadline = time.monotonic() + 10
            while any(alive(pid) for pid in owned) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not any(alive(pid) for pid in owned)
            checks["stop_entry_and_all_owned_processes_exited"] = "passed"
            for path in (*config.parent.glob("*.json"), *root.glob("logs/desktop/**/*")):
                if path.is_file():
                    assert SECRET.encode() not in path.read_bytes(), "placeholder key leaked to config/log"
            assert requests == [], "offline acceptance unexpectedly contacted model fixture"
            checks["provider_requests"] = 0
            checks["config_and_logs_without_plaintext_key"] = "passed"
    finally:
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=5)
    return {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "mode": "offline; extracted app child PATH contains only Windows tools", "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("output already exists")
    evidence = verify(arguments.archive.resolve())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("Extracted release start/stop acceptance passed; zero model requests.")


if __name__ == "__main__":
    main()
