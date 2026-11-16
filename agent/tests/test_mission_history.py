"""Durable Mission discovery is stable, filtered and never execution authority."""
import json
import os
from uuid import UUID

import httpx
import pytest

from forgepilot.api import create_app
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration import GraphDefinition, NodeDefinition, NodeType, validate_graph
from forgepilot.orchestration.store import GraphStore
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.workspace_manager import WorkspaceManager
from test_mission_events import make_mission
from test_mission_service import ControlledWorkflow, settled
from test_workspace_manager import committed_repo


def identity(number):
    return str(UUID(int=number, version=4))


def seed(store, number, *, repository="D:/Projects/Example", status="succeeded", day=1):
    task_id = identity(number)
    make_mission(store, task_id)
    stamp = f"2026-11-{day:02d}T08:00:00Z"
    with store._transaction():
        store._db.execute("UPDATE task_graph_runs SET created_at=?,updated_at=?,status=? WHERE task_id=?",
                          (stamp, stamp, status, task_id))
        store._db.execute("UPDATE graph_events SET payload_json=? WHERE task_id=? AND event_type='task.created'",
            (json.dumps(dict(repository_path=repository, request=f"Task {number}: fixture-secret",
                task_type="bug_fix", orchestration_mode="multi_agent", base_ref="HEAD",
                toolchain_profile="auto")), task_id))
    return task_id


