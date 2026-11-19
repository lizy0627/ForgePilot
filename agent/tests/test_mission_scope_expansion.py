"""Exercise writer scope expansion over HTTP with real Git, CMake, CTest and oracle."""
from contextlib import asynccontextmanager
import json
from pathlib import Path

import httpx
import pytest

from closed_loop_support import ORIGINAL, create, done, make_loop_case, patch, response
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.validation import ValidationService
from forgepilot.workspace_manager import WorkspaceManager
from test_mission_api_workflow import AnalysisProvider, frames, poll
from test_workspace_manager import git


HELPER = "clamp.hpp"
HEADER = "inline int clamp_input(int n) { return n < 0 ? 0 : n; }\n"
USES_HELPER = '#include "../clamp.hpp"\nint value(int input) { return clamp_input(input); }\n'
PARTIAL = "// awaiting approval for header\n"
REQUEST = {"path": HELPER, "reason": "Share the clamp implementation via this header",
           "plan_step_id": "STEP-1", "acceptance_criteria_ids": ["AC-1"]}
HASHES = ("task_spec_hash", "plan_hash", "scope_hash")


def request_scope():
    return response(json.dumps({"status": "awaiting_human", "summary": "Need the shared header",
                                "scope_expansion": {"files": [REQUEST]}}))


