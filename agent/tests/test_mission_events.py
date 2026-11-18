"""Durable v2 replay, atomic outbox, bounded subscriptions and redaction."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import (
    MissionEvent, MissionEventStream, append_mission_event, publish_mission_event,
)
from forgepilot.orchestration import GraphDefinition, NodeDefinition, NodeResult, NodeStatus, NodeType, validate_graph
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.runner import GraphRunner
from forgepilot.orchestration.store import GraphStore
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID, task_spec


def make_mission(store, task_id=TASK_ID):
    graph = GraphDefinition("event-fixture.v1", 1, (
        NodeDefinition("work", NodeType.DETERMINISTIC, "work"),))
    GraphStore(store).create_run(task_id, validate_graph(graph))
    publish_mission_event(store, task_id, "task.created", {
        "request": "fixture", "repository_path": "D:/fixture", "task_type": "auto",
        "orchestration_mode": "multi_agent", "base_ref": "HEAD", "toolchain_profile": "auto",
    })
    return graph


def finish_mission(store, task_id=TASK_ID):
    with store._transaction():
        store._db.execute("UPDATE task_graph_runs SET status='succeeded' WHERE task_id=?", (task_id,))
        append_mission_event(store, task_id, "task.completed", {})


@pytest.fixture
def store():
    instance = SqliteEventStore(redactor=Redactor(("fixture-secret",)))
    make_mission(instance)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("cursor", ["", "other:1", TASK_ID + ":0", TASK_ID + ":-1", TASK_ID + ":01",
                                     TASK_ID + ":1\n", TASK_ID + ":1.0", TASK_ID + ":" + "9" * 20])
def test_invalid_cursors_fail_without_subscribing(store, cursor):
    stream = MissionEventStream(store)
    with pytest.raises(ForgePilotError, match="invalid_event_id"):
        stream.subscribe(TASK_ID, cursor)
    assert stream.subscriber_count(TASK_ID) == 0


def test_ahead_unknown_and_capacity_are_distinct(store):
    stream = MissionEventStream(store, max_subscribers=1)
    with pytest.raises(ForgePilotError, match="event_cursor_ahead"):
        stream.subscribe(TASK_ID, TASK_ID + ":999")
    with pytest.raises(ForgePilotError, match="unknown_graph"):
        stream.subscribe("missing")
    subscriber = stream.subscribe(TASK_ID)
    with pytest.raises(ForgePilotError, match="mission_stream_capacity"):
        stream.subscribe(TASK_ID)
    subscriber.close()
    subscriber.close()
    assert stream.subscriber_count(TASK_ID) == 0


def test_global_subscriber_bound_covers_many_historical_missions(store):
    other = "00000000-0000-4000-a000-000000000002"
    make_mission(store, other)
    stream = MissionEventStream(store, max_total_subscribers=1)
    subscription = stream.subscribe(TASK_ID)
    try:
        with pytest.raises(ForgePilotError, match="mission_stream_capacity"):
            stream.subscribe(other)
    finally:
        subscription.close()
    stream.subscribe(other).close()


def test_state_and_event_rollback_together(store):
    before = GraphStore(store).snapshot(TASK_ID)
    with pytest.raises(RuntimeError, match="injected"):
        with store._transaction():
            store._db.execute("UPDATE task_graph_runs SET status='failed' WHERE task_id=?", (TASK_ID,))
            append_mission_event(store, TASK_ID, "task.failed", {"reason": "fixture"})
            raise RuntimeError("injected failure before commit")
    assert GraphStore(store).snapshot(TASK_ID) == before
    assert len(GraphStore(store).graph_events(TASK_ID)) == 2
    with pytest.raises(RuntimeError, match="state transaction"):
        append_mission_event(store, TASK_ID, "task.failed", {})


def test_parallel_publish_has_strict_revisions_and_stable_contiguous_ids(store):
    def publish(worker):
        for index in range(70):
            publish_mission_event(store, TASK_ID, "tool.completed", {"worker": worker, "index": index})
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(publish, range(3)))
    events = GraphStore(store).graph_events(TASK_ID)
    assert [event.sequence for event in events] == list(range(1, 213))
    assert [event.revision for event in events] == list(range(1, 213))
    assert GraphStore(store).snapshot(TASK_ID).revision == 212


@pytest.mark.asyncio
async def test_bounded_pages_reconnect_without_duplicates_and_cleanup(store):
    for index in range(143):
        publish_mission_event(store, TASK_ID, "tool.completed", {"index": index})
    finish_mission(store)
    stream = MissionEventStream(store, page_size=7)
    first = stream.subscribe(TASK_ID)
    initial = first.read()
    assert len(initial[0]) == 7 and not initial[1]
    assert first.read() == initial  # preflight must not consume the first page.
    frames = first.frames(initial=initial)
    assert await anext(frames) == "retry: 1000\n\n"
    received = [await anext(frames) for _ in range(11)]
    last = received[-1].splitlines()[0].removeprefix("id: ")
    await frames.aclose()
    assert stream.subscriber_count(TASK_ID) == 0
    again = stream.subscribe(TASK_ID, last)
    received.extend([frame async for frame in again.frames() if frame.startswith("id:")])
    ids = [frame.splitlines()[0].removeprefix("id: ") for frame in received]
    assert ids == [f"{TASK_ID}:{index}" for index in range(1, 147)]
    assert stream.subscriber_count(TASK_ID) == 0
    final = stream.subscribe(TASK_ID, ids[-1])
    assert final.read() == ((), True)
    final.close()


@pytest.mark.asyncio
async def test_idle_wait_cancel_and_request_disconnect_release_subscribers(store):
    stream = MissionEventStream(store, heartbeat_seconds=10, poll_seconds=.001)
    subscriber = stream.subscribe(TASK_ID, TASK_ID + ":2")
    frames = subscriber.frames()
    await anext(frames)
    waiting = asyncio.create_task(anext(frames))
    await asyncio.sleep(.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert stream.subscriber_count(TASK_ID) == 0

    class Disconnected:
        async def is_disconnected(self):
            return True
    subscriber = stream.subscribe(TASK_ID)
    assert [frame async for frame in subscriber.frames(Disconnected())] == ["retry: 1000\n\n"]
    assert stream.subscriber_count(TASK_ID) == 0


@pytest.mark.asyncio
async def test_stream_storage_failure_releases_subscriber_without_terminal_fiction(store, monkeypatch):
    stream = MissionEventStream(store)
    subscription = stream.subscribe(TASK_ID)
    def fail_read():
        raise sqlite3.OperationalError("injected read failure")
    monkeypatch.setattr(subscription, "read", fail_read)
    frames = subscription.frames()
    assert await anext(frames) == "retry: 1000\n\n"
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        await anext(frames)
    assert stream.subscriber_count(TASK_ID) == 0

def test_redaction_precedes_persistence_and_wire_serialization(store):
    publish_mission_event(store, TASK_ID, "security.recorded", {
        "password": "fixture-password", "known_secret": "fixture-secret", "reasoning": "private-reasoning",
        "nested": [{"hidden_test_source": "private-oracle", "DPAPI_blob": "encrypted-blob"}],
        "tool_json": json.dumps({"environment": {"HOME": "private-home"}, "authorization": "Bearer private-token"}),
    })
    serialized = store._db.execute("SELECT payload_json FROM graph_events WHERE task_id=? AND sequence=3", (TASK_ID,)).fetchone()[0]
    for secret in ("fixture-password", "fixture-secret", "private-reasoning", "private-oracle", "encrypted-blob", "private-home", "private-token"):
        assert secret not in serialized
    # Old rows are defensively sanitized before wire serialization too.
    with store._transaction():
        store._db.execute("UPDATE graph_events SET payload_json=? WHERE task_id=? AND sequence=1",
                          (json.dumps({"chain_of_thought": "legacy-private", "api_key": "legacy-key"}), TASK_ID))
    subscriber = MissionEventStream(store).subscribe(TASK_ID)
    try:
        frames = "".join(event.frame() for event in subscriber.read()[0])
        assert "legacy-private" not in frames and "legacy-key" not in frames
    finally:
        subscriber.close()


def test_artifact_and_observation_records_publish_in_the_same_transaction(store):
    artifact = create_artifact(task_id=TASK_ID, producer="orchestrator.v1", producer_node="work", content=task_spec())
    store.artifacts.put(artifact)
    store.missions.record_model_call(call_id="model-call", task_id=TASK_ID, role="orchestrator", model="fixture-model",
        prompt_version="draft.v1", input_hash="a" * 64, tool_schema={}, duration_ms=2)
    store.missions.record_validation(validation_id="validation", task_id=TASK_ID, attempt=1, status="passed")
    store.missions.record_security_event(event_id="security", task_id=TASK_ID, event_type="permission_denied", severity="warning")
    store.missions.save_workspace(workspace_id="workspace", task_id=TASK_ID, repository_path="D:/repo",
        worktree_path="D:/worktree", base_ref="HEAD", status="ready")
    kinds = [event.event_type for event in GraphStore(store).graph_events(TASK_ID)]
    assert kinds == ["graph_created", "task.created", "artifact.created", "model.completed",
                     "validation.completed", "security.recorded", "workspace.created"]
    assert GraphStore(store).snapshot(TASK_ID).revision == 7


def test_security_event_rejects_payload_fields_before_database_and_sse(store):
    store.missions.record_security_event(event_id="safe-denial", task_id=TASK_ID,
        event_type="tool_denied", severity="warning", attributes={
            "code": "policy_denied", "boundary": "tool_validation",
            "payload": "private-request-body", "api_key": "private-key",
            "tool": "read_file", "source": "Bearer private-token",
            "environment": {"SECRET": "private-env"},
        })
    row = store._db.execute("SELECT attributes_json FROM security_events WHERE event_id='safe-denial'").fetchone()[0]
    assert json.loads(row) == {"boundary": "tool_validation", "code": "policy_denied"}
    event = store._db.execute("SELECT payload_json FROM graph_events WHERE event_type='security.recorded'").fetchone()[0]
    for value in ("private-request-body", "private-key", "private-token", "private-env"):
        assert value not in row and value not in event


def test_repair_reason_follows_the_triggering_route(store):
    publish_mission_event(store, TASK_ID, "node.finished", {"to": "succeeded", "next_route": "repair"},
                          node_id="review_0")
    publish_mission_event(store, TASK_ID, "node.started", {}, node_id="repair_1")
    assert store.missions.observability(TASK_ID)["repairs"][0]["reason"] == "review_blockers"


@pytest.mark.asyncio
async def test_observability_is_task_bound_and_contains_only_provenance(store):
    from types import SimpleNamespace
    from forgepilot.mission_observability import tool_call
    context = SimpleNamespace(task_id=TASK_ID, node=SimpleNamespace(node_id="work", agent_id="analyst.v1"))
    class Registry:
        async def execute(self, name, arguments):
            return SimpleNamespace(success=True, duration_ms=3, error=None, output="private-tool-output")
    await tool_call(store, context, Registry(), "read_file", {"path": "src/read.cpp", "content": "private-input"})
    await tool_call(store, context, Registry(), "write_file", {"path": "src/written.cpp", "content": "private-input"})
    store.missions.record_model_call(call_id="audit-model", task_id=TASK_ID, node_id="work",
        role="analyst", model="fixture-model", prompt_version="analyst.v2", input_hash="a" * 64,
        tool_schema={"names": ["read_file"]}, duration_ms=7, input_tokens=2, output_tokens=3,
        total_tokens=5)
    GraphRunner(store, HandlerRegistry({"work": lambda _: NodeResult(NodeStatus.SUCCEEDED)})).run(
        TASK_ID, GraphDefinition("event-fixture.v1", 1, (
            NodeDefinition("work", NodeType.DETERMINISTIC, "work"),)))
    other = "00000000-0000-4000-a000-000000000002"
    make_mission(store, other)
    publish_mission_event(store, other, "tool.completed", {"tool": "read_file", "path": "other/secret.cpp"})
    audit = store.missions.observability(TASK_ID)
    assert audit["nodes"][0]["executor"] == "orchestrator"
    assert audit["model_calls"][0]["prompt_version"] == "analyst.v2"
    assert audit["model_calls"][0]["total_tokens"] == 5
    assert audit["files_read"] == ["src/read.cpp"]
    assert audit["files_modified"] == ["src/written.cpp"]
    assert len(audit["tools"]) == 2 and len(audit["nodes"]) == 1
    serialized = json.dumps(audit)
    assert "private-tool-output" not in serialized and "private-input" not in serialized
    assert "other/secret.cpp" not in serialized


@pytest.mark.asyncio
async def test_tool_events_record_paths_without_raw_arguments_or_output(store):
    from types import SimpleNamespace
    from forgepilot.mission_observability import tool_call
    context = SimpleNamespace(task_id=TASK_ID, node=SimpleNamespace(node_id="work", agent_id="analyst.v1"))
    class Registry:
        async def execute(self, name, arguments):
            return SimpleNamespace(success=True, duration_ms=1, error=None, output="private-output")
    for arguments in ({"path": "src/example.cpp", "content": "private-content"},
                      '{"path":"src/example.cpp","content":"private-content"}'):
        await tool_call(store, context, Registry(), "repository_read_range", arguments)
    rows = store._db.execute("SELECT payload_json FROM graph_events WHERE event_type='tool.completed'").fetchall()
    assert len(rows) == 2
    for row in rows:
        payload = json.loads(row[0])
        assert payload["path"] == "src/example.cpp"
        assert len(payload["arguments_hash"]) == 64
        assert "private-output" not in row[0] and "private-content" not in row[0]


def test_event_insert_failure_rolls_back_artifact_and_metadata(store):
    from forgepilot.artifacts.sqlite_store import ArtifactStorageError
    artifact = create_artifact(task_id=TASK_ID, producer="orchestrator.v1", producer_node="work", content=task_spec())
    store._db.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON graph_events
        WHEN NEW.event_type='artifact.created' BEGIN SELECT RAISE(ABORT,'injected event failure'); END""")
    with pytest.raises(ArtifactStorageError):
        store.artifacts.put(artifact)
    assert store._db.execute("SELECT count(*) FROM artifacts WHERE artifact_id=?", (artifact.artifact_id,)).fetchone()[0] == 0
    assert GraphStore(store).snapshot(TASK_ID).revision == 2
    assert len(GraphStore(store).graph_events(TASK_ID)) == 2


