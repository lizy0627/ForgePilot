"""Recover before the first analyst dispatch and verify the actual provider input."""
import asyncio
from threading import Event

import pytest

from closed_loop_support import make_loop_case
from forgepilot import builtin_skills
from forgepilot.orchestration.checkpoint import CheckpointStore
from forgepilot.orchestration.execution import coordinator_for_storage
from test_mission_api_workflow import poll
from test_mission_skill_workflow import RecordingAnalyst, create_mission, session, upgrade


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [None, "explicit_disable", "missing_catalog"])
async def test_resume_after_task_spec_uses_frozen_skills_at_first_dispatch(tmp_path, git_exe, qt_draft, monkeypatch,
                                                                          rollback):
    case = await make_loop_case(tmp_path, git_exe)
    original_provider = RecordingAnalyst(case)
    try:
        async with session(case, tmp_path, git_exe, original_provider) as (client, host):
            # Stop scheduling at a real durable checkpoint, with no fake node
            # records. The next process will use a fresh execution coordinator.
            reached_checkpoint = Event()
            checkpoints = CheckpointStore(host.store)
            coordinator = coordinator_for_storage(host.store)
            begin_execution = coordinator.begin_execution

            def stop_after_task_spec(task_id):
                if checkpoints.latest(task_id) is not None:
                    reached_checkpoint.set()
                    return False
                return begin_execution(task_id)

            monkeypatch.setattr(coordinator, "begin_execution", stop_after_task_spec)
            stages = ["root_cause_analysis", "impact_test_analysis"]
            draft = host.store.skills.create_draft(request_id="original", data=qt_draft)
            detail = f"/api/v2/skill-drafts/{draft.draft_id}"
            saved = await client.patch(detail, json={
                "expected_revision": draft.revision,
                "content": {**qt_draft.content.model_dump(mode="json"), "stages": stages}})
            assert saved.status_code == 200, saved.text
            assert saved.json()["data"]["content"]["stages"] == sorted(stages)
            published = await client.post(detail + "/publish-local", json={
                "expected_revision": saved.json()["revision"], "request_id": "publish"})
            assert published.status_code == 200, published.text
            version = published.json()
            first = {key: version[key] for key in ("skill_id", "version", "content_sha256")}
            path = await create_mission(client, case, selected_skills=[first])
            tid = path.rsplit("/", 1)[1]
            assert await asyncio.to_thread(reached_checkpoint.wait, 20)
            checkpoint = checkpoints.latest(tid)
            assert checkpoint.node_id == "draft_task_spec"
            assert (await client.get(path + "/skills")).json()["injections"] == []
            assert len(original_provider.calls) == 1 and "request" in original_provider.calls[0][0]
            graph_hash = (await client.get(path + "/graph")).json()["definition_hash"]
            assert upgrade(host.store, first).version == 2
            host.store.skills.disable(first["skill_id"], expected_revision=2)

        restarted_provider = RecordingAnalyst(case)
        if rollback == "explicit_disable":
            monkeypatch.setenv("FORGEPILOT_DISABLE_SKILL_ENTRIES", "1")
        elif rollback == "missing_catalog":
            monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: tmp_path / "missing")
        async with session(case, tmp_path, git_exe, restarted_provider, restart=True) as (client, _):
            mission = (await client.get(path)).json()
            assert mission["recovery_required"]
            preview = await client.post(path + "/resume", json={"expected_revision": mission["current_revision"]})
            assert preview.status_code == 202, preview.text
            plan = preview.json()["recovery"]
            assert plan["checkpoint_id"] == checkpoint.checkpoint_id and plan["blocked_reason"] is None
            assert restarted_provider.calls == []
            confirmed = await client.post(path + "/resume", json={
                "expected_revision": preview.json()["mission"]["current_revision"], "recovery_token": plan["token"]})
            assert confirmed.status_code == 202, confirmed.text
            waiting = await poll(client, path, {"awaiting_human", "failed", "retryable", "cancelled"})
            assert waiting["status"] == "awaiting_human"
            assert (await client.get(path + "/graph")).json()["definition_hash"] == graph_hash
            inputs = [payload for payload, _, _ in restarted_provider.calls]
            assert len(inputs) == 3 and all("request" not in payload for payload in inputs)
            references = [payload["skill_references"] for payload in inputs if "skill_references" in payload]
            assert len(references) == len(stages)
            for ref, in references:
                assert {key: ref[key] for key in first} == first and ref["body"] == qt_draft.content.body
            usage = (await client.get(path + "/skills")).json()
            assert usage["task_id"] == tid and usage["selected_skills"] == [first]
            assert sorted(usage["injections"], key=lambda item: item["node_id"]) == [
                dict(first, node_id=stage, attempt=1) for stage in sorted(stages)]
    finally:
        await case.gateway.aclose()