@asynccontextmanager
async def server(case, tmp_path, git_exe, *, restart=False):
    # Reopen all durable state and use fresh service, validator and model budget
    # instances. Only the offline providers' queues represent the external model.
    manager = WorkspaceManager(tmp_path / "host data", git_executable=git_exe) if restart else case.manager
    store = (SqliteEventStore(tmp_path / "closed-loop.sqlite3", workspace_backend=manager,
                             artifact_root=tmp_path / "artifact store") if restart else case.events)
    base = case.gateway.router.configuration
    analyst = ModelProfile(profile_id="analysis", base_url="http://127.0.0.1:1/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model="fake-analysis", family="analyst")
    config = ModelConfiguration(profiles=(*base.profiles, analyst), routes=(*base.routes,
        ModelRoute(route_id="analysis", primary="analysis"), ModelRoute(route_id="orchestration", primary="analysis")))
    providers = {**case.providers, "analysis": AnalysisProvider(case)}
    gateway = ModelGateway(config, secret_resolver=lambda _: "offline-only",
        provider_factory=lambda profile, _: providers[profile.profile_id])
    validator = ValidationService(manager, case.validator.profile, build_root=case.validator.guard.root,
        oracle=case.validator.oracle, generator="Visual Studio 17 2022", architecture="x64")
    app = create_app(lambda trace: None, store=store, git_executable=git_exe,
        mission_factory=lambda events: DeliveryWorkflow(events, manager, gateway, validator=validator))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                yield client
    finally:
        await gateway.aclose()
        store.close()


async def mission_at(client, path, status):
    mission = await poll(client, path, {"awaiting_human", "retryable", "failed", "cancelled", "succeeded"})
    graph = (await client.get(path + "/graph")).json()
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    assert mission["status"] == status, {key: node["result"] for key, node in nodes.items() if node["result"]}
    return mission, nodes


async def begin_writer(client, case):
    created = await client.post("/api/v2/missions", json={"repository_path": str(case.root),
        "request": "Clamp negative inputs via a shared header", "task_type": "bug_fix"})
    assert created.status_code == 202, created.text
    path = "/api/v2/missions/" + created.json()["task_id"]
    mission, _ = await mission_at(client, path, "awaiting_human")
    review = mission["review"]
    decision = await client.post(path + "/approvals", json={"decision": "approved",
        "expected_revision": mission["current_revision"], **{key: review[key] for key in HASHES}})
    assert decision.status_code == 202, decision.text
    return path, review


async def pending(client, path, writer):
    mission, nodes = await mission_at(client, path, "awaiting_human")
    assert nodes[writer]["status"] == "awaiting_human", nodes[writer]
    result = await client.get(path + "/scope-expansion")
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview["files"] == [REQUEST]
    assert (preview["node_id"], preview["attempt"]) == (writer, 1)
    assert preview["checkpoint_id"] and "checkpoint" in preview["replay_behavior"].lower()
    assert preview["revision"] == mission["current_revision"]
    return preview, nodes


def decide(preview, decision):
    return {"approval_type": "scope_expansion", "decision": decision,
            "expected_revision": preview["revision"], **{key: preview[key] for key in HASHES}}


async def deliver(client, path, case, preview, original):
    confirmation = decide(preview, "approved")
    conflict = await client.post(path + "/approvals", json={**confirmation, "scope_hash": "0" * 64})
    assert conflict.status_code == 409, conflict.text
    accepted = await client.post(path + "/approvals", json=confirmation)
    assert accepted.status_code == 202, accepted.text
    mission, nodes = await mission_at(client, path, "succeeded")
    duplicate = await client.post(path + "/approvals", json=confirmation)
    assert duplicate.status_code == 409, duplicate.text
    duplicate = await client.post(path + "/approvals", json={
        **confirmation, "expected_revision": mission["current_revision"]})
    assert duplicate.status_code == 409, duplicate.text
    assert mission["task_id"] == path.rsplit("/", 1)[1]
    assert nodes[preview["node_id"]]["attempt"] == 2
    assert nodes["baseline_capture"]["attempt"] == 1

    artifacts = (await client.get(path + "/artifacts")).json()["items"]
    by_id = {a["artifact_id"]: a for a in artifacts}
    bundle, = [a["content"] for a in artifacts if a["artifact_type"] == "DeliveryBundle"]
    changes = by_id[bundle["change_set_artifact_id"]]
    if preview["partial_change_set_artifact_id"]:
        partial_changes = by_id[preview["partial_change_set_artifact_id"]]["content"]["files"]
        assert "".join(item["unified_diff"] for item in partial_changes) == preview["partial_patch"]
    validation = by_id[bundle["validation_report_artifact_id"]]["content"]
    review = by_id[bundle["review_report_artifact_id"]]
    assert validation["tests_passed"] > 0 and validation["hidden_oracle"] == "passed"
    assert {"configure", "build", "test"} <= {c["kind"] for c in validation["commands"]}
    assert review["content"]["recommend_delivery"] and changes["artifact_id"] in review["parent_artifact_ids"]
    plan = by_id[changes["content"]["execution_plan_artifact_id"]]
    spec = by_id[plan["content"]["task_spec_artifact_id"]]
    assert spec["artifact_id"] in review["parent_artifact_ids"]
    assert plan["content_sha256"] == preview["plan_hash"] != original["plan_hash"]
    assert spec["content_sha256"] == preview["task_spec_hash"] != original["task_spec_hash"]
    old_plan = by_id[original["plan_artifact_id"]]["content"]
    assert HELPER not in old_plan["steps"][0]["expected_files"]
    assert HELPER in plan["content"]["steps"][0]["expected_files"]
    assert plan["content"]["steps"][0]["file_criteria"] == [
        {key: REQUEST[key] for key in ("path", "reason", "acceptance_criteria_ids")}]
    assert {item["path"] for item in changes["content"]["files"]} == {"src/value.cpp", HELPER}
    final_patch = (await client.get(path + "/diff")).json()["patch"]
    assert "clamp_input(input)" in final_patch and HEADER.strip() in final_patch and PARTIAL.strip() not in final_patch

    package = await client.post(path + "/delivery", json={"expected_revision": mission["current_revision"]})
    assert package.status_code == 200, package.text
    package = package.json()
    exported = await client.post(path + "/delivery", json={"expected_revision": package["mission"]["current_revision"],
        "mode": "patch", "token": package["delivery"]["token"]})
    assert exported.status_code == 200, exported.text
    assert exported.json()["delivery"]["mode"] == "patch"
    events = frames((await client.get(path + "/events")).text)
    requested, = [event for _, event in events if event["event_type"] == "approval.required"
                  and event["data"].get("approval_type") == "scope_expansion"]
    assert requested["node_id"] == preview["node_id"]
    for key in ("checkpoint_id", "partial_change_set_artifact_id", "attempt"):
        assert requested["data"][key] == preview[key]
    assert (case.root / "src/value.cpp").read_bytes() == ORIGINAL.encode()
    assert not (case.root / HELPER).exists()
    return nodes


@pytest.mark.asyncio
async def test_implementer_requests_before_writing_and_delivers(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe, implement=[
        request_scope(), create(HELPER, HEADER), patch(after=USES_HELPER), done()])
    try:
        async with server(case, tmp_path, git_exe) as client:
            path, original = await begin_writer(client, case)
            preview, _ = await pending(client, path, "implementer")
            assert preview["partial_change_set_artifact_id"] is None and preview["partial_patch"] == ""
            await deliver(client, path, case, preview, original)
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
async def test_repair_partial_replayed_after_restart_with_original_budget_and_baseline(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe, implement=[patch(after=USES_HELPER), done()],
        repair=[patch(USES_HELPER, PARTIAL + USES_HELPER), request_scope(), create(HELPER, HEADER), done()])
    try:
        async with server(case, tmp_path, git_exe) as client:
            path, original = await begin_writer(client, case)
            preview, before = await pending(client, path, "repair_1")
            assert preview["partial_change_set_artifact_id"] and PARTIAL.strip() in preview["partial_patch"]
            task_id = path.rsplit("/", 1)[1]
            integrity = case.validator.guard.root / task_id / "integrity.json"
            first_baseline = integrity.read_bytes()
        async with server(case, tmp_path, git_exe, restart=True) as client:
            persisted, _ = await pending(client, path, "repair_1")
            assert persisted["revision"] > preview["revision"]
            assert persisted == {**preview, "revision": persisted["revision"]}
            stale = await client.post(path + "/approvals", json=decide(preview, "approved"))
            assert stale.status_code == 409, stale.text
            after = await deliver(client, path, case, persisted, original)
            assert after["implementer"] == before["implementer"]
            assert after["validation_0"] == before["validation_0"]
            assert after["repair_2"]["attempt"] == 0
            assert after["repair_1"]["result"]["metrics"]["model_calls"] == 4
            assert after["repair_1"]["result"]["metrics"]["tool_calls"] == 2
            assert integrity.read_bytes() == first_baseline
            assert git(git_exe, case.root, "status", "--porcelain") == b""
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
async def test_reject_preserves_partial_and_cannot_be_confirmed_again(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe, implement=[patch(after=PARTIAL + ORIGINAL), request_scope()])
    try:
        async with server(case, tmp_path, git_exe) as client:
            path, _ = await begin_writer(client, case)
            preview, _ = await pending(client, path, "implementer")
            assert PARTIAL.strip() in preview["partial_patch"]
            rejected = await client.post(path + "/approvals", json=decide(preview, "rejected"))
            assert rejected.status_code in {200, 202}, rejected.text
            current, nodes = await mission_at(client, path, "awaiting_human")
            assert nodes["implementer"]["attempt"] == 1
            retained = (await client.get(path + "/scope-expansion")).json()
            assert retained["status"] == "rejected"
            assert retained["partial_change_set_artifact_id"] == preview["partial_change_set_artifact_id"]
            assert retained["partial_patch"] == preview["partial_patch"]
            for decision in ("rejected", "approved"):
                confirmation = decide(preview, decision) | {"expected_revision": current["current_revision"]}
                duplicate = await client.post(path + "/approvals", json=confirmation)
                assert duplicate.status_code == 409, duplicate.text
            assert len(case.providers["implement"].seen) == 2
            assert not (case.root / HELPER).exists()
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("free_text", [True, False], ids=["legacy-free-text", "denied-write"])
async def test_only_structured_voluntary_requests_allow_scope_review(tmp_path, git_exe, free_text):
    summary = f"Please allow {HELPER} for STEP-1 and AC-1"
    output = (response(json.dumps({"status": "awaiting_human", "summary": summary})) if free_text
              else create(HELPER, HEADER))
    case = await make_loop_case(tmp_path, git_exe, implement=[output])
    try:
        async with server(case, tmp_path, git_exe) as client:
            path, review = await begin_writer(client, case)
            mission, nodes = await mission_at(client, path, "awaiting_human" if free_text else "failed")
            if free_text:
                assert nodes["implementer"]["result"]["decision_summary"] == summary
            assert (await client.get(path + "/scope-expansion")).status_code == 404
            confirmation = {"expected_revision": mission["current_revision"], "decision": "approved",
                            "approval_type": "scope_expansion", **{name: review[name] for name in HASHES}}
            refused = await client.post(path + "/approvals", json=confirmation)
            assert refused.status_code in {404, 409}
            worktree = case.manager.record(mission["task_id"])["worktree_path"]
            assert not (Path(worktree) / HELPER).exists()
    finally:
        await case.gateway.aclose()
