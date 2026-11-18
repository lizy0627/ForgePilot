"""Resume the exact previously reviewed live Mission without paying for analysis again.

The caller starts this only after an explicit user approval, with a dedicated,
bounded ModelGateway. This helper does not create a repository, graph, or Oracle.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from time import monotonic

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_mission_functional import (
    _public_view, _save, DeliveryWorkflow, create_app, DataPolicy, ForgePilotError,
    SqliteEventStore, ToolchainProfile, ProtectedOracle, ValidationService, WorkspaceManager,
)


_ANALYSIS_NODES = frozenset({"draft_task_spec", "analyst_fanout", "root_cause_analysis",
                            "impact_test_analysis", "join_evidence", "generate_plan"})
_FROZEN_FIELDS = ("task_spec_artifact_id", "task_spec_hash", "plan_artifact_id", "plan_hash", "scope_hash")


def _same_package(review, frozen):
    return bool(review and all(review.get(name) == frozen.get(name) for name in _FROZEN_FIELDS))


def _analysis_completed(graph):
    states = {item["node_id"]: item["status"] for item in graph["nodes"]}
    return all(states.get(node) == "succeeded" for node in _ANALYSIS_NODES)


async def run_resume(gateway, output: Path, original: Path):
    """Use the original database and normal recovery/approval/delivery APIs.

    original/human-approval-resume.json must contain approved=true, task_id,
    the ORIGINAL review_hash, and allow_patch_export=true for final patch export.
    Every pre-execution check is deterministic; rejected recovery never calls a model.
    """
    output, original = Path(output).resolve(), Path(original).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "mission-resume-result.json").exists():
        raise FileExistsError("Choose a fresh resume evidence directory")
    result = {"status": "not_verified", "scope": "resume_original_live_mission", "http": [],
        "checks": {name: {"status": "not_verified"} for name in (
            "original_review_authorized", "recovery_without_analysis_replay", "frozen_execution_approval",
            "implementation_validation_independent_review", "patch_delivery", "original_repository_preserved")}}
    store = None
    serving = None
    server = None
    sock = None
    try:
        package = json.loads((original / "review-package.json").read_text(encoding="utf-8"))
        previous = json.loads((original / "mission-result.json").read_text(encoding="utf-8"))
        approval = json.loads((original / "human-approval-resume.json").read_text(encoding="utf-8"))
        task_id = package["task_id"]
        frozen = package["review"]
        if not (approval.get("approved") is True and approval.get("task_id") == task_id
                and approval.get("review_hash") == frozen["review_hash"]):
            result["run_error"] = "exact_original_review_not_approved"
            return result
        short = Path(previous["fixture_root"])
        source = Path(previous["source_repository"])
        database = Path(previous["evaluator_database"])
        if (source != short / "s" or database != short / "mission.sqlite3" or not database.is_file()
                or not (short / "o").is_dir() or not (short / "a").is_dir() or not (short / "w").is_dir()):
            raise ForgePilotError("mission_resume_identity", "Original fixture/database/Oracle paths do not match")
        result.update(task_id=task_id, fixture_root=str(short), source_repository=str(source),
            evaluator_database=str(database), original_review_hash=frozen["review_hash"])
        result["checks"]["original_review_authorized"] = {"status": "passed"}
        git_exe, rg_exe = shutil.which("git"), shutil.which("rg")
        if not git_exe or not rg_exe:
            raise RuntimeError("Git and ripgrep are required")
        git_records = []
        def git(*arguments):
            command = [git_exe, "-C", str(source), *arguments]
            completed = subprocess.run(command, capture_output=True, check=False)
            git_records.append({"argv": command, "exit_code": completed.returncode})
            if completed.returncode:
                raise RuntimeError("Original functional repository inspection failed")
            return completed.stdout.decode("utf-8", errors="replace").strip()
        original_source = (source / "src/value.cpp").read_bytes()
        original_head = git("rev-parse", "HEAD")
        if git("status", "--porcelain"):
            raise ForgePilotError("mission_resume_identity", "Original fixture repository is no longer clean")
        workspace = WorkspaceManager(short / "w", git_executable=git_exe)
        # Reopening the owned database runs its normal interrupted-task inspection.
        store = SqliteEventStore(database, artifact_root=short / "a", workspace_backend=workspace,
                                 redactor=gateway.redactor)
        profile = ToolchainProfile(cmake="D:/CMake/bin/cmake.exe", ctest="D:/CMake/bin/ctest.exe")
        def factory(events):
            validator = ValidationService(workspace, profile, build_root=short / "v",
                oracle=ProtectedOracle(short / "o"), configuration="Release", generator="Visual Studio 17 2022",
                architecture="x64", timeout=180, redactor=gateway.redactor)
            workflow = DeliveryWorkflow(events, workspace, gateway, validator=validator, rg_executable=rg_exe)
            workflow.mission_data_policy = DataPolicy(cloud_model_allowed=True, external_mcp_allowed=False)
            return workflow
        def no_v1_task(trace):
            raise AssertionError("Resume must use the existing API v2 Mission")
        app = create_app(no_v1_task, store=store, mission_factory=factory, mcp_disabled=True,
                         model_client_closer=gateway.aclose)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, log_config=None))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        async def request(client, method, suffix="", body=None):
            path = f"/api/v2/missions/{task_id}{suffix}"
            response = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
            result["http"].append({"method": method, "path": path, "status": response.status_code})
            if response.is_error:
                try:
                    error = response.json().get("error", {}).get("code", "http_error")
                except (ValueError, AttributeError):
                    error = "http_error"
                raise ForgePilotError("mission_resume_http", f"HTTP {response.status_code}: {error}")
            return response
        async def get(client, suffix=""):
            return (await request(client, "GET", suffix)).json()
        async def snapshot(client, label):
            values = {}
            for name, suffix in (("state", ""), ("graph", "/graph"), ("artifacts", "/artifacts"), ("validation", "/validation")):
                values[name] = await get(client, suffix)
                _save(output / f"{label}-{name}.json", _public_view(values[name]))
            return values
        deadline = monotonic() + 20
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Resume HTTP server stopped before readiness")
            if monotonic() > deadline:
                raise TimeoutError("Resume HTTP server readiness timed out")
            await asyncio.sleep(.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=200) as client:
            before = await snapshot(client, "startup")
            if not _same_package(before["state"].get("review"), frozen) or not _analysis_completed(before["graph"]):
                raise ForgePilotError("mission_resume_changed", "Frozen package changed or completed analysis is unavailable")
            storage_resume = (before["state"]["status"] == "failed"
                and next(n for n in before["graph"]["nodes"] if n["node_id"] == "execution_approval")["status"] == "succeeded")
            if not storage_resume and any(node["status"] == "running" for node in before["graph"]["nodes"]):
                raise ForgePilotError("mission_resume_active", "Original Mission still has an active node")
            preview = (await request(client, "POST", "/resume", {
                "expected_revision": before["state"]["current_revision"]})).json()
            _save(output / "recovery-preview.json", _public_view(preview))
            recovery = preview.get("recovery") or {}
            if recovery.get("blocked_reason") or not recovery.get("token"):
                raise ForgePilotError("mission_resume_blocked", "Normal recovery inspection is blocked")
            for action in recovery.get("actions", []):
                if action["node_id"] in _ANALYSIS_NODES and action["action"] not in {"reuse_checkpoint", "complete_intent"}:
                    raise ForgePilotError("mission_resume_analysis_replay", "Recovery would rerun paid analysis")
            if storage_resume and not any(a["node_id"] == "implementer" and a["action"] == "adopt_changes"
                                          for a in recovery.get("actions", [])):
                raise ForgePilotError("mission_resume_writer_replay", "Storage recovery must adopt verified writer bytes")
            # No model call may have occurred before this exact frozen recovery confirmation.
            await request(client, "POST", "/resume", {
                "expected_revision": preview["mission"]["current_revision"], "recovery_token": recovery["token"]})
            deadline = monotonic() + 30
            while not storage_resume:
                resumed = await get(client)
                graph = await get(client, "/graph")
                if not _analysis_completed(graph):
                    raise ForgePilotError("mission_resume_analysis_replay", "Analysis nodes changed during recovery")
                if not _same_package(resumed.get("review"), frozen):
                    raise ForgePilotError("mission_resume_changed", "Recovery changed the approved package")
                if resumed["status"] == "awaiting_human" and not resumed["recovery_required"]:
                    break
                if resumed["status"] in {"failed", "cancelled", "retryable"} or monotonic() > deadline:
                    raise ForgePilotError("mission_resume_blocked", "Recovery did not return to the existing approval gate")
                await asyncio.sleep(.15)
            result["checks"]["recovery_without_analysis_replay"] = {"status": "passed"}
            if not storage_resume:
                # Let the deterministic replay release its control handle.
                await asyncio.sleep(.3)
                resumed = await get(client)
                if not _same_package(resumed.get("review"), frozen):
                    raise ForgePilotError("mission_resume_changed", "Package changed before execution approval")
                _save(output / "recovered-review.json", _public_view(resumed))
                current_review = resumed["review"]
                await request(client, "POST", "/approvals", {
                    "expected_revision": resumed["current_revision"], "approval_type": "execution", "decision": "approved",
                    "task_spec_hash": current_review["task_spec_hash"], "plan_hash": current_review["plan_hash"],
                    "scope_hash": current_review["scope_hash"]})
            result["checks"]["frozen_execution_approval"] = {"status": "passed", "reused_original_approval": storage_resume}
            deadline = monotonic() + 600
            while True:
                state = await get(client)
                graph = await get(client, "/graph")
                delivery = next((node for node in graph["nodes"] if node["node_id"] == "delivery_bundle"), {})
                if delivery.get("status") == "succeeded" or state["status"] in {"failed", "cancelled", "retryable", "awaiting_human"}:
                    break
                if monotonic() > deadline:
                    raise TimeoutError("Approved Mission did not finish within the live-check limit")
                await asyncio.sleep(.35)
            final = await snapshot(client, "final")
            _save(output / "final-diff.json", _public_view(await get(client, "/diff")))
            _save(output / "observability.json", _public_view(await get(client, "/observability")))
            latest = final["validation"].get("latest") or {}
            validation = latest.get("content", {})
            artifacts = final["artifacts"]["items"]
            reviews = [item["content"] for item in artifacts if item["artifact_type"] == "ReviewReport"]
            complete = (final["state"]["status"] == "succeeded" and validation.get("status") == "passed"
                and validation.get("hidden_oracle") == "passed" and reviews and reviews[-1].get("recommend_delivery")
                and any(item["artifact_type"] == "DeliveryBundle" for item in artifacts))
            result.update(graph_status=final["state"]["status"], validation_status=validation.get("status"),
                hidden_oracle=validation.get("hidden_oracle"), public_tests_passed=validation.get("tests_passed"),
                independent_reviewer_approved=bool(reviews and reviews[-1].get("recommend_delivery")))
            result["checks"]["implementation_validation_independent_review"] = {"status": "passed" if complete else "failed"}
            if complete:
                events = await request(client, "GET", "/events")
                (output / "mission-events.txt").write_text(events.text, encoding="utf-8")
                delivery_preview = (await request(client, "POST", "/delivery", {
                    "expected_revision": final["state"]["current_revision"], "mode": "preview"})).json()
                _save(output / "delivery-preview.json", _public_view(delivery_preview))
                if approval.get("allow_patch_export") is True:
                    exported = (await request(client, "POST", "/delivery", {
                        "expected_revision": delivery_preview["mission"]["current_revision"], "mode": "patch",
                        "token": delivery_preview["delivery"]["token"]})).json()
                    _save(output / "delivery-export.json", _public_view(exported))
                    result["checks"]["patch_delivery"] = {"status": "passed"}
            preserved = ((source / "src/value.cpp").read_bytes() == original_source
                         and git("rev-parse", "HEAD") == original_head and not git("status", "--porcelain"))
            result["checks"]["original_repository_preserved"] = {"status": "passed" if preserved else "failed"}
            result["status"] = "passed" if complete and preserved and result["checks"]["patch_delivery"]["status"] == "passed" else "failed"
            with store._lock:
                result["phase_roles"] = [row["role"] for row in store._db.execute(
                    "SELECT role FROM model_calls WHERE task_id=? ORDER BY created_at", (task_id,))]
            _save(output / "git-commands.json", git_records)
            return result
    except BaseException as exc:
        result.update(status="failed", run_error=exc.code if isinstance(exc, ForgePilotError) else type(exc).__name__)
        return result
    finally:
        if result["status"] == "failed" and "run_error" not in result:
            result["run_error"] = "resumed_mission_did_not_complete"
        _save(output / "mission-resume-result.json", result)
        if server is not None:
            server.should_exit = True
        try:
            if serving is not None:
                await serving
            else:
                await gateway.aclose()
        finally:
            if sock is not None:
                sock.close()
            if store is not None:
                store.close()
        print("MISSION_RESUME_DONE", result["status"], flush=True)
