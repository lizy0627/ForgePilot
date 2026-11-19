"""Real Main QML -> production Mission -> offline model/local MCP, then restart/read."""
import argparse
import asyncio
from contextlib import contextmanager
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

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "agent/tests"))

from forgepilot.process import executable
from mission_tools_support import MissionToolsCase, SOURCE


@contextmanager
def serve_app(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning",
                                         timeout_graceful_shutdown=2))
    thread = Thread(target=lambda: server.run(sockets=[sock]), name="mission-tools-http")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Production Mission fixture did not start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(20)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Production Mission fixture did not stop")


@contextmanager
def serve(case):
    try:
        with serve_app(case.app) as url:
            yield url
    finally:
        case.store.close()


def run_qt(url, source, sid, output, *, task_id=None):
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM="offscreen", FORGEPILOT_MISSION_TOOLS_URL=url,
        FORGEPILOT_MISSION_TOOLS_REPOSITORY=str(source), FORGEPILOT_MISSION_TOOLS_MCP_ID=sid,
        FORGEPILOT_MISSION_TOOLS_OUTPUT=str(output), FORGEPILOT_MISSION_TOOLS_TOOL_NAME="project_info",
        FORGEPILOT_MISSION_TOOLS_EXPECT_RAG_USED="1", FORGEPILOT_MISSION_TOOLS_EXPECT_MCP_USED="1")
    if task_id is not None:
        env["FORGEPILOT_MISSION_TOOLS_TASK_ID"] = task_id
    else:
        env.pop("FORGEPILOT_MISSION_TOOLS_TASK_ID", None)
    qt_bin = os.environ.get("FORGEPILOT_QT_KIT", "D:/Qt/6.8.3/msvc2022_64")
    env["PATH"] = str(Path(qt_bin) / "bin") + os.pathsep + env.get("PATH", "")
    report = output / "qt-results.txt"
    result = subprocess.run([str(ROOT / "client/build/Release/ForgePilotWorkflowQmlTests.exe"),
        "missionToolsFromMainUsesFrozenSelection", "-v1", "-o", str(report) + ",txt"],
        env=env, capture_output=True, timeout=90)
    detail = (report.read_bytes() if report.exists() else b"") + result.stdout + result.stderr
    (output / "qt.log").write_bytes(detail)
    print(detail.decode("utf-8", errors="replace"), flush=True)
    if result.returncode:
        raise RuntimeError(f"Mission tools desktop integration failed with exit {result.returncode}; {report}")
    return json.loads((output / "mission-tools-qt-snapshot.json").read_text(encoding="utf-8"))


def wait_plan(url, task_id):
    deadline = time.monotonic() + 30
    with httpx.Client(base_url=url, trust_env=False) as client:
        while time.monotonic() < deadline:
            response = client.get("/api/v2/missions/" + task_id)
            response.raise_for_status()
            result = response.json()
            if result["status"] in {"awaiting_human", "failed", "retryable"}:
                assert result["status"] == "awaiting_human", result
                return result
            time.sleep(0.02)
    raise RuntimeError("Mission analysis did not reach its approval gate")


def run(output_root, modes):
    output = output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    print("OFFLINE production Mission/Main-QML evidence: " + str(output), flush=True)
    git_exe = executable(os.environ.get("FORGEPILOT_GIT"), "git")
    rg_exe = executable(os.environ.get("FORGEPILOT_RG"), "rg")
    summaries = []
    for mode in modes:
        mode_output = output / mode
        with tempfile.TemporaryDirectory(prefix="fp-mission-tools-") as temporary:
            root = Path(temporary)
            case = MissionToolsCase(root, git_exe=git_exe, rg_exe=rg_exe,
                embedding_failure=mode == "embedding-failure", vector_failure=mode == "vector-failure",
                rag_enabled=mode != "rag-disabled")
            sid = asyncio.run(case.seed_mcp())
            with serve(case) as url:
                run_qt(url, case.source, sid, mode_output / "created")
                page = httpx.get(url + "/api/v2/missions", trust_env=False).json()
                assert len(page["items"]) == 1, page
                task_id = page["items"][0]["task_id"]
                mission = wait_plan(url, task_id)
                evidence = case.evidence()
                evidence["mission"] = mission
                assert mission["tool_context"]["rag"]["used"] and mission["tool_context"]["mcp"]["used"]
                assert mission["tool_context"]["clangd"]["fallback_reasons"]
                reasons = {"embedding-failure": "embedding_failed", "vector-failure": "qdrant_failed", "rag-disabled": "rag_disabled"}
                if mode in reasons:
                    assert reasons[mode] in mission["tool_context"]["rag"]["fallback_reasons"]
                tools = [set(names) for names in evidence["model_tools"] if any(name.startswith("mcp_") for name in names)]
                assert tools and all({"rag_retrieve", "repository_get_symbol"} <= names for names in tools)
                assert not any(entry["method"] in {"POST", "PUT"} and (
                    "/probe" in entry["path"] or entry["path"].endswith("/test")) for entry in case.requests)
                assert not any(workflow.validator._monitors for workflow in case.workflows)
                assert (case.source / "src/value.cpp").read_text(encoding="utf-8") == SOURCE
                (mode_output / "production-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            if mode == "success":
                restarted = MissionToolsCase(root, git_exe=git_exe, rg_exe=rg_exe, source=case.source)
                with serve(restarted) as url:
                    run_qt(url, restarted.source, sid, mode_output / "restarted", task_id=task_id)
                    reread = wait_plan(url, task_id)
                    assert reread["use_rag"] and reread["mcp_server_ids"] == [sid]
                    assert reread["tool_context"] == mission["tool_context"]
                    assert not restarted.provider.seen and not restarted.embeddings.calls and not restarted.vectors.calls
                    assert not restarted.workflows
                    assert restarted.requests and all(entry["method"] == "GET" for entry in restarted.requests)
                    (mode_output / "restart-read-only-evidence.json").write_text(json.dumps(restarted.evidence(), indent=2), encoding="utf-8")
            summaries.append({"mode": mode, "task_id": task_id, "status": "passed"})
    (output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print("Production Mission + Main QML + real local MCP + frozen read-only restart passed", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "build-test/product-readiness-stage3/mission-tools")
    parser.add_argument("--mode", action="append", choices=("success", "embedding-failure", "vector-failure", "rag-disabled"))
    args = parser.parse_args()
    raise SystemExit(run(args.output_root, args.mode or ["success", "embedding-failure", "vector-failure", "rag-disabled"]))
