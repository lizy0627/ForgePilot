"""Selected guidance across HTTP admission, provider dispatch and durable recovery."""
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from closed_loop_support import create, done, make_loop_case, patch
from forgepilot.agents.prompts import get_prompt
from forgepilot.artifacts.canonical import content_sha256
from forgepilot.models.errors import http_error
from forgepilot.models.routing import ModelRouter, ResolvedRoute
from test_mission_api_workflow import AnalysisProvider, poll
from test_mission_scope_expansion import (
    HASHES, HEADER, HELPER, USES_HELPER, deliver, pending, request_scope, scope_app,
)


class RecordingAnalyst(AnalysisProvider):
    def __init__(self, case):
        super().__init__(case)
        self.calls = []
        self.failures = []

    async def chat(self, messages, tools=None):
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        self.calls.append((deepcopy(payload), messages[0]["content"], tuple(t.name for t in tools or ())))
        if "skill_references" in payload and self.failures:
            raise http_error(self.failures.pop(0))
        return await super().chat(messages, tools)


@asynccontextmanager
async def session(case, tmp_path, git_exe, analyst, *, restart=False):
    async with scope_app(case, tmp_path, git_exe, restart=restart, analyst_provider=analyst) as app:
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            yield client, app.state.mission_host


def select_skill(store, draft, *, request_id="original"):
    working = store.skills.create_draft(request_id=request_id, data=draft)
    version = store.skills.publish_local(working.draft_id, expected_revision=working.revision,
                                        request_id=f"publish-{request_id}")
    return version.model_dump(include={"skill_id", "version", "content_sha256"})


def upgrade(store, first):
    draft = store.skills.draft_from_version(first["skill_id"], first["version"], request_id="revise")
    content = draft.data.content.model_copy(update={"body": "Guidance from the newer version"})
    edited = store.skills.edit_draft(draft.draft_id, expected_revision=draft.revision,
        data=draft.data.model_copy(update={"content": content}))
    return store.skills.publish_local(edited.draft_id, expected_revision=edited.revision, request_id="v2")


def request_for(case):
    return {"repository_path": str(case.root), "request": "Clamp negative inputs", "task_type": "bug_fix"}


async def create_mission(client, case, **fields):
    result = await client.post("/api/v2/missions", json=request_for(case) | fields)
    assert result.status_code == 202, result.text
    return "/api/v2/missions/" + result.json()["task_id"]


