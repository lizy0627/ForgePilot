"""A small live multi-Agent Mission check on a disposable CMake repository.

The caller owns paid-request accounting and supplies a dedicated ModelGateway.
Only the generated review package can be approved, by a separate human file.
No benchmark asset is read; the functional Oracle is created outside candidates.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.artifacts.models import DataPolicy
from forgepilot.errors import ForgePilotError
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.toolchain import ToolchainProfile
from forgepilot.validation import ProtectedOracle, ValidationService
from forgepilot.workspace_manager import WorkspaceManager


def _save(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _public_view(value):
    """Retain finite API evidence, never full protected-process diagnostics."""
    result = deepcopy(value)
    if isinstance(result, dict):
        if result.get("artifact_type") == "ValidationReport":
            for command in result.get("content", {}).get("commands", []):
                if command.get("kind") == "protected_test":
                    command["output_summary"] = "Protected Oracle diagnostics withheld"
        return {key: _public_view(item) for key, item in result.items() if key != "token"}
    if isinstance(result, list):
        return [_public_view(item) for item in result]
    return result


async def run_mission(gateway, output: Path):
    """Run real API v2 and return a finite result; no request is sent on import.

    Awaiting approval produces output/review-package.json. The trusted caller may
    write output/human-approval.json after the user reviews that exact package:
    {"approved": true, "task_id": "...", "review_hash": "...",
     "allow_patch_export": true}. No main-branch merge is ever requested.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "mission-result.json").exists():
        raise FileExistsError("Choose a fresh Mission evidence directory")
    short = Path("D:/") / ("FPAdv1108-" + uuid4().hex[:8])
    short.mkdir(exist_ok=False)
    source = short / "s"
    oracle = short / "o"
    source.mkdir()
    (source / "src").mkdir()
    (source / "tests").mkdir()
    oracle.mkdir()
    original = "int value(int input) { return input; }\n// preserve this user comment\n"
    (source / "src/value.cpp").write_text(original, encoding="utf-8")
    (source / "tests/public.cpp").write_text(
        "int value(int);\nint main() { return value(-1) == 0 && value(3) == 3 ? 0 : 1; }\n",
        encoding="utf-8")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.21)\nproject(MissionFunctionCheck LANGUAGES CXX)\n"
        "set(CMAKE_CXX_STANDARD 17)\nenable_testing()\nadd_library(value src/value.cpp)\n"
        "add_executable(public_test tests/public.cpp)\ntarget_link_libraries(public_test PRIVATE value)\n"
        "add_test(NAME public_behavior COMMAND public_test)\n", encoding="utf-8")
    # Self-authored evaluator data, outside both original and candidate repositories.
    (oracle / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.21)\nproject(MissionFunctionOracle LANGUAGES CXX)\n"
        "enable_testing()\nadd_executable(behavior check.cpp \"${FORGEPILOT_WORKTREE}/src/value.cpp\")\n"
        "add_test(NAME protected_behavior COMMAND behavior)\n", encoding="utf-8")
    (oracle / "check.cpp").write_text(
        "int value(int);\nint main() { return value(-7) == 0 && value(0) == 0 && value(9) == 9 ? 0 : 1; }\n",
        encoding="utf-8")
    git_exe = shutil.which("git")
    rg_exe = shutil.which("rg")
    if not git_exe or not rg_exe:
        raise RuntimeError("Git and ripgrep are required for the Mission functional check")
    git_records = []
    def git(*arguments):
        command = [git_exe, "-C", str(source), *arguments]
        result = subprocess.run(command, capture_output=True, check=False)
        git_records.append({"argv": command, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError("Disposable Git setup failed")
        return result.stdout.decode("utf-8", errors="replace").strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "ForgePilot Functional Test")
    git("config", "user.email", "functional@localhost")
    git("config", "core.autocrlf", "false")
    git("add", "--all")
    git("commit", "-qm", "Disposable Mission baseline")
    before_head = git("rev-parse", "HEAD")
    workspace = WorkspaceManager(short / "w", git_executable=git_exe)
    store = SqliteEventStore(short / "mission.sqlite3", artifact_root=short / "a", workspace_backend=workspace,
                            redactor=gateway.redactor)
    profile = ToolchainProfile(cmake="D:/CMake/bin/cmake.exe", ctest="D:/CMake/bin/ctest.exe")
    def factory(events):
        validator = ValidationService(workspace, profile, build_root=short / "v",
            oracle=ProtectedOracle(oracle), configuration="Release", generator="Visual Studio 17 2022",
            architecture="x64", timeout=180, redactor=gateway.redactor)
        workflow = DeliveryWorkflow(events, workspace, gateway, validator=validator, rg_executable=rg_exe)
        workflow.mission_data_policy = DataPolicy(cloud_model_allowed=True, external_mcp_allowed=False)
        return workflow
    def no_legacy_task(trace):
        raise AssertionError("The Mission check must use API v2")
    app = create_app(no_legacy_task, store=store, mission_factory=factory, mcp_disabled=True,
                     model_client_closer=gateway.aclose)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, log_config=None))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    result = {"status": "not_verified", "scope": "live_multi_agent_functional_check",
              "fixture_root": str(short), "source_repository": str(source),
              "evaluator_database": str(short / "mission.sqlite3"), "delivery_mode": "patch_only",
              "process_isolation_acceptance": "not_claimed", "http": [],
              "checks": {name: {"status": "not_verified"} for name in (
                  "mission_create_and_parallel_analysis", "frozen_execution_approval",
                  "implementation_validation_independent_review", "patch_delivery",
                  "original_repository_preserved")}}
    _save(output / "mission-result.json", result)
    task_id = None
    async def request(client, method, path, body=None):
        response = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
        result["http"].append({"method": method, "path": path, "status": response.status_code})
        if response.is_error:
            try:
                code = response.json().get("error", {}).get("code", "http_error")
            except (ValueError, AttributeError):
                code = "http_error"
            raise ForgePilotError("mission_functional_http", f"HTTP {response.status_code}: {code}")
        return response
    async def get(client, suffix=""):
        return (await request(client, "GET", f"/api/v2/missions/{task_id}{suffix}")).json()
    async def snapshot(client, label):
        state = await get(client)
        graph = await get(client, "/graph")
        artifacts = await get(client, "/artifacts")
        validation = await get(client, "/validation")
        for name, value in (("state", state), ("graph", graph), ("artifacts", artifacts), ("validation", validation)):
            _save(output / f"{label}-{name}.json", _public_view(value))
        return state, graph, artifacts, validation
    async def wait(client, *, final=False, seconds=600):
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            state = await get(client)
            if not final and state["status"] == "awaiting_human" and state.get("review"):
                return state
            if state["status"] in {"failed", "cancelled", "retryable", "skipped"}:
                return state
            if final:
                graph = await get(client, "/graph")
                delivery = next((node for node in graph["nodes"] if node["node_id"] == "delivery_bundle"), {})
                if delivery.get("status") == "succeeded" or state["status"] == "awaiting_human":
                    return state
            await asyncio.sleep(.35)
        raise TimeoutError("Mission did not reach a review or terminal result within the functional-check limit")
    try:
        deadline = monotonic() + 20
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Mission HTTP server stopped before readiness")
            if monotonic() > deadline:
                raise TimeoutError("Mission HTTP server readiness timed out")
            await asyncio.sleep(.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=200) as client:
            created = (await request(client, "POST", "/api/v2/missions", {
                "repository_path": str(source), "base_ref": "HEAD", "task_type": "bug_fix",
                "orchestration_mode": "multi_agent", "toolchain_profile": "cmake-ctest-v1", "language": "zh-CN",
                "request": "这是临时C++功能测试仓库。src/value.cpp 的 value(input) 当前原样返回输入，"
                    "应把负数钳制为0，非负数保持不变，保留用户注释。只允许修改src/value.cpp；"
                    "现有tests/public.cpp和CMakeLists.txt只读，不新增文件或测试。"
                    "验收三项：负数/零/正数的回归行为通过protected_test、CMake构建通过build、所有公开CTest通过ctest。"
                    "仅有这三个公开文件；两个Analyst各自在一个工具批次读取它们，取得实际SHA256与行号后立即输出简短EvidenceBundle，"
                    "不要重复搜索。分析与方案保持精简，等待人工批准。实现时读取当前src/value.cpp后用精确patch修复，随后输出完成JSON；"
                    "不执行构建、Git或测试，验证由系统负责。最终仅导出patch，不合并原仓库。"
            })).json()
            task_id = created["task_id"]
            result["task_id"] = task_id
            result["status"] = "analyzing"
            _save(output / "mission-result.json", result)
            state = await wait(client)
            state, graph, artifacts, _ = await snapshot(client, "analysis")
            if state["status"] != "awaiting_human" or not state.get("review"):
                result.update(status="failed", reason="analysis_or_baseline_did_not_reach_review", graph_status=state["status"])
                result["checks"]["mission_create_and_parallel_analysis"] = {"status": "failed"}
                return result
            review = state["review"]
            package = {"task_id": task_id, "source_repository": str(source), "review": review,
                "artifacts": [item for item in artifacts["items"] if item["artifact_type"] in {"TaskSpec", "ExecutionPlan", "EvidenceBundle"}],
                "delivery_intent": "Only export a patch from the disposable task worktree; never merge the original repository",
                "approval_file": str(output / "human-approval.json"),
                "approval_fields": {"approved": True, "task_id": task_id, "review_hash": review["review_hash"],
                                    "allow_patch_export": True}}
            _save(output / "review-package.json", _public_view(package))
            result.update(status="awaiting_human", review_hash=review["review_hash"])
            result["checks"]["mission_create_and_parallel_analysis"] = {"status": "passed"}
            _save(output / "mission-result.json", result)
            print("MISSION_REVIEW_READY", str(output / "review-package.json"), flush=True)
            approval_path = output / "human-approval.json"
            deadline = monotonic() + 600
            while not approval_path.is_file() and monotonic() < deadline:
                await asyncio.sleep(.5)
            approval = json.loads(approval_path.read_text(encoding="utf-8")) if approval_path.is_file() else {}
            if not (approval.get("approved") is True and approval.get("task_id") == task_id
                    and approval.get("review_hash") == review["review_hash"]):
                current = await get(client)
                await request(client, "POST", f"/api/v2/missions/{task_id}/cancel", {"expected_revision": current["current_revision"]})
                result.update(status="not_verified", reason="exact_human_approval_not_received")
                return result
            await request(client, "POST", f"/api/v2/missions/{task_id}/approvals", {
                "expected_revision": review["revision"], "approval_type": "execution", "decision": "approved",
                "task_spec_hash": review["task_spec_hash"], "plan_hash": review["plan_hash"], "scope_hash": review["scope_hash"]})
            result["checks"]["frozen_execution_approval"] = {"status": "passed"}
            await wait(client, final=True)
            state, graph, artifacts, validation = await snapshot(client, "final")
            _save(output / "final-diff.json", _public_view(await get(client, "/diff")))
            _save(output / "observability.json", _public_view(await get(client, "/observability")))
            latest = validation.get("latest")
            content = latest.get("content", {}) if isinstance(latest, dict) else {}
            delivered = any(item["artifact_type"] == "DeliveryBundle" for item in artifacts["items"])
            reviewer = [item["content"] for item in artifacts["items"] if item["artifact_type"] == "ReviewReport"]
            result.update(graph_status=state["status"], validation_status=content.get("status"),
                public_tests_passed=content.get("tests_passed"), hidden_oracle=content.get("hidden_oracle"),
                independent_reviewer_approved=bool(reviewer and reviewer[-1].get("recommend_delivery")),
                delivery_bundle_created=delivered)
            complete = (state["status"] == "succeeded" and content.get("status") == "passed"
                and content.get("hidden_oracle") == "passed" and delivered and result["independent_reviewer_approved"])
            result["checks"]["implementation_validation_independent_review"] = {"status": "passed" if complete else "failed"}
            if complete:
                stream = await request(client, "GET", f"/api/v2/missions/{task_id}/events")
                (output / "mission-events.txt").write_text(stream.text, encoding="utf-8")
                preview = (await request(client, "POST", f"/api/v2/missions/{task_id}/delivery", {
                    "expected_revision": state["current_revision"], "mode": "preview"})).json()
                _save(output / "delivery-preview.json", _public_view(preview))
                if approval.get("allow_patch_export") is True:
                    export = (await request(client, "POST", f"/api/v2/missions/{task_id}/delivery", {
                        "expected_revision": preview["mission"]["current_revision"], "mode": "patch",
                        "token": preview["delivery"]["token"]})).json()
                    _save(output / "delivery-export.json", _public_view(export))
                    result["patch_exported"] = True
                    result["checks"]["patch_delivery"] = {"status": "passed"}
                else:
                    result["patch_exported"] = False
            original_unchanged = ((source / "src/value.cpp").read_text(encoding="utf-8") == original
                                  and git("rev-parse", "HEAD") == before_head and not git("status", "--porcelain"))
            result["original_source_unchanged"] = original_unchanged
            result["checks"]["original_repository_preserved"] = {"status": "passed" if original_unchanged else "failed"}
            result["status"] = "passed" if complete and result.get("patch_exported") and original_unchanged else "failed"
            result["phase_roles"] = [row["role"] for row in store._db.execute(
                "SELECT role FROM model_calls WHERE task_id=? ORDER BY created_at", (task_id,))]
            return result
    except BaseException as exc:
        result.update(status="failed", error_code=exc.code if isinstance(exc, ForgePilotError) else type(exc).__name__)
        result["run_error"] = result["error_code"]
        return result
    finally:
        if result["status"] == "failed" and "run_error" not in result:
            result["run_error"] = result.get("reason", "mission_graph_failed")
        _save(output / "git-commands.json", git_records)
        _save(output / "mission-result.json", result)
        server.should_exit = True
        try:
            await serving
        finally:
            sock.close()
            store.close()
        print("MISSION_FUNCTIONAL_DONE", result["status"], flush=True)
