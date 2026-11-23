"""Exercise the asynchronous draft API through HTTP and the real model gateway."""
import asyncio
from contextlib import closing, contextmanager
import json
from threading import Event
import time
from uuid import uuid4

import httpx
import pytest

from forgepilot.artifacts.models import DataPolicy
from forgepilot.mission_tools import MissionToolSelection
from forgepilot.models.gateway import ModelGateway
from forgepilot.sqlite_store import SqliteEventStore
from test_api_network import serving
from test_artifact_protocol import TASK_ID, all_contents, task_spec
from test_mission_events import make_mission
from test_model_gateway import completion
from test_skill_drafts_http import V2, error, success
from test_skill_mission_drafts import put, set_state


CREATE = f"{V2}/missions/{TASK_ID}/skill-drafts"


@contextmanager
def draft_api(tmp_path, handler, *, policy=DataPolicy()):
    gateway = ModelGateway.from_legacy("http://127.0.0.1:1/v1", "fake-test-credential", {"summary": "fixture"},
                                       transport=httpx.MockTransport(handler))
    with closing(SqliteEventStore(tmp_path / "drafts.db")) as store:
        with serving(lambda _: None, store=store, skill_gateway=gateway, skill_data_policy=policy,
                     model_client_closer=gateway.aclose) as (url, _):
            with httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
                yield client, store


def source(store, status, *, evidence=True, frozen_policy=True):
    make_mission(store)
    if frozen_policy:
        selection = MissionToolSelection(False, DataPolicy()).to_dict()
        with store.transaction() as db:
            db.execute("""UPDATE graph_events SET payload_json=json_set(payload_json,'$.tool_selection',json(?))
                          WHERE task_id=? AND event_type='task.created'""", (json.dumps(selection), TASK_ID))
    if evidence:
        put(store, task_spec())
        put(store, all_contents()[1])
        put(store, all_contents()[4])
    set_state(store, status)


def source_records(store):
    with store.locked_connection() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE task_id=?", (TASK_ID,))]
                for table in ("task_graph_runs", "graph_node_states", "approvals", "graph_events", "artifacts")}


def settled(client, path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        draft = success(client.get(path))
        if draft["data"]["generation_status"] != "generating":
            return draft
        time.sleep(.015)
    pytest.fail("Draft generation did not settle")


def test_accepted_before_model_finishes_and_edit_survives_regeneration(tmp_path):
    entered, release = Event(), Event()
    inputs = []

    async def generate(request):
        inputs.append(json.loads(request.content))
        materials = json.loads(next(item["content"] for item in inputs[-1]["messages"] if item["role"] == "user"))["materials"]
        public_result = next(item for item in materials if item["classification"] == "verified_result")
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        suggestion = dict(phenomenon="Worker keeps running.", priority_checks="Inspect shutdown.",
                          decision_branches="If still running, check its cancellation token.",
                          stop_conditions="Stop when teardown ordering is established.",
                          boundaries="Applies during worker teardown.", conclusion="Ordering needs review.",
                          evidence_keys=["unissued", public_result["key"]])
        return httpx.Response(200, json=completion(json.dumps(suggestion),
                              usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}))

    with draft_api(tmp_path, generate) as (client, store):
        source(store, "succeeded")
        findings = all_contents()[1]
        claim, = findings.items
        additional = tuple(claim.model_copy(update={"evidence_id": f"EV-{n}", "claim": "窗口仍显示旧值" * 200})
                           for n in range(2, 12))
        put(store, findings.model_copy(update={"items": (claim, *additional)}))
        before = source_records(store)
        create = {"generation_mode": "model", "request_id": "lesson-1", "developer_note": "Remember teardown."}
        draft = success(client.post(CREATE, json=create), 202)
        path = f"{V2}/skill-drafts/{draft['draft_id']}"
        first = draft["latest_attempt"]["attempt_id"]
        try:
            assert entered.wait(3)
            assert draft["data"]["generation_status"] == "generating"
            replay = client.post(CREATE, json=create)
            assert success(replay, 202)["latest_attempt"]["attempt_id"] == first
            assert replay.headers["location"] == f"{path}/attempts/{first}"
            error(client.post(CREATE, json={**create, "generation_mode": "manual"}), 409, "skill_request_conflict")
            saved = success(client.patch(path, json={"expected_revision": draft["revision"],
                                   "content": {"body": "Check worker lifetime before reviewing signals."}}))
        finally:
            release.set()
        ready = settled(client, path)
        assert ready["data"]["content"] == saved["data"]["content"]
        assert ready["data"]["developer_note"] == create["developer_note"]
        assert ready["data"]["generation_status"] == "ready", ready["latest_attempt"]
        assert ready["latest_attempt"]["start_revision"] == draft["revision"] < saved["revision"]
        pending, verified = ready["latest_attempt"]["suggestion"]["basis"]
        assert pending["classification"] == "pending_inference" and pending["reference"] is None
        assert verified["reference"] in draft["data"]["evidence_refs"]
        assert ready["latest_attempt"]["usage"]
        editor = {key: text for key, text in ready["latest_attempt"]["suggestion"].items() if key != "basis"}
        editor.update(attempt_id=first, conclusion="Review whether cancellation ran before teardown.",
                      basis_note="The model's citation needs manual confirmation.")
        edit = {"expected_revision": saved["revision"], "edited_lesson": editor}
        error(client.patch(path, json={**edit, "edited_lesson": {**editor, "attempt_id": str(uuid4())}}),
              422, "invalid_skill_evidence")
        saved = success(client.patch(path, json=edit))
        assert saved["data"]["edited_lesson"] == editor
        assert saved["latest_attempt"] == ready["latest_attempt"]
        assert len(inputs) == 1
        sent = json.dumps(inputs[0]["messages"], ensure_ascii=False)
        assert len(sent) <= 12_000 and len(sent.encode("utf-8")) <= 24_000
        assert "Cancel work before teardown." in sent and "No cancellation call." in sent
        assert "protected_test" not in sent and "D:/fixture" not in sent
        retry = {"request_id": "again", "expected_revision": saved["revision"]}
        new = success(client.post(path + "/regenerate", json=retry), 202)
        second = new["latest_attempt"]["attempt_id"]
        assert first != second
        assert success(client.post(path + "/regenerate", json=retry), 202)["latest_attempt"]["attempt_id"] == second
        regenerated = settled(client, path)
        assert regenerated["data"]["content"] == saved["data"]["content"]
        assert regenerated["data"]["edited_lesson"] == editor
        attempt_path = path + "/attempts/" + first
        assert success(client.get(attempt_path)) == ready["latest_attempt"]
        replay = client.post(CREATE, json=create)
        assert success(replay, 202)["latest_attempt"]["attempt_id"] == second
        assert replay.headers["location"] == attempt_path
        assert success(client.post(attempt_path + "/cancel", json={})) == ready["latest_attempt"]
        error(client.get(f"{V2}/skill-drafts/{uuid4()}/attempts/{first}"), 404, "skill_attempt_not_found")
        assert len(inputs) == 2 and source_records(store) == before