@pytest.mark.asyncio
async def test_selected_retry_revision_and_scope_replay_keep_original_guidance(
        tmp_path, git_exe, qt_draft):
    case = await make_loop_case(tmp_path, git_exe, implement=[
        request_scope(), create(HELPER, HEADER), patch(after=USES_HELPER), done()])
    analyst = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, analyst) as (client, host):
            first = select_skill(host.store, qt_draft)
            plain = await create_mission(client, case)
            assert (await poll(client, plain, {"awaiting_human", "failed"}))["status"] == "awaiting_human"
            baseline = analyst.calls.copy()
            assert len(baseline) == 4 and all("skill_references" not in p for p, _, _ in baseline)
            plain_graph = (await client.get(plain + "/graph")).json()
            assert "skill_manifest_sha256" not in json.dumps(plain_graph)
            assert (await client.get(plain + "/skills")).json()["selected_skills"] == []
            analyst.calls.clear()

            preview = await client.post("/api/v2/missions/preview", json={"repository_path": str(case.root)})
            assert preview.status_code == 200, preview.text
            analyst.failures = [408, 503]  # Gateway retry within attempt 1, then explicit graph retry.
            path = await create_mission(client, case, selected_skills=[first],
                                        preview_token=preview.json()["preview_token"])
            tid = path.rsplit("/", 1)[1]
            waiting = await poll(client, path, {"retryable", "failed"})
            assert waiting["status"] == "retryable", (await client.get(path + "/graph")).json()
            injections = [dict(first, node_id="root_cause_analysis", attempt=1)]
            assert (await client.get(path + "/skills")).json() == {
                "task_id": tid, "selected_skills": [first], "injections": injections}
            assert upgrade(host.store, first).version == 2
            stale = await client.post("/api/v2/missions", json=request_for(case) | {"selected_skills": [first]})
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "skill_selection_changed"
            host.store.skills.disable(first["skill_id"], expected_revision=2)
            retry = await client.post(path + "/retry", json={
                "expected_revision": waiting["current_revision"], "node_id": "root_cause_analysis"})
            assert retry.status_code == 202, retry.text
            waiting = await poll(client, path, {"awaiting_human", "failed"})
            assert waiting["status"] == "awaiting_human", (await client.get(path + "/graph")).json()
            graph = (await client.get(path + "/graph")).json()
            markers = [n["node_id"] for n in graph["definition"]["nodes"] if
                       "skill_manifest_sha256" in n["settings"]]
            assert markers == ["root_cause_analysis"]
            unbound = deepcopy(graph["definition"])
            for node in unbound["nodes"]:
                node["settings"].pop("skill_manifest_sha256", None)
            assert unbound == plain_graph["definition"]
            assert graph["definition_hash"] != plain_graph["definition_hash"]
            for payload, prompt, tools in analyst.calls:
                (legacy, legacy_tools), = [(p, t) for p, s, t in baseline if s == prompt]
                assert tools == legacy_tools
                assert payload.keys() - {"skill_references", "skill_reference_usage"} == legacy.keys()
                if "request" in payload:
                    assert payload == legacy
                else:
                    assert payload["task_spec"]["content"] == legacy["task_spec"]["content"]
            references = [p["skill_references"] for p, _, _ in analyst.calls if "skill_references" in p]
            assert len(references) == 3
            for ref, in references:
                assert {k: ref[k] for k in first} == first
                assert ref["body"] == qt_draft.content.body
            assert qt_draft.developer_note not in json.dumps(analyst.calls, ensure_ascii=False)
            injections.append(dict(first, node_id="root_cause_analysis", attempt=2))
            usage = (await client.get(path + "/skills")).json()
            assert usage["injections"] == injections

            analysis = host._workflow(tid)._analysis_for(tid)
            old = analysis.review(tid)
            original_plan = host.store.artifacts.get(old.plan_artifact_id).content
            revised = analysis.revise(old, task_spec=host.store.artifacts.get(old.task_spec_artifact_id).content,
                plan=original_plan.model_copy(update={"summary": "Revised plan for the clamp"}))
            assert revised.plan_hash != old.plan_hash
            original = (await client.get(path)).json()["review"]
            approval = await client.post(path + "/approvals", json={"decision": "approved",
                "expected_revision": revised.revision, **{k: original[k] for k in HASHES}})
            assert approval.status_code == 202, approval.text
            await pending(client, path, "implementer")
        async with session(case, tmp_path, git_exe, analyst, restart=True) as (client, _):
            proposal, _ = await pending(client, path, "implementer")
            await deliver(client, path, case, proposal, original)
            assert (await client.get(path + "/skills")).json() == usage
            assert (await client.get(path + "/graph")).json()["definition_hash"] == graph["definition_hash"]
            events = (await client.get(path + "/events")).text
            assert qt_draft.content.body not in events and qt_draft.developer_note not in events
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
async def test_cloud_fallback_rejection_preserves_source_preview(tmp_path, git_exe, qt_draft, monkeypatch):
    case = await make_loop_case(tmp_path, git_exe)
    analyst = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, analyst) as (client, host):
            selected = select_skill(host.store, qt_draft)
            preview = await client.post("/api/v2/missions/preview", json={"repository_path": str(case.root)})
            assert preview.status_code == 200
            token = preview.json()["preview_token"]
            validate = ModelRouter.validate_agent

            def with_cloud_fallback(router, definition):
                route = validate(router, definition)
                if definition.role == "root_cause_analyst":
                    return ResolvedRoute(route.primary, route.primary.model_copy(
                        update={"base_url": "https://cloud.example.invalid/v1"}))
                return route

            with monkeypatch.context() as patch_route:
                patch_route.setattr(ModelRouter, "validate_agent", with_cloud_fallback)
                response = await client.post("/api/v2/missions", json=request_for(case) | {
                    "selected_skills": [selected], "preview_token": token})
            assert response.status_code == 403 and response.json()["error"]["code"] == "skill_route_denied"
            assert analyst.calls == [] and host.list_page()["total"] == 0
            assert host._previews[token].task_id not in host._services
            path = await create_mission(client, case, selected_skills=[], preview_token=token)
            assert (await poll(client, path, {"awaiting_human", "failed"}))["status"] == "awaiting_human"
            assert all("skill_references" not in p for p, _, _ in analyst.calls)
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "upgrade"])
async def test_changed_during_admission_rolls_back_graph_and_preview(
        tmp_path, git_exe, qt_draft, monkeypatch, change):
    case = await make_loop_case(tmp_path, git_exe)
    analyst = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, analyst) as (client, host):
            first = select_skill(host.store, qt_draft)
            preview = await client.post("/api/v2/missions/preview", json={"repository_path": str(case.root)})
            token = preview.json()["preview_token"]
            tid = host._previews[token].task_id
            worktree = Path(case.manager.record(tid)["worktree_path"])
            admit = host.store.missions.admit_mission

            def race(*args, **kwargs):
                assert host.graphs.snapshot(tid) and worktree.exists()
                if change == "disable":
                    host.store.skills.disable(first["skill_id"], expected_revision=1)
                else:
                    upgrade(host.store, first)
                return admit(*args, **kwargs)

            monkeypatch.setattr(host.store.missions, "admit_mission", race)
            result = await client.post("/api/v2/missions", json=request_for(case) | {
                "selected_skills": [first], "preview_token": token})
            assert result.status_code == 409 and result.json()["error"]["code"] == "skill_selection_changed"
            assert host._previews == {} and tid not in host._services and not worktree.exists()
            assert analyst.calls == [] and host.list_page()["total"] == 0
            with host.store.locked_connection() as db:
                for table in ("workspace_records", "graph_events", "task_graph_runs", "mission_skill_snapshots"):
                    key = "mission_id" if table == "mission_skill_snapshots" else "task_id"
                    assert not db.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (tid,)).fetchone()
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "changed"])
async def test_damaged_skill_after_restart_allows_history_and_blocks_execution(
        tmp_path, git_exe, qt_draft, damage):
    case = await make_loop_case(tmp_path, git_exe)
    analyst = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, analyst) as (client, host):
            first = select_skill(host.store, qt_draft)
            path = await create_mission(client, case, selected_skills=[first])
            waiting = await poll(client, path, {"awaiting_human", "failed"})
            assert waiting["status"] == "awaiting_human"
            tid = waiting["task_id"]
            with host.store.transaction() as db:
                if damage == "missing":
                    db.execute("DELETE FROM mission_skill_snapshots WHERE mission_id=?", (tid,))
                else:
                    db.execute("DROP TRIGGER mission_skill_snapshot_no_update")
                    db.execute("UPDATE mission_skill_snapshots SET content_json=json_set(content_json, "
                               "'$.body','modified guidance') WHERE mission_id=?", (tid,))
        analyst.calls.clear()
        async with session(case, tmp_path, git_exe, analyst, restart=True) as (client, _):
            for suffix in ("", "/skills", "/artifacts", "/graph"):
                assert (await client.get(path + suffix)).status_code == 200
            revision = (await client.get(path)).json()["current_revision"]
            denied = await client.post(path + "/resume", json={"expected_revision": revision})
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "skill_snapshot_invalid"
            assert analyst.calls == []
    finally:
        await case.gateway.aclose()