def test_keyset_pages_have_no_gaps_or_duplicates_across_ties_and_anchor_deletion(tmp_path):
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    try:
        expected = [seed(store, number, day=1 + number // 8) for number in range(1, 24)][::-1]
        first = store.missions.list_page(5)
        assert first["total"] == len(expected)
        assert [item["task_id"] for item in first["items"]] == expected[:5]
        # Updating a live status does not move its immutable ordering key.
        store._db.execute("UPDATE task_graph_runs SET status='failed',updated_at='2099-01-01T00:00:00Z' WHERE task_id=?",
                          (expected[8],))
        # A keyset cursor survives deletion of its anchor; no row lookup is needed.
        store._db.execute("DELETE FROM task_graph_runs WHERE task_id=?", (expected[4],))
        newer = seed(store, 50, day=12)
        found = [item["task_id"] for item in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            page = store.missions.list_page(5, cursor)
            found.extend(item["task_id"] for item in page["items"])
            cursor = page["next_cursor"]
        assert found == expected
        assert len(found) == len(set(found))
        assert store.missions.list_page(5)["items"][0]["task_id"] == newer
    finally:
        store.close()


def test_repository_status_filters_are_bound_to_cursor_and_exclude_internal_graphs(tmp_path):
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    try:
        wanted = [seed(store, number, repository="D:\\Projects\\Example", status="failed") for number in range(1, 8)][::-1]
        seed(store, 20, repository="D:/Projects/Other", status="failed")
        seed(store, 21, status="succeeded")
        graph = GraphDefinition("internal-only", 1, (NodeDefinition("work", NodeType.DETERMINISTIC, "work"),))
        GraphStore(store).create_run(identity(99), validate_graph(graph))
        query = dict(repository_path="d:/projects/example" if os.name == "nt" else "D:/Projects/Example", status="failed")
        first = store.missions.list_page(3, **query)
        assert first["total"] == 7
        second = store.missions.list_page(3, first["next_cursor"], **query)
        third = store.missions.list_page(3, second["next_cursor"], **query)
        assert [item["task_id"] for page in (first, second, third) for item in page["items"]] == wanted
        assert third["next_cursor"] is None
        assert store.missions.list_page()["total"] == 9
        for changed in ({"status": "succeeded"}, {"repository_path": "D:/Projects/Other"}):
            with pytest.raises(ForgePilotError, match="invalid_page"):
                store.missions.list_page(3, first["next_cursor"], **(query | changed))
        for cursor in ("unknown", "p1.invalid", "m1.bad.p1.invalid"):
            with pytest.raises(ForgePilotError, match="invalid_page"):
                store.missions.list_page(3, cursor)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reopened_missions_are_discoverable_and_readable_without_workflow_or_resume(tmp_path):
    database = tmp_path / "history.sqlite3"
    artifact_root = tmp_path / "artifacts"
    store = SqliteEventStore(database, artifact_root=artifact_root)
    task_id = seed(store, 1, status="interrupted")
    store.close()
    store = SqliteEventStore(database, artifact_root=artifact_root, redactor=Redactor(("fixture-secret",)))
    app = create_app(lambda trace: None, store=store)  # No model or Mission workflow configured.
    try:
        before = store._db.total_changes
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.get("/api/v2/missions", params={"repository_path": "D:/Projects/Example", "status": "interrupted"})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == 1 and page["next_cursor"] is None
            assert page["items"][0]["task_id"] == task_id
            assert page["items"][0]["recovery_required"] is True
            assert "fixture-secret" not in response.text
            for suffix in ("", "/graph", "/artifacts", "/diff", "/validation"):
                detail = await client.get("/api/v2/missions/" + task_id + suffix)
                assert detail.status_code == 200, detail.text
            detail = (await client.get("/api/v2/missions/" + task_id)).json()
            assert detail["status"] == "interrupted" and detail["recovery_required"]
        assert store._db.total_changes == before
        assert app.state.mission_host._services == {}
        assert app.state.mission_host._running == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_created_http_mission_is_found_after_backend_restart_without_model_configuration(tmp_path, git_exe, committed_repo):
    database = tmp_path / "history.sqlite3"
    artifact_root = tmp_path / "artifacts"
    workspace = WorkspaceManager(tmp_path / "workspaces", git_executable=git_exe)
    store = SqliteEventStore(database, artifact_root=artifact_root, workspace_backend=workspace)
    app = create_app(lambda trace: None, store=store,
        mission_factory=lambda events: ControlledWorkflow(events, workspace))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.post("/api/v2/missions", json={
                    "repository_path": str(committed_repo), "request": "Offline restart discovery"})
                assert response.status_code == 202, response.text
                task_id = response.json()["task_id"]
                mission = await settled(app.state.mission_host, task_id)
                assert mission["status"] == "awaiting_human"
    finally:
        store.close()
    reopened = SqliteEventStore(database, artifact_root=artifact_root)
    app = create_app(lambda trace: None, store=reopened)
    try:
        async with app.router.lifespan_context(app):
            before = reopened._db.total_changes
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                page = (await client.get("/api/v2/missions")).json()
                assert [item["task_id"] for item in page["items"]] == [task_id]
                assert page["items"][0]["recovery_required"]
                response = await client.get("/api/v2/missions/" + task_id)
                assert response.status_code == 200, response.text
                detail = response.json()
                assert detail["status"] == "awaiting_human" and detail["recovery_required"]
                for key in ("task_spec_artifact_id", "task_spec_hash", "plan_artifact_id", "plan_hash", "scope_hash"):
                    assert detail["review"][key] == mission["review"][key]
                assert detail["current_revision"] > mission["current_revision"]  # Existing startup recovery event.
                assert detail["review"]["revision"] == detail["current_revision"]
                assert detail["review"]["review_hash"] != mission["review"]["review_hash"]
            assert reopened._db.total_changes == before
            assert app.state.mission_host._services == {}
            assert app.state.mission_host._running == {}
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_history_query_boundaries_reject_invalid_values_without_echo(tmp_path):
    store = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    app = create_app(lambda trace: None, store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/v2/missions")).json() == {"items": [], "total": 0, "next_cursor": None}
            for query, status in (({"limit": 0}, 422), ({"limit": 101}, 422),
                    ({"status": "fixture-secret"}, 422), ({"before": "fixture-secret"}, 400),
                    ({"before": ""}, 422), ({"before": "x" * 513}, 422),
                    ({"repository_path": "fixture-secret"}, 422), ({"repository_path": "D:/repo/../secret"}, 422),
                    ({"repository_path": "//host/fixture-secret"}, 422)):
                response = await client.get("/api/v2/missions", params=query)
                assert response.status_code == status, response.text
                assert "fixture-secret" not in response.text
    finally:
        store.close()