@pytest.mark.parametrize(("status", "evidence", "policy"), [
    ("cancelled", False, True), ("failed", False, True), ("succeeded", True, False),
])
def test_missing_evidence_or_policy_keeps_note_without_dispatch(tmp_path, status, evidence, policy):
    calls = []
    with draft_api(tmp_path, lambda request: calls.append(request)) as (client, store):
        source(store, status, evidence=evidence, frozen_policy=policy)
        before = source_records(store)
        create = {"developer_note": "Rule out stale state."}
        if status == "failed":
            draft = success(client.post(CREATE, json=create), 201)
            path = f"{V2}/skill-drafts/{draft['draft_id']}"
            draft = success(client.post(path + "/regenerate", json={
                "expected_revision": draft["revision"], "request_id": "generate-from-note",
            }), 202)
        else:
            draft = success(client.post(CREATE, json={**create, "generation_mode": "model"}), 202)
        path = f"{V2}/skill-drafts/{draft['draft_id']}"
        failed = settled(client, path)
        assert failed["latest_attempt"]["status"] == "failed"
        assert failed["latest_attempt"]["error_code"] and not calls
        saved = success(client.patch(path, json={"expected_revision": failed["revision"], "name": "Local note"}))
        assert saved["data"]["developer_note"] == "Rule out stale state."
        assert source_records(store) == before


def test_cancel_targets_only_the_named_attempt(tmp_path):
    entered = Event()

    async def wait_for_cancel(request):
        entered.set()
        await asyncio.Event().wait()

    with draft_api(tmp_path, wait_for_cancel) as (client, store):
        source(store, "cancelled")
        before = source_records(store)
        draft = success(client.post(CREATE, json={"generation_mode": "model", "developer_note": "Handwritten."}), 202)
        path = f"{V2}/skill-drafts/{draft['draft_id']}"
        attempt = path + "/attempts/" + draft["latest_attempt"]["attempt_id"]
        assert entered.wait(3)
        assert success(client.post(attempt + "/cancel", json={}))["status"] == "cancelled"
        saved = settled(client, path)
        assert success(client.post(attempt + "/cancel", json={})) == saved["latest_attempt"]
        assert saved["data"]["developer_note"] == "Handwritten." and source_records(store) == before