@pytest.mark.asyncio
async def test_two_skills_reach_only_their_selected_analysts(tmp_path, git_exe, qt_draft):
    case = await make_loop_case(tmp_path, git_exe)
    analyst = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, analyst) as (client, host):
            root_only = select_skill(host.store, qt_draft)
            both_stages = ("root_cause_analysis", "impact_test_analysis")
            content = qt_draft.content.model_copy(update={"body": "Check regression coverage.", "stages": both_stages})
            shared = select_skill(host.store, qt_draft.model_copy(update={"content": content}), request_id="shared")
            manifest = sorted([root_only, shared], key=lambda ref: ref["skill_id"])
            third = select_skill(host.store, qt_draft, request_id="third")
            denied = await client.post("/api/v2/missions", json=request_for(case) | {
                "selected_skills": [*manifest, third]})
            assert denied.status_code == 422 and denied.json()["error"]["code"] == "invalid_skill_selection"
            path = await create_mission(client, case, selected_skills=list(reversed(manifest)))
            assert (await poll(client, path, {"awaiting_human", "failed"}))["status"] == "awaiting_human"
            graph = (await client.get(path + "/graph")).json()["definition"]
            assert {node["node_id"]: node["settings"]["skill_manifest_sha256"] for node in graph["nodes"]
                    if "skill_manifest_sha256" in node["settings"]} == dict.fromkeys(both_stages, content_sha256(manifest))

            expected = {"root_cause_analysis": manifest, "impact_test_analysis": [shared]}
            by_prompt = {get_prompt(node["agent_id"], node["node_id"]): expected[node["node_id"]]
                         for node in graph["nodes"] if node["node_id"] in expected}
            assert len(analyst.calls) == 4
            for payload, prompt, _ in analyst.calls:
                if prompt in by_prompt:
                    assert [{key: item[key] for key in shared} for item in payload["skill_references"]] == by_prompt[prompt]
                else:
                    assert "skill_references" not in payload
            usage = (await client.get(path + "/skills")).json()
            assert usage["selected_skills"] == manifest
            for stage, refs in expected.items():
                assert [record for record in usage["injections"] if record["node_id"] == stage] == [
                    dict(ref, node_id=stage, attempt=1) for ref in refs]
            assert len(usage["injections"]) == 3
    finally:
        await case.gateway.aclose()
