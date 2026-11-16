"""Reviewed source and preset inputs cross the real HTTP/Graph/Validator boundary."""
import json
from pathlib import Path

import httpx
import pytest

from closed_loop_support import make_loop_case
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.build_process import run_build_process
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.validation import ValidationService
from test_mission_api_workflow import AnalysisProvider, poll
from test_workspace_manager import git


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [False, True], ids=["reject-no-target-process", "real-preset-pipeline"])
async def test_reviewed_selected_source_and_presets_share_one_baseline(tmp_path, git_exe, approve, monkeypatch):
    case = await make_loop_case(tmp_path, git_exe)
    cmake = case.root / "CMakeLists.txt"
    cmake.write_text(cmake.read_text(encoding="utf-8") + '\nif(NOT STAGE2_MARKER STREQUAL "inherited-value")\n'
        '  message(FATAL_ERROR "Reviewed preset cache was not applied")\nendif()\n', encoding="utf-8")
    (case.root / "CMakePresets.json").write_text(json.dumps({
        "version": 3,
        "configurePresets": [
            {"name": "shared", "hidden": True, "generator": "Visual Studio 17 2022",
             "cacheVariables": {"STAGE2_MARKER": "inherited-value"}},
            {"name": "local", "inherits": "shared", "binaryDir": "${sourceDir}/out/${presetName}"}],
        "buildPresets": [{"name": "compile", "configurePreset": "local", "configuration": "Debug"}],
        "testPresets": [{"name": "verify", "configurePreset": "local", "configuration": "Debug"}],
    }), encoding="utf-8")
    git(git_exe, case.root, "add", "--all")
    git(git_exe, case.root, "commit", "-qm", "preset fixture")
    head = git(git_exe, case.root, "rev-parse", "HEAD").decode().strip()
    documentation = case.root / "README.txt"
    documentation.write_text("User-confirmed baseline documentation\n", encoding="utf-8")
    source_status = git(git_exe, case.root, "status", "--porcelain=v1")
    source_index = (case.root / ".git/index").read_bytes()
    original_source = (case.root / "src/value.cpp").read_bytes()

    base = case.gateway.router.configuration
    providers = {**case.providers, "analysis": AnalysisProvider(case)}
    config = ModelConfiguration(profiles=(*base.profiles, ModelProfile(profile_id="analysis",
        base_url="http://127.0.0.1:1/v1", api_key_ref="env:FORGEPILOT_API_KEY", model="fake-analysis",
        family="analyst", max_output_tokens=2048)), routes=(*base.routes,
        ModelRoute(route_id="analysis", primary="analysis"), ModelRoute(route_id="orchestration", primary="analysis")))
    gateway = ModelGateway(config, secret_resolver=lambda _: "offline-only",
        provider_factory=lambda profile, _: providers[profile.profile_id])
    calls = []

    async def target_process(argv, cwd, **kwargs):
        calls.append((tuple(argv), str(cwd)))
        return await run_build_process(argv, cwd, **kwargs)

    validator = ValidationService(case.manager, case.validator.profile, build_root=case.validator.guard.root,
        oracle=case.validator.oracle, generator="Visual Studio 17 2022", architecture="x64")
    monkeypatch.setattr("forgepilot.validation.run_build_process", target_process)
    app = create_app(lambda trace: None, store=case.events,
        mission_factory=lambda events: DeliveryWorkflow(events, case.manager, gateway, validator=validator))
    selection = {"repository_path": str(case.root), "baseline_paths": ["README.txt"],
        "configure_preset": "local", "build_preset": "compile", "test_preset": "verify"}
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                preview = await client.post("/api/v2/missions/preview", json=selection)
                assert preview.status_code == 200, preview.text
                reviewed = preview.json()
                assert reviewed["base_commit"] == head
                assert "User-confirmed baseline documentation" in reviewed["patch"]
                assert calls == []
                created = await client.post("/api/v2/missions", json={**selection,
                    "request": "Fix negative input behavior", "task_type": "bug_fix",
                    "preview_token": reviewed["preview_token"], "baseline_confirmed": True})
                assert created.status_code == 202, created.text
                task = created.json()["task_id"]
                path = "/api/v2/missions/" + task
                waiting = await poll(client, path, {"awaiting_human", "failed", "retryable"})
                graph = (await client.get(path + "/graph")).json()
                assert waiting["status"] == "awaiting_human", graph
                assert calls == []
                worktree = case.manager.record(task)["worktree_path"]
                assert (Path(worktree) / "README.txt").read_bytes() == documentation.read_bytes()
                assert not (Path(worktree) / "out").exists()
                review = waiting["review"]
                decision = await client.post(path + "/approvals", json={
                    "expected_revision": waiting["current_revision"], "decision": "approved" if approve else "rejected",
                    **{field: review[field] for field in ("task_spec_hash", "plan_hash", "scope_hash")}})
                assert decision.status_code in {200, 202}, decision.text
                terminal = await poll(client, path, {"succeeded", "failed", "retryable", "cancelled"})
                graph = (await client.get(path + "/graph")).json()
                if approve:
                    assert terminal["status"] == "succeeded", graph
                    validation = (await client.get(path + "/validation")).json()["latest"]["content"]
                    assert validation["status"] == "passed"
                    assert validation["tests_passed"] > 0
                    assert {item["kind"] for item in validation["commands"]} >= {"configure", "build", "test"}
                    assert calls and not any("--preset" in argv for argv, _ in calls)
                    assert not (Path(worktree) / "out").exists()
                    changes = (await client.get(path + "/diff")).json()["change_sets"][-1]["content"]
                    assert "README.txt" not in {item["path"] for item in changes["files"]}
                    nodes = {item["node_id"]: item for item in graph["nodes"]}
                    assert nodes["baseline_capture"]["status"] == "succeeded"
                else:
                    assert terminal["status"] == "cancelled", graph
                    assert calls == []
                assert documentation.read_text(encoding="utf-8") == "User-confirmed baseline documentation\n"
                assert (case.root / "src/value.cpp").read_bytes() == original_source
                assert (case.root / ".git/index").read_bytes() == source_index
                assert git(git_exe, case.root, "status", "--porcelain=v1") == source_status
    finally:
        await gateway.aclose()
        await case.close()
