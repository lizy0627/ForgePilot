"""Exercise writer approval through Main.qml, localhost v2, SQLite and real CMake/CTest."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

import httpx

from run_mission_tools_integration import ROOT, serve_app

from closed_loop_support import ORIGINAL, create, done, make_loop_case, patch, response
from forgepilot.mission_events import publish_mission_event
from forgepilot.process import executable
from test_mission_scope_expansion import HASHES, HEADER, HELPER, PARTIAL, USES_HELPER, request_scope, scope_app


async def scenario(mode, root, output):
    git = executable(os.environ.get("FORGEPILOT_GIT"), "git")
    fixtures = {
        "approve": dict(implement=[patch(after=USES_HELPER), done()],
            repair=[patch(USES_HELPER, PARTIAL + USES_HELPER), request_scope(), create(HELPER, HEADER), done()]),
        "reject": dict(implement=[patch(after=PARTIAL + ORIGINAL), request_scope()]),
        "legacy": dict(implement=[response(json.dumps({"status": "awaiting_human",
            "summary": "Please allow clamp.hpp for STEP-1"}))]),
    }
    case = await make_loop_case(root, git, **fixtures[mode])
    decisions, previews = [], []
    async with scope_app(case, root, git) as app:
        @app.middleware("http")
        async def record(request, call_next):
            path = request.url.path
            if request.method == "POST" and path.endswith("/approvals"):
                task_id = path.split("/")[-2]
                body = await request.json()
                if mode == "approve" and body.get("approval_type") == "scope_expansion" and len(decisions) == 1:
                    # A concurrent event makes the first displayed revision genuinely stale.
                    publish_mission_event(case.events, task_id, "task.state_changed", {})
                result = await call_next(request)
                decisions.append((task_id, body, result.status_code))
                return result
            if request.method == "GET" and path.endswith("/scope-expansion"):
                result = await call_next(request)
                previews.append(result.status_code)
                return result
            return await call_next(request)

        with serve_app(app) as url:
            try:
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", FORGEPILOT_SCOPE_URL=url,
                    FORGEPILOT_SCOPE_REPOSITORY=str(case.root), FORGEPILOT_SCOPE_MODE=mode,
                    FORGEPILOT_UI_OUTPUT=str(output))
                kit = Path(env.get("FORGEPILOT_QT_KIT", "D:/Qt/6.8.3/msvc2022_64"))
                env["PATH"] = str(kit / "bin") + os.pathsep + env.get("PATH", "")
                log = output / "qt-results.txt"
                binary = ROOT / "client/build/Release/ForgePilotWorkflowQmlTests.exe"
                result = subprocess.run([str(binary), "scopeExpansionFromMain", "-v1", "-o", str(log) + ",txt"],
                                        capture_output=True, timeout=180, env=env)
                print(log.read_text(encoding="utf-8") if log.exists() else result.stderr, flush=True)
                result.check_returncode()
                task_id = decisions[0][0]
                assert all(row[0] == task_id for row in decisions), decisions
                assert decisions[0][1]["approval_type"] == "execution" and decisions[0][2] == 202, decisions
                with httpx.Client(base_url=f"{url}/api/v2/missions/{task_id}/", trust_env=False) as client:
                    graph = client.get("graph").json()
                    nodes = {node["node_id"]: node for node in graph["nodes"]}
                    if mode == "approve":
                        assert graph["status"] == "succeeded", graph
                        assert nodes["repair_1"]["attempt"] == 2 and nodes["implementer"]["attempt"] == 1
                        assert [row[2] for row in decisions[1:]] == [409, 202] and previews.count(200) >= 2
                        stale, reviewed = [row[1] for row in decisions[1:]]
                        assert stale["expected_revision"] < reviewed["expected_revision"]
                        assert all(stale[key] == reviewed[key] for key in HASHES)
                        final_patch = client.get("diff").json()["patch"]
                        assert HEADER.strip() in final_patch and PARTIAL.strip() not in final_patch
                    elif mode == "reject":
                        assert graph["status"] == "cancelled" and nodes["implementer"]["attempt"] == 1
                        assert len(decisions) == 2 and decisions[1][1]["decision"] == "rejected"
                        assert decisions[1][2] == 202 and 200 in previews
                    else:
                        assert graph["status"] == "cancelled" and len(decisions) == 1 and not previews
                assert (case.root / "src/value.cpp").read_bytes() == ORIGINAL.encode()
            finally:
                (output / "requests.json").write_text(json.dumps(dict(decisions=decisions, previews=previews),
                                                                  indent=2), encoding="utf-8")
                await case.gateway.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", action="append", choices=("approve", "reject", "legacy"))
    parser.add_argument("--output-root", type=Path, default=ROOT / "logs/client-integration")
    args = parser.parse_args()
    output = args.output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                           + "-scope-" + uuid4().hex[:8])
    for mode in args.mode or ("approve", "reject", "legacy"):
        evidence = output / mode
        evidence.mkdir(parents=True)
        print(f"{mode}: {evidence}", flush=True)
        with tempfile.TemporaryDirectory(prefix="fp-scope-") as temp:
            root = Path(temp) / "case"
            root.mkdir()
            asyncio.run(scenario(mode, root, evidence))


if __name__ == "__main__":
    main()
