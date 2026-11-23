"""Generation dispatch, routing, single-call budget and worker lifecycle."""
import asyncio
from contextlib import asynccontextmanager, closing
import json
from threading import Event

import pytest

from forgepilot.artifacts.models import DataPolicy
from forgepilot.errors import ForgePilotError
from forgepilot.host_runtime import HostRuntime
from forgepilot.mission_tools import MissionToolSelection
from forgepilot.models.contracts import ChatReply
from forgepilot.models.errors import http_error
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.skill_draft_jobs import SkillDraftJobs
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_capacity import TaskCapacity
from test_artifact_protocol import TASK_ID, all_contents
from test_mission_events import make_mission
from test_skill_mission_drafts import put, set_state


LOCAL = "http://127.0.0.1:1/v1"
CLOUD = "https://offline-model.invalid/v1"


@asynccontextmanager
async def service(tmp_path, chat, *, primary=LOCAL, fallback=LOCAL, frozen=False, current=False, timeout=60):
    profiles = [ModelProfile(profile_id=name, base_url=url, model=name, family="fixture",
                             api_key_ref="env:TEST_KEY", timeout_seconds=timeout)
                for name, url in (("primary", primary), ("fallback", fallback))]
    config = ModelConfiguration(profiles=profiles,
        routes=[ModelRoute(route_id="summary", primary="primary", fallback="fallback")])
    allocated = []

    class Provider:
        async def aclose(self):
            pass

    def provider(profile, _key):
        allocated.append(profile.profile_id)
        result = Provider()
        result.chat = chat
        return result

    runtime = HostRuntime()
    gateway = ModelGateway(config, secret_resolver=lambda _: "test-secret", provider_factory=provider)
    with closing(SqliteEventStore(tmp_path / "jobs.sqlite3")) as store:
        make_mission(store)
        selection = MissionToolSelection(False, DataPolicy(cloud_model_allowed=frozen)).to_dict()
        with store.transaction() as db:
            db.execute("""UPDATE graph_events SET payload_json=json_set(payload_json,'$.tool_selection',json(?))
                          WHERE task_id=? AND event_type='task.created'""", (json.dumps(selection), TASK_ID))
        put(store, all_contents()[1])
        set_state(store, "failed")
        jobs = SkillDraftJobs(store, runtime, gateway, DataPolicy(cloud_model_allowed=current),
                              capacity=TaskCapacity(max_tasks=2))
        await runtime.start()
        await jobs.start()
        try:
            yield jobs, allocated
        finally:
            await jobs.close()
            await runtime.close(close_resources=gateway.aclose)


async def settled(jobs, draft_id, attempt_id):
    async with asyncio.timeout(4):
        while True:
            attempt = jobs.store.skills.get_attempt(draft_id, attempt_id)
            if attempt.status != "generating":
                return attempt
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(("primary", "fallback", "frozen", "current"), [
    (LOCAL, CLOUD, False, True), (LOCAL, CLOUD, True, False), (CLOUD, LOCAL, False, True),
])
async def test_restricted_primary_or_fallback_prevents_all_dispatch(tmp_path, primary, fallback, frozen, current):
    async def no_calls(messages, tools=None):
        pytest.fail("Restricted route dispatched a model")

    async with service(tmp_path, no_calls, primary=primary, fallback=fallback, frozen=frozen, current=current) as (jobs, allocated):
        draft, aid = await jobs.create(TASK_ID, developer_note="Preserve me.")
        attempt = await settled(jobs, draft.draft_id, aid)
        assert (attempt.status, attempt.error_code) == ("failed", "skill_route_denied")
        assert not allocated
        assert jobs.store.skills.get_draft(draft.draft_id).data.developer_note == "Preserve me."


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["server", "schema"])
async def test_one_dispatch_even_when_gateway_could_fallback_or_repair(tmp_path, error):
    calls = []
    async def fail(messages, tools=None):
        calls.append((messages, tools))
        if error == "server":
            raise http_error(503)
        return ChatReply.model_validate({"message": {"role": "assistant", "content": "malformed-private-output"},
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    async with service(tmp_path, fail, primary=CLOUD, frozen=True, current=True) as (jobs, allocated):
        draft, aid = await jobs.create(TASK_ID, developer_note="Save this note")
        attempt = await settled(jobs, draft.draft_id, aid)
        assert attempt.status == "failed" and attempt.route == "summary/primary"
        assert attempt.elapsed_ms is not None and attempt.template_version == "skill-draft.v1"
        assert len(calls) == 1 and calls[0][1] is None and allocated == ["primary"]
        assert "malformed-private-output" not in attempt.model_dump_json()
        assert attempt.error_code == ("llm_http" if error == "server" else "llm_schema")
        if error == "schema":
            assert attempt.usage == {"prompt_tokens": 12, "completion_tokens": 3}


@pytest.mark.asyncio
async def test_timeout_receipt_and_retry_survive_cancellation_resistant_reply(tmp_path):
    release, late = Event(), Event()
    calls = []
    async def slow(messages, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                late.set()
                while not release.is_set():
                    await asyncio.sleep(.005)
        body = dict(phenomenon="Delayed close", priority_checks="Inspect ownership", decision_branches="Check shutdown",
                    stop_conditions="Stop after teardown", boundaries="Only worker threads", conclusion="Verify the order",
                    evidence_keys=[])
        return ChatReply.model_validate({"message": {"role": "assistant", "content": json.dumps(body)}})

    async with service(tmp_path, slow, timeout=.3) as (jobs, _):
        try:
            draft, aid = await jobs.create(TASK_ID, developer_note="Keep my note")
            first = await settled(jobs, draft.draft_id, aid)
            assert first.status == "timed_out" and first.error_code == "llm_timeout"
            assert await asyncio.to_thread(late.wait, 1)
            retried, second_id = await jobs.regenerate(draft.draft_id, expected_revision=draft.revision, request_id="retry")
            assert retried.latest_attempt.attempt_id == second_id != aid
            second = await settled(jobs, draft.draft_id, second_id)
            assert second.status == "ready" and second.suggestion
        finally:
            release.set()
        async with asyncio.timeout(3):
            while not jobs.store.skills.get_attempt(draft.draft_id, aid).suggestion:
                await asyncio.sleep(.01)
        assert jobs.store.skills.get_attempt(draft.draft_id, aid).status == "timed_out"
        latest = jobs.store.skills.get_draft(draft.draft_id)
        assert latest.latest_attempt == second and latest.data.developer_note == "Keep my note"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_capacity_and_orderly_shutdown_preserve_each_receipt(tmp_path):
    entered = Event()
    calls = []
    async def wait(messages, tools=None):
        calls.append(messages)
        if len(calls) == 2:
            entered.set()
        await asyncio.Event().wait()

    async with service(tmp_path, wait) as (jobs, _):
        first, second = await asyncio.gather(jobs.create(TASK_ID), jobs.create(TASK_ID))
        assert await asyncio.to_thread(entered.wait, 3)
        assert jobs.capacity.active_count() == 2
        full, aid = await jobs.create(TASK_ID, developer_note="Save while busy")
        assert full.latest_attempt.attempt_id == aid and full.latest_attempt.error_code == "task_capacity"
        assert full.data.developer_note == "Save while busy" and len(calls) == 2
        await jobs.close()
        for draft, aid in (first, second):
            assert jobs.store.skills.get_attempt(draft.draft_id, aid).status == "interrupted"
        assert jobs.capacity.active_count() == 0
        with pytest.raises(ForgePilotError, match="server_stopping"):
            await jobs.create(TASK_ID)
