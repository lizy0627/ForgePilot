"""Sensitive metadata never reaches either trace view or durable task events."""

import json

import pytest

from forgepilot.artifacts.canonical import content_sha256
from forgepilot.errors import ForgePilotError
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.trace import MemoryTrace, TraceEvent
from forgepilot.recording import now
from forgepilot.validation import ValidationService
from forgepilot.workspace_safety import Guard
from forgepilot.cli import _public_json
from test_persistence import task


def test_private_trace_fields_are_removed_before_memory_and_sqlite(tmp_path):
    secret = "fixture-private-oracle-source"
    data = {"name": "read_file", "oracle_source": secret,
            "nested": {"chain_of_thought": secret, "environment": {"TOKEN": secret}}}
    trace = MemoryTrace(Redactor())
    trace.emit("task", "tool_started", data)
    safe = trace.events[0].data
    assert safe["oracle_source"] == "[REDACTED]"
    assert safe["nested"]["chain_of_thought"] == "[REDACTED]"
    assert safe["nested"]["environment"] == "[REDACTED]"

    path = tmp_path / "events.sqlite3"
    store = SqliteEventStore(path)
    snapshot = task()
    task_id = snapshot["task_id"]
    try:
        store.create(task_id, snapshot)
        store.publish(task_id, snapshot, [], traces=[TraceEvent(1, task_id, "tool_started", now(), data)])
        assert store.trace_detail(task_id, 1)["data"] == safe
        assert secret not in "\n".join(store._db.iterdump())
    finally:
        store.close()


def test_cli_structured_output_uses_the_same_sensitive_boundary():
    secret = "fixture-cli-private-key-19"
    value = {"result": "safe", "environment": {"FORGEPILOT_API_KEY": secret},
             "nested": {"chain_of_thought": "private-reasoning"}, "output": secret}
    visible = _public_json(Redactor((secret,)), value)
    assert '"result": "safe"' in visible
    assert secret not in visible and "private-reasoning" not in visible


def test_validation_log_sanitizes_direct_storage_and_read(tmp_path):
    secret = "fixture-validation-credential"
    validator = ValidationService.__new__(ValidationService)
    validator.guard = Guard(tmp_path / "validation", create=True)
    validator.redactor = Redactor((secret,))
    validator._paths = lambda task_id: None
    task_id = "test-task"
    artifact_id = validator._store_log(task_id, "build", {
        "stdout": f"password={secret}", "environment": {"TOKEN": secret},
    })
    record = validator.read_log(task_id, artifact_id)
    assert secret not in str(record)
    assert record["content"]["environment"] == "[REDACTED]"
    target = validator.guard.path(f"{task_id}/logs/{artifact_id}.json")
    assert secret.encode() not in target.read_bytes()
    # Even a rehashed legacy/tampered log is denied at the read boundary.
    record["content"]["stdout"] = secret
    record["content_sha256"] = content_sha256(record["content"])
    target.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ForgePilotError, match="validation_log"):
        validator.read_log(task_id, artifact_id)
