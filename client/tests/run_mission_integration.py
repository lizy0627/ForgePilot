"""Real Qt/HTTP/SQLite/Graph mission acceptance with deterministic offline nodes."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
from uuid import uuid4

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "agent" / "tests"))

from forgepilot.api import create_app
from forgepilot.process import executable
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.workspace_manager import WorkspaceManager
from test_mission_service import ControlledWorkflow
from test_workspace_manager import git


def run(root, output_root):
    output = output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-mission-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    print("OFFLINE MISSION Qt/HTTP evidence: " + str(output), flush=True)
    git_exe = executable(os.environ.get("FORGEPILOT_GIT"), "git")
    with tempfile.TemporaryDirectory(prefix="ForgePilot Mission Qt ") as temporary:
        isolated = Path(temporary)
        repository = isolated / "user source"
        repository.mkdir()
        git(git_exe, repository, "init", "-q", "-b", "main")
        git(git_exe, repository, "config", "user.name", "Mission Fixture")
        git(git_exe, repository, "config", "user.email", "mission@example.invalid")
        git(git_exe, repository, "config", "core.autocrlf", "false")
        original = b"original source\n"
        (repository / "source.txt").write_bytes(original)
        git(git_exe, repository, "add", "--all")
        git(git_exe, repository, "commit", "-q", "-m", "offline fixture")
        before = git(git_exe, repository, "rev-parse", "HEAD")
        workspace = WorkspaceManager(isolated / "workspaces", git_executable=git_exe)
        store = SqliteEventStore(output / "mission.sqlite3", artifact_root=output / "artifacts", workspace_backend=workspace)
        def no_legacy_execution(trace):
            raise AssertionError("Mission integration must not construct a legacy model service")
        app = create_app(no_legacy_execution, store=store, heartbeat_seconds=0.1,
                         mission_factory=lambda events: ControlledWorkflow(events, workspace))
        requests = []
        @app.middleware("http")
        async def capture(request, call_next):
            response = await call_next(request)
            requests.append(dict(method=request.method, path=request.url.path, status=response.status_code))
            return response
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", timeout_graceful_shutdown=2))
        thread = Thread(target=lambda: server.run(sockets=[sock]))
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Mission backend did not start")
                time.sleep(0.01)
            env = os.environ.copy()
            env.update(FORGEPILOT_INTEGRATION_URL=f"http://127.0.0.1:{sock.getsockname()[1]}",
                       FORGEPILOT_INTEGRATION_REPOSITORY=str(repository), FORGEPILOT_INTEGRATION_OUTPUT=str(output))
            report = output / "mission-qt-results.txt"
            result = subprocess.run([str(root / "client/build/Release/ForgePilotMissionBackendTests.exe"),
                                     "-v1", "-o", str(report) + ",txt"], env=env, capture_output=True, timeout=90)
            detail = (report.read_bytes() if report.exists() else b"") + result.stdout + result.stderr
            (output / "mission-qt.log").write_bytes(detail)
            print(detail.decode("utf-8", errors="replace"), flush=True)
            if result.returncode:
                return result.returncode
            with store._lock:
                missions = [dict(row) for row in store._db.execute("SELECT task_id,status,current_revision FROM task_graph_runs")]
                approvals = [dict(row) for row in store._db.execute("SELECT task_id,status FROM approvals")]
                assert len(missions) == 1 and missions[0]["status"] == "succeeded", missions
                assert len(approvals) == 1 and approvals[0]["status"] == "approved", approvals
                assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
            approval_posts = [entry for entry in requests if entry["method"] == "POST" and entry["path"].endswith("/approvals")]
            assert [entry["status"] for entry in approval_posts] == [409, 202, 409], approval_posts
            assert (repository / "source.txt").read_bytes() == original
            assert git(git_exe, repository, "rev-parse", "HEAD") == before
            assert not git(git_exe, repository, "status", "--porcelain=v1")
            (output / "mission-backend-evidence.json").write_text(json.dumps(dict(missions=missions, approvals=approvals), indent=2), encoding="utf-8")
            print("REAL Qt + v2 HTTP + SSE + SQLite + Graph: create, frozen approval, terminal projections, artifacts and real 409 conflicts passed", flush=True)
        finally:
            server.should_exit = True
            thread.join(20)
            sock.close()
            (output / "http-requests.json").write_text(json.dumps(requests, indent=2), encoding="utf-8")
            if thread.is_alive():
                raise RuntimeError("Mission backend did not stop")
            store.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "logs/client-integration")
    args = parser.parse_args()
    raise SystemExit(run(ROOT, args.output_root))
