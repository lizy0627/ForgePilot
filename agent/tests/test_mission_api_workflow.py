"""API v2 creates, reviews, validates and delivers through the real backend."""
import asyncio
from hashlib import sha256
import json

import httpx
import pytest

from closed_loop_support import ORIGINAL, make_loop_case
from conftest import reply
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.api import create_app
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.validation import ValidationService
from forgepilot.workspace_manager import WorkspaceManager
from test_mission_service import ControlledWorkflow
from test_workspace_manager import committed_repo


def frames(text):
    events = []
    for frame in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "data" in lines:
            payload = json.loads(lines["data"])
            assert payload["event_type"] == lines["event"]
            assert payload["event_id"] == lines["id"]
            events.append((lines["id"], payload))
    return events


class AnalysisProvider:
    def __init__(self, case):
        self.case = case

    async def chat(self, messages, tools=None):
        payload = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
        if "request" in payload:
            result = self.case.spec.content.model_dump(mode="json")
            result["repository"] = payload["repository"]
            result["data_policy"] = payload["data_policy"]
        elif "evidence" in payload:
            result = self.case.plan.content.model_dump(mode="json")
            result["task_spec_artifact_id"] = payload["task_spec"]["artifact_id"]
        else:
            result = {"items": [{"evidence_id": "EV-1", "claim": "Negative input is returned unchanged",
                "reason": "The value function returns its input", "confidence": 0.9,
                "path": "src/value.cpp", "start_line": 1, "end_line": 1, "symbol": "value",
                "file_hash": sha256(ORIGINAL.encode()).hexdigest(), "source": "local_repository"}],
                "summary": "The boundary behavior needs a regression fix"}
        return reply(json.dumps(result), usage={"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40})

    async def aclose(self):
        pass


async def poll(client, path, statuses):
    for _ in range(6000):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        mission = response.json()
        if mission["status"] in statuses:
            # The service also owns cleanup after the terminal/gate transaction.
            await asyncio.sleep(.03)
            return (await client.get(path)).json()
        await asyncio.sleep(.01)
    pytest.fail("Mission did not settle within 60 seconds")


@pytest.mark.asyncio
async def test_http_mission_analysis_approval_validation_delivery_and_sse(tmp_path, git_exe):
    case = await make_loop_case(tmp_path, git_exe)
    base = case.gateway.router.configuration
    providers = {**case.providers, "analysis": AnalysisProvider(case)}
    config = ModelConfiguration(profiles=(*base.profiles, ModelProfile(profile_id="analysis",
        base_url="http://127.0.0.1:1/v1", api_key_ref="env:FORGEPILOT_API_KEY", model="fake-analysis",
        family="analyst", max_output_tokens=2048)), routes=(*base.routes,
        ModelRoute(route_id="analysis", primary="analysis"), ModelRoute(route_id="orchestration", primary="analysis")))
    gateway = ModelGateway(config, secret_resolver=lambda _: "offline-only",
                           provider_factory=lambda profile, _: providers[profile.profile_id])
    validator = ValidationService(case.manager, case.validator.profile, build_root=case.validator.guard.root,
        oracle=case.validator.oracle, generator="Visual Studio 17 2022", architecture="x64")
    def factory(events):
        return DeliveryWorkflow(events, case.manager, gateway, validator=validator)
    app = create_app(lambda trace: None, store=case.events, mission_factory=factory)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.post("/api/v2/missions", json={"repository_path": str(case.root),
                    "request": "Fix negative input behavior", "task_type": "bug_fix"})
                assert response.status_code == 202, response.text
                tid = response.json()["task_id"]
                path = f"/api/v2/missions/{tid}"
                mission = await poll(client, path, {"awaiting_human", "failed", "retryable"})
                assert mission["status"] == "awaiting_human", (await client.get(path + "/graph")).json()
                assert (await client.get(path + "/graph")).json()["graph_id"] == "task-analysis-delivery.v1"
                artifacts = (await client.get(path + "/artifacts")).json()["items"]
                assert {a["artifact_type"] for a in artifacts} == {"TaskSpec", "EvidenceBundle", "ExecutionPlan"}
                assert (await client.get(path + "/artifacts/" + artifacts[0]["artifact_id"])).json() == artifacts[0]
                foreign = await client.get(path + "/artifacts/" + case.spec.artifact_id)
                assert foreign.status_code == 404
                review = mission["review"]
                approval = {"expected_revision": mission["current_revision"], "decision": "approved",
                    **{field: review[field] for field in ("task_spec_hash", "plan_hash", "scope_hash")}}
                conflict = await client.post(path + "/approvals", json={**approval, "plan_hash": "0" * 64})
                assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "mission_approval_conflict"
                response = await client.post(path + "/approvals", json=approval)
                assert response.status_code == 202, response.text
                mission = await poll(client, path, {"succeeded", "failed", "retryable", "cancelled"})
                assert mission["status"] == "succeeded", (await client.get(path + "/graph")).json()
                validation = (await client.get(path + "/validation")).json()["latest"]["content"]
                assert validation["status"] == "passed" and validation["hidden_oracle"] == "passed"
                diff = (await client.get(path + "/diff")).json()["patch"]
                assert "+int value(int input) { return input < 0 ? 0 : input; }" in diff
                preview = await client.post(path + "/delivery", json={"expected_revision": mission["current_revision"]})
                assert preview.status_code == 200, preview.text
                response = await client.post(path + "/delivery", json={
                    "expected_revision": preview.json()["mission"]["current_revision"],
                    "mode": "patch", "token": preview.json()["delivery"]["token"]})
                assert response.status_code == 200, response.text
                assert response.json()["delivery"]["mode"] == "patch"
                assert (case.root / "src/value.cpp").read_text(encoding="utf-8") == ORIGINAL
                stream = frames((await client.get(path + "/events")).text)
                categories = {event["event_type"].split(".")[0] for _, event in stream}
                assert {"task", "node", "artifact", "approval", "model", "tool", "validation", "workspace"} <= categories
                revisions = [event["task_revision"] for _, event in stream]
                assert all(a < b for a, b in zip(revisions, revisions[1:]))
                replay = frames((await client.get(path + "/events", headers={"Last-Event-ID": stream[8][0]})).text)
                assert replay == stream[9:]
                assert (await client.get(path + "/events", headers={"Last-Event-ID": stream[-1][0]})).status_code == 204
    finally:
        await gateway.aclose()
        await case.gateway.aclose()


@pytest.mark.asyncio
async def test_http_recovery_and_delivery_require_fresh_explicit_confirmation(tmp_path, git_exe, committed_repo):
    """Exercise the UI's preview/confirm boundary over HTTP, including stale state."""
    workspace = WorkspaceManager(tmp_path / "workspaces", git_executable=git_exe)
    store = SqliteEventStore(tmp_path / "missions.sqlite3", artifact_root=tmp_path / "artifacts",
                             workspace_backend=workspace)
    app = create_app(lambda trace: None, store=store,
                     mission_factory=lambda events: ControlledWorkflow(events, workspace, pause=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            created = await client.post("/api/v2/missions", json={
                "repository_path": str(committed_repo), "request": "Fix the worker", "task_type": "bug_fix"})
            assert created.status_code == 202, created.text
            path = f"/api/v2/missions/{created.json()['task_id']}"
            waiting = await poll(client, path, {"awaiting_human"})
            assert waiting["review"] is not None
            review = waiting["review"]
            artifacts = (await client.get(path + "/artifacts")).json()["items"]
            assert {item["artifact_type"] for item in artifacts} == {"TaskSpec", "ExecutionPlan"}
            assert review["task_spec_artifact_id"] in {item["artifact_id"] for item in artifacts}
            assert review["plan_artifact_id"] in {item["artifact_id"] for item in artifacts}
            approval = {"expected_revision": waiting["current_revision"], "decision": "approved",
                        **{field: review[field] for field in ("task_spec_hash", "plan_hash", "scope_hash")}}
            stale = await client.post(path + "/approvals", json={**approval, "expected_revision": approval["expected_revision"] - 1})
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "mission_revision_conflict"
            assert (await client.get(path)).json()["current_revision"] == waiting["current_revision"]
            accepted = await client.post(path + "/approvals", json=approval)
            assert accepted.status_code == 202, accepted.text
            paused = await poll(client, path, {"retryable"})
            assert paused["recovery_required"] is False
            assert (await client.get(path + "/validation")).json()["latest"] is None
            assert (await client.get(path + "/diff")).json()["patch"] == ""

            recovery = await client.post(path + "/resume", json={"expected_revision": paused["current_revision"]})
            assert recovery.status_code == 202, recovery.text
            preview = recovery.json()
            assert preview["mission"]["recovery_required"] is True
            assert preview["recovery"]["blocked_reason"] is None
            assert preview["recovery"]["token"]
            wrong = await client.post(path + "/resume", json={
                "expected_revision": preview["mission"]["current_revision"], "recovery_token": "0" * 64})
            assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "graph_conflict"
            assert (await client.get(path)).json()["status"] == "retryable"
            resumed = await client.post(path + "/resume", json={
                "expected_revision": preview["mission"]["current_revision"],
                "recovery_token": preview["recovery"]["token"]})
            assert resumed.status_code == 202, resumed.text
            completed = await poll(client, path, {"succeeded"})
            assert completed["recovery_required"] is False
            assert next(node for node in (await client.get(path + "/graph")).json()["nodes"]
                        if node["node_id"] == "finish")["attempt"] == 2

            delivery = await client.post(path + "/delivery", json={"expected_revision": completed["current_revision"]})
            assert delivery.status_code == 200, delivery.text
            package = delivery.json()
            assert package["delivery"]["token"]
            wrong = await client.post(path + "/delivery", json={
                "expected_revision": package["mission"]["current_revision"],
                "mode": "abandon", "token": "0" * 64})
            assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "delivery_approval"
            assert (await client.get(path)).json()["status"] == "succeeded"
            delivery = await client.post(path + "/delivery", json={
                "expected_revision": (await client.get(path)).json()["current_revision"]})
            assert delivery.status_code == 200, delivery.text
            package = delivery.json()
            delivered = await client.post(path + "/delivery", json={
                "expected_revision": package["mission"]["current_revision"],
                "mode": "abandon", "token": package["delivery"]["token"]})
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["delivery"]["status"] == "abandoned"
            stream = frames((await client.get(path + "/events")).text)
            assert [event["event_type"] for _, event in stream].count("workspace.delivered") == 1
            assert (await client.get(path + "/events", headers={"Last-Event-ID": stream[-1][0]})).status_code == 204
