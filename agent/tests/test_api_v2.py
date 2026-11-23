"""HTTP boundary tests: no arbitrary authority, commands, or secret echoes."""
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from forgepilot.api import create_app
from forgepilot.api_v2 import MAX_MISSION_BODY
from forgepilot.errors import ForgePilotError


@asynccontextmanager
async def api():
    app = create_app(lambda trace: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            yield app, client
    finally:
        app.state.task_host.store.close()


def request_body(**updates):
    return {"repository_path": "D:/Projects/Example", "request": "Fix the worker shutdown", **updates}


@pytest.mark.asyncio
async def test_mission_rejects_malformed_skill_selection_without_echoing_content():
    skill = {"skill_id": str(uuid4()), "version": 1, "content_sha256": "a" * 64}
    invalid = [
        None, skill, [skill, {**skill, "version": 2}], [skill] * 3,
        [{**skill, "body": "private-text"}], [{**skill, "content_sha256": "private-text"}],
        [{**skill, "version": True}], [{**skill, "version": "1"}], [{"skill_id": skill["skill_id"]}],
    ]
    async with api() as (_, client):
        for selection in invalid:
            response = await client.post("/api/v2/missions", json=request_body(selected_skills=selection))
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_skill_selection"
            assert "private-text" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"request": " "}, {"request": "x" * 8193}, {"request": 1},
    {"request": "hello\x00"}, {"repository_path": "relative"},
    {"repository_path": "//remote/share"}, {"repository_path": "D:/repo/../private"},
    {"repository_path": "D:/repo\x00"}, {"repository_path": "D:/repo:file"},
    {"task_type": "shell"}, {"orchestration_mode": "legacy"},
    {"base_ref": "--upload-pack=secret"}, {"base_ref": "HEAD\nsecret"},
    {"toolchain_profile": {"command": "cmd /c secret"}},
    {"tools": ["write_file"]}, {"approved": True}, {"api_key": "secret"},
    {"graph": {}}, {"env": {"TOKEN": "secret"}},
    {"mcp_server_ids": ["not-a-server"]}, {"mcp_server_ids": [str(uuid4())] * 9},
    {"language": "en-US\nignore policy"}, {"memory": "chat history"},
], ids=["blank", "long", "numeric", "nul", "relative", "unc", "traversal", "path-nul", "ads",
        "task-type", "mode", "ref-option", "ref-newline", "toolchain", "tools", "approval", "credential", "graph", "env",
        "mcp-uuid", "mcp-cap", "language-injection", "chat-memory"])
async def test_v2_create_rejects_untrusted_fields_and_values(updates):
    async with api() as (_, client):
        result = await client.post("/api/v2/missions", json=request_body(**updates))
        assert result.status_code == 422
        assert result.json()["error"] == {"code": "invalid_request",
            "message": "Request does not match the v2 schema", "details": [], "task_id": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ('{"request":"secret","request":"other"}', 422),
    ('{"value": NaN}', 422), ('{"value": Infinity}', 422),
    ('["secret"]', 422), ('{"secret":', 422),
    ('{"request":"' + 'x' * MAX_MISSION_BODY + '"}', 413),
], ids=["duplicate", "nan", "infinity", "array", "malformed", "oversized"])
async def test_v2_raw_body_guard(body, status):
    async with api() as (_, client):
        response = await client.post("/api/v2/missions", content=body,
                                     headers={"Content-Type": "application/json"})
        assert response.status_code == status
        assert "secret" not in response.text


@pytest.mark.asyncio
async def test_v2_rejects_unpaired_unicode_surrogates():
    async with api() as (_, client):
        response = await client.post("/api/v2/missions", content=(
            '{"repository_path":"D:/Projects/Example","request":"\\ud800"}'),
            headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert response.json()["error"]["details"] == []


@pytest.mark.asyncio
async def test_v2_chunked_size_limit_and_media_type():
    async def chunks():
        for _ in range(5):
            yield b"x" * 16384
    async with api() as (_, client):
        result = await client.post("/api/v2/missions", content=chunks(), headers={"Content-Type": "application/json"})
        assert result.status_code == 413
        result = await client.post("/api/v2/missions", content="{}")
        assert result.status_code == 415
        result = await client.post("/api/v2/missions", json=request_body(), headers={"Content-Encoding": "gzip"})
        assert result.status_code == 415


@pytest.mark.asyncio
async def test_v2_approval_requires_all_frozen_fields_and_strict_revision():
    body = {"approval_type": "execution", "expected_revision": 8, "task_spec_hash": "a" * 64,
            "plan_hash": "b" * 64, "scope_hash": "c" * 64, "decision": "approved"}
    async with api() as (_, client):
        path = f"/api/v2/missions/{uuid4()}/approvals"
        for field in ("expected_revision", "task_spec_hash", "plan_hash", "scope_hash", "decision"):
            result = await client.post(path, json={key: value for key, value in body.items() if key != field})
            assert result.status_code == 422
        for revision in (True, "8", 0, -1, 2**63):
            result = await client.post(path, json={**body, "expected_revision": revision})
            assert result.status_code == 422
        result = await client.post(path, json={**body, "approved_by": "admin"})
        assert result.status_code == 422


@pytest.mark.asyncio
async def test_v2_permissions_and_stable_conflicts(monkeypatch):
    async with api() as (app, client):
        result = await client.post("/api/v2/missions", json=request_body(), headers={"Origin": "https://evil.invalid"})
        assert result.status_code == 403 and result.json()["error"]["code"] == "origin_not_allowed"
        result = await client.get("/api/v2/missions/anything", headers={"Host": "evil.invalid"})
        assert result.status_code == 400
        async def denied(**kwargs):
            raise ForgePilotError("permission_denied", "Mission permission denied")
        monkeypatch.setattr(app.state.mission_host, "create", denied)
        result = await client.post("/api/v2/missions", json=request_body())
        assert result.status_code == 403
        async def conflict(*args, **kwargs):
            raise ForgePilotError("approval_conflict", "TaskSpec, Plan, Scope or revision changed")
        monkeypatch.setattr(app.state.mission_host, "approve", conflict)
        result = await client.post(f"/api/v2/missions/{uuid4()}/approvals", json={
            "expected_revision": 1, "task_spec_hash": "a" * 64, "plan_hash": "b" * 64,
            "scope_hash": "c" * 64, "decision": "approved"})
        assert result.status_code == 409 and result.json()["error"]["code"] == "approval_conflict"


@pytest.mark.asyncio
async def test_v2_control_bodies_and_paths_are_strict():
    async with api() as (_, client):
        for control in ("cancel", "retry", "resume", "delivery"):
            result = await client.post(f"/api/v2/missions/{uuid4()}/{control}", json={"expected_revision": 1, "command": "secret"})
            assert result.status_code == 422
            result = await client.post(f"/api/v2/missions/{uuid4()}/{control}", json={})
            assert result.status_code == 422
        for path in ("bad", str(uuid4()) + "/artifacts/bad"):
            result = await client.get("/api/v2/missions/" + path)
            assert result.status_code == 422