def test_restart_at_idle_approval_publishes_recovery_gate_once(tmp_path):
    path = tmp_path / "recovery-events.sqlite3"
    first = SqliteEventStore(path)
    graph = make_mission(first)
    runner = GraphRunner(first, HandlerRegistry({"work": lambda _: NodeResult(NodeStatus.AWAITING_HUMAN)}))
    waiting = runner.run(TASK_ID, graph)
    assert waiting.status is NodeStatus.AWAITING_HUMAN
    pauses = first.missions.observability(TASK_ID)["pauses"]
    assert len(pauses) == 1
    assert pauses[0]["node_id"] == "work" and pauses[0]["attempt"] == 1
    assert pauses[0]["reason"] == "human_approval_required"
    cursor = f"{TASK_ID}:{len(runner.events(TASK_ID))}"
    first.close()
    reopened = SqliteEventStore(path)
    try:
        run = reopened.missions.get_graph_run(TASK_ID)
        assert run["recovery_required"] == 1
        assert run["current_revision"] == waiting.revision + 1
        subscriber = MissionEventStream(reopened).subscribe(TASK_ID, cursor)
        events, closed = subscriber.read()
        subscriber.close()
        assert len(events) == 1 and not closed
        assert events[0].event_type == "task.recovery_required"
        assert events[0].data["recovery_required"] is True
        assert events[0].task_revision == run["current_revision"]
        assert json.loads(run["recovery_plan_json"])["revision"] == run["current_revision"]
    finally:
        reopened.close()
    again = SqliteEventStore(path)
    try:
        assert again.missions.get_graph_run(TASK_ID)["current_revision"] == waiting.revision + 1
    finally:
        again.close()


@pytest.mark.asyncio
async def test_actual_parallel_graph_model_artifacts_and_revision_boundary(cpp_repo, tmp_path):
    from test_analysis_workflow import service_for, request
    service, provider, events, gateway = await service_for(cpp_repo, tmp_path)
    try:
        graph = service.graph(request())
        GraphStore(events).create_run(TASK_ID, validate_graph(graph))
        publish_mission_event(events, TASK_ID, "task.created", {})
        result = await service.run(TASK_ID, request())
        assert result.status is NodeStatus.AWAITING_HUMAN
        subscriber = MissionEventStream(events).subscribe(TASK_ID)
        batch, _ = subscriber.read()
        subscriber.close()
        kinds = {event.event_type for event in batch}
        assert {"task.created", "model.completed", "artifact.created", "node.started", "node.completed", "approval.required"} <= kinds
        assert [event.task_revision for event in batch] == sorted({event.task_revision for event in batch})
        assert events._db.execute("SELECT count(*) FROM model_calls WHERE task_id=?", (TASK_ID,)).fetchone()[0] == 4
    finally:
        await gateway.aclose()
        events.close()
