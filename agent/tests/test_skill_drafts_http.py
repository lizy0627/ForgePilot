"""Author, review and rediscover a local skill using the production loopback routes."""
from contextlib import closing, contextmanager
import time
from uuid import uuid4

import httpx

from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.workspace_manager import WorkspaceManager
from test_api_network import serving
from test_artifact_protocol import TASK_ID, task_spec
from test_mission_events import finish_mission, make_mission
from test_mission_service import ControlledWorkflow, decision
from test_skill_mission_drafts import put
from test_skill_recommendations_http import recommend
from test_workspace_manager import committed_repo


V2 = "/api/v2"


@contextmanager
def local_api(tmp_path, *, workspace=None):
    with closing(SqliteEventStore(tmp_path / "skills.db", artifact_root=tmp_path / "artifacts",
                                  workspace_backend=workspace)) as store:
        workflow = (lambda events: ControlledWorkflow(events, workspace)) if workspace else None
        with serving(lambda trace: None, store=store, mission_factory=workflow, api_token="test-token") as (url, _):
            with httpx.Client(base_url=url, trust_env=False, headers={"X-ForgePilot-Token": "test-token"}) as client:
                yield client, store


def success(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def error(response, status, code):
    assert (response.status_code, response.json()["error"]["code"]) == (status, code), response.text


def test_cancelled_mission_note_to_recommendation_then_reopen(tmp_path, git_exe, committed_repo, qt_draft):
    workspace = WorkspaceManager(tmp_path / "workspaces", git_executable=git_exe)
    with local_api(tmp_path, workspace=workspace) as (client, _):
        mission = success(client.post(V2 + "/missions", json={
            "repository_path": str(committed_repo), "request": "Inspect stale UI state",
        }), 202)
        task_id = mission["task_id"]
        source = f"{V2}/missions/{task_id}"
        deadline = time.monotonic() + 25
        while mission["status"] != "awaiting_human":
            assert time.monotonic() < deadline, mission
            time.sleep(.03)
            mission = success(client.get(source))
        create = {"developer_note": qt_draft.developer_note, "request_id": "remember"}
        error(client.post(source + "/skill-drafts", json=create), 409, "mission_not_terminal")
        cancelled = success(client.post(source + "/approvals", json=decision(mission, decision="rejected")), 202)
        assert cancelled["status"] == "cancelled"
        draft = success(client.post(source + "/skill-drafts", json=create), 201)
        assert draft["source_mission_id"] == task_id
        assert draft["data"]["generation_status"] == "manual"
        assert draft["data"]["evidence_revision"] == cancelled["current_revision"]
        refs = draft["data"]["evidence_refs"]
        assert refs and all(ref["classification"] != "verified_result" for ref in refs)
        for ref in refs:
            artifact = success(client.get(source + "/artifacts/" + ref["record_id"]))
            assert artifact["content_sha256"] == ref["content_sha256"] and ref["locator"]
        path = f"{V2}/skill-drafts/{draft['draft_id']}"
        error(client.post(path + "/publish-local", json={"request_id": "review", "expected_revision": 1}),
              422, "skill_draft_incomplete")
        save = {"expected_revision": draft["revision"], "name": qt_draft.name,
                "pending_inference": "模型行可能仍然保留旧值。", "content": qt_draft.content.model_dump(mode="json")}
        saved = success(client.patch(path, json=save))
        assert saved["data"]["developer_note"] == create["developer_note"]
        assert saved["data"]["pending_inference"] == save["pending_inference"]
        assert saved["data"]["evidence_refs"] == refs and saved["revision"] == 2
        assert success(client.patch(path, json=save)) == saved
        error(client.patch(path, json={**save, "name": "并发编辑"}), 409, "skill_revision_conflict")
        assert success(client.get(path)) == saved
        assert success(client.post(source + "/skill-drafts", json=create), 201) == saved
        error(client.post(source + "/skill-drafts", json={**create, "developer_note": "另一条教训"}),
              409, "skill_request_conflict")
        assert recommend(client, "列表仍显示旧值") == []
        approve = {"expected_revision": saved["revision"], "request_id": "review"}
        version = success(client.post(path + "/publish-local", json=approve))
        assert success(client.post(path + "/publish-local", json=approve)) == version
        match, = recommend(client, "列表仍显示旧值")
        for key in ("skill_id", "version", "content_sha256"):
            assert match[key] == version[key]
        assert "developer_note" not in version and "evidence_refs" not in version["content"]

    # A fresh server and SQLite connection can work solely from persisted history.
    with local_api(tmp_path) as (client, _):
        page = success(client.get(V2 + "/skill-drafts", params={"source_mission_id": task_id, "limit": 1}))
        reviewed = success(client.get(path))
        assert page == {"drafts": [reviewed], "next_offset": None}
        assert reviewed["result_version"] == 1 and reviewed["data"]["evidence_refs"] == refs
        assert success(client.post(path + "/publish-local", json=approve)) == version
        skill = V2 + "/skills/" + version["skill_id"]
        assert success(client.get(skill + "/versions/1")) == version
        item, = success(client.get(V2 + "/skills"))["skills"]
        assert item["enabled"] and item["name"] == version["name"]
        fork = {"request_id": "revise", "version": 1}
        revision = success(client.post(skill + "/drafts", json=fork), 201)
        assert success(client.post(skill + "/drafts", json=fork), 201) == revision
        assert revision["base_version"] == 1 and revision["data"]["evidence_refs"] == []
        assert revision["data"]["content"] == version["content"]
        first_page = success(client.get(V2 + "/skill-drafts", params={"limit": 1}))
        assert first_page["drafts"] == [revision] and first_page["next_offset"] == 1
        last_page = success(client.get(V2 + "/skill-drafts", params={"limit": 1, "offset": 1}))
        assert last_page == {"drafts": [reviewed], "next_offset": None}
        disable = {"expected_revision": item["revision"], "enabled": False}
        error(client.patch(skill, json={**disable, "expected_revision": item["revision"] + 1}),
              409, "skill_revision_conflict")
        disabled = success(client.patch(skill, json=disable))
        assert not disabled["enabled"] and success(client.patch(skill, json=disable)) == disabled
        assert recommend(client, "列表仍显示旧值") == []
        revision_path = f"{V2}/skill-drafts/{revision['draft_id']}"
        error(client.post(revision_path + "/publish-local", json={"request_id": "stale-base", "expected_revision": 1}),
              409, "skill_revision_conflict")
        assert success(client.get(revision_path)) == revision
        assert success(client.get(skill + "/versions/1")) == version


def test_manual_draft_without_gateway_or_artifacts_and_strict_writes(tmp_path):
    with local_api(tmp_path) as (client, store):
        make_mission(store)
        finish_mission(store)
        path = f"{V2}/missions/{TASK_ID}/skill-drafts"
        empty = success(client.post(path, json={}), 201)
        assert empty["data"]["generation_status"] == "manual" and not empty["data"]["evidence_refs"]
        detail = f"{V2}/skill-drafts/{empty['draft_id']}"
        update = {"expected_revision": 1, "developer_note": "先保存手记，稍后审核。"}
        error(client.patch(detail, json=update, headers={"X-ForgePilot-Token": "incorrect"}), 401, "unauthorized")
        error(client.patch(detail, json={**update, "evidence_revision": 1}), 422, "invalid_request")
        error(client.patch(detail, json={**update, "evidence_refs": [{"record_id": str(uuid4())}]}),
              422, "invalid_request")
        error(client.patch(detail, json={**update, "expected_revision": True}), 422, "invalid_request")
        error(client.patch(detail, json={**update, "developer_note": "a" * 65536}), 413, "request_too_large")
        error(client.post(path, content=b"{}"), 415, "invalid_request")
        error(client.post(path, content=b'{"request_id":"a","request_id":"b"}',
                          headers={"Content-Type": "application/json"}), 422, "invalid_request")
        error(client.get(f"{V2}/skill-drafts/{uuid4()}"), 404, "skill_draft_not_found")
        error(client.get(f"{V2}/skills/{uuid4()}/versions/1"), 404, "skill_not_found")
        assert success(client.get(detail)) == empty
        assert success(client.patch(detail, json=update))["data"]["developer_note"] == update["developer_note"]


def test_foreign_evidence_rejected_on_http_read_save_and_publish(tmp_path):
    with local_api(tmp_path) as (client, store):
        other = str(uuid4())
        for task_id in (TASK_ID, other):
            make_mission(store, task_id)
            put(store, task_spec(), task_id=task_id)
            finish_mission(store, task_id)
        source = f"{V2}/missions/{TASK_ID}"
        create = {"request_id": "source"}
        draft = success(client.post(source + "/skill-drafts", json=create), 201)
        foreign = success(client.get(f"{V2}/missions/{other}/artifacts"))["items"][0]
        reference, = draft["data"]["evidence_refs"]
        assert reference["content_sha256"] == foreign["content_sha256"]
        assert reference["record_id"] != foreign["artifact_id"]
        error(client.get(source + "/artifacts/" + foreign["artifact_id"]), 404, "unknown_artifact")

        # Simulate a damaged persisted pointer to an identical payload in a different Mission.
        # The public PATCH schema only accepts editor fields.
        with store.transaction() as db:
            db.execute("""UPDATE skill_drafts SET data_json=json_set(data_json,'$.evidence_refs[0].record_id',?)
                          WHERE draft_id=?""", (foreign["artifact_id"], draft["draft_id"]))
        detail = f"{V2}/skill-drafts/{draft['draft_id']}"
        for response in (
            client.get(detail), client.get(V2 + "/skill-drafts"),
            client.post(source + "/skill-drafts", json=create),
            client.patch(detail, json={"expected_revision": 1, "name": "check source"}),
            client.post(detail + "/publish-local", json={"request_id": "review", "expected_revision": 1}),
        ):
            error(response, 422, "invalid_skill_evidence")
            assert foreign["artifact_id"] not in response.text
