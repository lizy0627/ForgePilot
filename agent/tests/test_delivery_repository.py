"""Delivery persistence keeps audit allocation and approval checks serialized."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.canonical import content_sha256
from forgepilot.delivery_store import DeliveryRepository
from forgepilot.errors import ForgePilotError
import forgepilot.mission_events as mission_events
from forgepilot.orchestration.analysis_review import scope_content
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import plan, task_spec


@pytest.fixture
def records(tmp_path):
    events = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    task_id = str(uuid4())
    events.missions.create_graph_run(task_id=task_id, graph_id="delivery-test", graph_version=1)
    mission_events.publish_mission_event(events, task_id, "task.created", {})
    try:
        yield events, DeliveryRepository(events), task_id
    finally:
        events.close()


def record_validation(repository, task_id, validation_id=None):
    return repository.record_next_validation(validation_id=validation_id or str(uuid4()),
        task_id=task_id, status="passed", started_at="2026-11-15T00:00:00Z",
        finished_at="2026-11-15T00:00:01Z", report_artifact_id=None, error_code="none")


def test_concurrent_validation_audit_allocates_one_attempt_and_event_per_record(records):
    events, repository, task_id = records
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = tuple(pool.map(lambda _: record_validation(repository, task_id), range(12)))
    assert sorted(row["attempt"] for row in rows) == list(range(1, 13))
    with events._lock:
        assert events._db.execute("SELECT count(*) FROM validation_runs WHERE task_id=?",
                                  (task_id,)).fetchone()[0] == 12
        assert events._db.execute("SELECT count(*) FROM graph_events WHERE task_id=? "
            "AND event_type='validation.completed'", (task_id,)).fetchone()[0] == 12
    assert events.missions.get_graph_run(task_id)["current_revision"] == 14


def test_validation_event_failure_rolls_back_record_and_attempt_allocation(records, monkeypatch):
    events, repository, task_id = records
    original = mission_events.append_record_event

    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("offline injected outbox failure")

    revision = events.missions.get_graph_run(task_id)["current_revision"]
    with monkeypatch.context() as failure:
        failure.setattr(mission_events, "append_record_event", fail_after_event)
        with pytest.raises(sqlite3.OperationalError, match="outbox failure"):
            record_validation(repository, task_id)
    with events._lock:
        assert events._db.execute("SELECT count(*) FROM validation_runs").fetchone()[0] == 0
        assert events._db.execute("SELECT count(*) FROM graph_events WHERE event_type='validation.completed'").fetchone()[0] == 0
    assert events.missions.get_graph_run(task_id)["current_revision"] == revision
    assert record_validation(repository, task_id)["attempt"] == 1


def test_duplicate_validation_record_does_not_consume_an_audit_attempt(records):
    _, repository, task_id = records
    first = record_validation(repository, task_id)
    with pytest.raises(ForgePilotError, match="storage_record"):
        record_validation(repository, task_id, first["validation_id"])
    assert record_validation(repository, task_id)["attempt"] == 2


def test_approval_candidates_keep_task_kind_and_frozen_package_bound(records):
    events, repository, task_id = records
    spec = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="draft",
        content=task_spec())
    execution = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="plan",
        parent_artifact_ids=(spec.artifact_id,),
        content=plan().model_copy(update={"task_spec_artifact_id": spec.artifact_id}))
    events.artifacts.put(spec)
    events.artifacts.put(execution)
    run = events.missions.get_graph_run(task_id)
    scope_hash = content_sha256(scope_content(spec.content))
    events.missions.update_graph_run(task_id, previous_revision=run["current_revision"],
        task_spec_artifact_id=spec.artifact_id, plan_artifact_id=execution.artifact_id,
        scope_hash=scope_hash)
    approval = events.missions.save_approval(approval_id=str(uuid4()), task_id=task_id,
        task_spec_artifact_id=spec.artifact_id, plan_artifact_id=execution.artifact_id,
        scope_hash=scope_hash, approved_by="user")
    assert repository.approved_grant_ids(task_id, "execution") == (approval["approval_id"],)
    assert repository.approved_grant_ids(task_id, "existing_tests") == ()
    assert repository.approved_grant_ids(str(uuid4()), "execution") == ()
    run = events.missions.get_graph_run(task_id)
    events.missions.update_graph_run(task_id, previous_revision=run["current_revision"],
                                    scope_hash="0" * 64)
    assert repository.approved_grant_ids(task_id, "execution") == ()
    assert not events.missions.get_approval(approval["approval_id"])["valid"]
