"""Cross the HTTP, graph, model, repair and delivery boundaries in one offline run.

Focused fault tests for each boundary live in their respective module suites. This
case verifies that those boundaries compose when the first real build fails.
"""
from __future__ import annotations

import httpx
import pytest

from closed_loop_support import COMPILE_BROKEN, FIXED, ORIGINAL, make_loop_case, patch, done
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.validation import ValidationService
from test_mission_api_workflow import AnalysisProvider, frames, poll


@pytest.mark.asyncio
async def test_http_dual_analyst_join_approval_real_build_repair_review_delivery(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe,
        implement=[patch(after=COMPILE_BROKEN), done()],
        repair=[patch(COMPILE_BROKEN, FIXED), done()])
    analysis = AnalysisProvider(case)
    providers = {**case.providers, "analysis": analysis}
    base = case.gateway.router.configuration
    config = ModelConfiguration(profiles=(*base.profiles, ModelProfile(
        profile_id="analysis", base_url="http://127.0.0.1:1/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model="fake-analysis",
        family="analyst", max_output_tokens=2048)), routes=(*base.routes,
        ModelRoute(route_id="analysis", primary="analysis"),
        ModelRoute(route_id="orchestration", primary="analysis")))
    gateway = ModelGateway(config, secret_resolver=lambda _: "offline-only",
                           provider_factory=lambda profile, _: providers[profile.profile_id])
    validator = ValidationService(case.manager, case.validator.profile,
        build_root=case.validator.guard.root, oracle=case.validator.oracle,
        generator="Visual Studio 17 2022", architecture="x64")
    app = create_app(lambda trace: None, store=case.events,
                     mission_factory=lambda events: DeliveryWorkflow(
                         events, case.manager, gateway, validator=validator))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v2/missions", json={
                    "repository_path": str(case.root), "request": "Fix negative input behavior",
                    "task_type": "bug_fix"})
                assert created.status_code == 202, created.text
                path = f"/api/v2/missions/{created.json()['task_id']}"
                waiting = await poll(client, path, {"awaiting_human", "failed", "retryable"})
                assert waiting["status"] == "awaiting_human"
                before = (await client.get(path + "/graph")).json()
                states = {node["node_id"]: node["status"] for node in before["nodes"]}
                assert before["graph_id"] == "task-analysis-delivery.v3"
                assert all(states[name] == "succeeded" for name in (
                    "draft_task_spec", "analyst_fanout", "root_cause_analysis",
                    "impact_test_analysis", "join_evidence", "generate_plan"))
                assert states["execution_approval"] == "awaiting_human"
                artifacts = (await client.get(path + "/artifacts")).json()["items"]
                evidence = {item["artifact_id"]: item for item in artifacts
                            if item["artifact_type"] == "EvidenceBundle"}
                joined = next(item for item in evidence.values()
                              if item["producer_node"] == "join_evidence")
                assert {evidence[parent]["producer_node"] for parent in
                        joined["parent_artifact_ids"]} == {
                            "root_cause_analysis", "impact_test_analysis"}

                review = waiting["review"]
                approved = await client.post(path + "/approvals", json={
                    "expected_revision": waiting["current_revision"], "decision": "approved",
                    **{field: review[field] for field in (
                        "task_spec_hash", "plan_hash", "scope_hash")}})
                assert approved.status_code == 202, approved.text
                finished = await poll(client, path, {"succeeded", "failed", "retryable"})
                assert finished["status"] == "succeeded", (await client.get(path + "/graph")).json()
                after = (await client.get(path + "/graph")).json()
                states = {node["node_id"]: node["status"] for node in after["nodes"]}
                assert all(states[name] == "succeeded" for name in (
                    "execution_approval", "implementer", "validation_0", "repair_1",
                    "validation_1", "reviewer_1", "delivery_bundle"))
                assert states["reviewer_0"] == "skipped"
                artifacts = (await client.get(path + "/artifacts")).json()["items"]
                reports = [item for item in artifacts if item["artifact_type"] == "ValidationReport"]
                assert [item["content"]["failure_classification"] for item in reports] == [
                    "code_failure", "none"]
                assert reports[1]["content"]["status"] == "passed"
                bundle = next(item for item in artifacts if item["artifact_type"] == "DeliveryBundle")
                assert bundle["content"]["validation_report_artifact_id"] == reports[1]["artifact_id"]
                assert len(case.providers["repair"].seen) == 2
                assert len(case.providers["review"].seen) == 1
                assert (case.root / "src/value.cpp").read_text(encoding="utf-8") == ORIGINAL

                all_events = frames((await client.get(path + "/events")).text)
                replay = frames((await client.get(path + "/events", headers={
                    "Last-Event-ID": all_events[len(all_events) // 2][0]})).text)
                assert replay == all_events[len(all_events) // 2 + 1:]
                preview = await client.post(path + "/delivery", json={
                    "expected_revision": finished["current_revision"]})
                assert preview.status_code == 200, preview.text
                delivered = await client.post(path + "/delivery", json={
                    "expected_revision": preview.json()["mission"]["current_revision"],
                    "mode": "patch", "token": preview.json()["delivery"]["token"]})
                assert delivered.status_code == 200, delivered.text
                assert delivered.json()["delivery"]["mode"] == "patch"
    finally:
        await gateway.aclose()
        await case.gateway.aclose()
        case.events.close()
