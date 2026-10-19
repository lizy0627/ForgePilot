"""Schema-8 persistence repositories for future graph orchestration.

This module deliberately contains persistence only.  It does not schedule or run graph
nodes; callers use the repository methods instead of embedding SQL in orchestration code.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Any, Mapping

from .artifacts.canonical import canonical_json_bytes
from .errors import ForgePilotError


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def create_mission_schema(db: sqlite3.Connection) -> None:
    """Create the schema-8 graph/artifact persistence surface."""
    statements = (
        """CREATE TABLE task_graph_runs(
            task_id TEXT PRIMARY KEY,
            graph_id TEXT NOT NULL,
            graph_version INTEGER NOT NULL CHECK(graph_version >= 1),
            status TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision >= 1),
            task_spec_artifact_id TEXT,
            plan_artifact_id TEXT,
            scope_hash TEXT CHECK(scope_hash IS NULL OR length(scope_hash) = 64),
            workspace_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT)""",
        """CREATE TABLE task_node_attempts(
            task_id TEXT NOT NULL REFERENCES task_graph_runs(task_id),
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt >= 1),
            node_type TEXT NOT NULL,
            agent_id TEXT,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 64),
            input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
            error_code TEXT,
            error_summary TEXT,
            checkpoint_id TEXT,
            PRIMARY KEY(task_id,node_id,attempt))""",
        """CREATE TABLE artifacts(
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            producer TEXT NOT NULL,
            producer_node TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
            storage_kind TEXT NOT NULL CHECK(storage_kind IN ('inline','external')),
            inline_json BLOB,
            relative_path TEXT,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            CHECK((storage_kind='inline' AND inline_json IS NOT NULL AND relative_path IS NULL)
               OR (storage_kind='external' AND inline_json IS NULL AND relative_path IS NOT NULL)))""",
        "CREATE INDEX artifacts_task_created ON artifacts(task_id,created_at,artifact_id)",
        """CREATE TABLE artifact_relations(
            parent_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            child_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            relation TEXT NOT NULL CHECK(relation IN ('derived_from','revision_of')),
            created_at TEXT NOT NULL,
            PRIMARY KEY(parent_artifact_id,child_artifact_id,relation),
            CHECK(parent_artifact_id <> child_artifact_id))""",
        "CREATE INDEX artifact_relations_child ON artifact_relations(child_artifact_id,parent_artifact_id)",
        """CREATE TABLE approvals(
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task_graph_runs(task_id),
            approval_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('approved','rejected','invalidated')),
            task_spec_artifact_id TEXT NOT NULL,
            task_spec_hash TEXT NOT NULL CHECK(length(task_spec_hash) = 64),
            plan_artifact_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL CHECK(length(plan_hash) = 64),
            scope_hash TEXT NOT NULL CHECK(length(scope_hash) = 64),
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            invalidated_at TEXT,
            invalidation_reason TEXT)""",
        "CREATE INDEX approvals_task_created ON approvals(task_id,created_at,approval_id)",
        """CREATE TABLE model_calls(
            call_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            node_id TEXT,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
            tool_schema_json TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
            total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 0),
            error_code TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            fallback_model TEXT,
            response_hash TEXT CHECK(response_hash IS NULL OR length(response_hash) = 64),
            created_at TEXT NOT NULL)""",
        "CREATE INDEX model_calls_task_created ON model_calls(task_id,created_at,call_id)",
        """CREATE TABLE checkpoints(
            checkpoint_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            checkpoint_type TEXT NOT NULL,
            status TEXT NOT NULL,
            workspace_id TEXT,
            commit_hash TEXT,
            artifact_id TEXT,
            created_at TEXT NOT NULL,
            error_code TEXT)""",
        "CREATE INDEX checkpoints_task_created ON checkpoints(task_id,created_at,checkpoint_id)",
        """CREATE TABLE workspace_records(
            workspace_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            repository_path TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_ref TEXT NOT NULL,
            head_commit TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT)""",
        "CREATE UNIQUE INDEX workspace_records_task ON workspace_records(task_id)",
        """CREATE TABLE validation_runs(
            validation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt >= 1),
            status TEXT NOT NULL,
            report_artifact_id TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
            error_code TEXT,
            UNIQUE(task_id,attempt))""",
        """CREATE TABLE security_events(
            event_id TEXT PRIMARY KEY,
            task_id TEXT,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            attributes_json TEXT NOT NULL)""",
        "CREATE INDEX security_events_task_time ON security_events(task_id,occurred_at,event_id)",
        """CREATE TRIGGER approvals_stale_after_graph_change
            AFTER UPDATE OF task_spec_artifact_id,plan_artifact_id,scope_hash ON task_graph_runs
            WHEN OLD.task_spec_artifact_id IS NOT NEW.task_spec_artifact_id
              OR OLD.plan_artifact_id IS NOT NEW.plan_artifact_id
              OR OLD.scope_hash IS NOT NEW.scope_hash
            BEGIN
                UPDATE approvals SET status='invalidated',invalidated_at=CURRENT_TIMESTAMP,
                    invalidation_reason='approved_objects_changed'
                WHERE task_id=NEW.task_id AND status='approved';
            END""",
        """CREATE TRIGGER approvals_stale_after_artifact_change
            AFTER UPDATE OF content_sha256 ON artifacts
            WHEN OLD.content_sha256 <> NEW.content_sha256
            BEGIN
                UPDATE approvals SET status='invalidated',invalidated_at=CURRENT_TIMESTAMP,
                    invalidation_reason='approved_artifact_changed'
                WHERE status='approved' AND
                    (task_spec_artifact_id=NEW.artifact_id OR plan_artifact_id=NEW.artifact_id);
            END""",
    )
    for statement in statements:
        db.execute(statement)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ForgePilotError("storage_record", f"Invalid {label}")
    return value


def _hash(value: str | None, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ForgePilotError("storage_record", f"Invalid {label}")
    return value


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class MissionRepository:
    """Typed write/read boundary for schema-8 orchestration records."""

    _GRAPH_FIELDS = {
        "status", "task_spec_artifact_id", "plan_artifact_id", "scope_hash",
        "workspace_id", "finished_at",
    }

    def __init__(self, event_store) -> None:
        self.events = event_store

    def create_graph_run(self, *, task_id: str, graph_id: str, graph_version: int,
                         status: str = "pending", task_spec_artifact_id: str | None = None,
                         plan_artifact_id: str | None = None, scope_hash: str | None = None,
                         workspace_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
        task_id = _identifier(task_id, "task ID")
        graph_id = _identifier(graph_id, "graph ID")
        status = _identifier(status, "graph status")
        if type(graph_version) is not int or graph_version < 1:
            raise ForgePilotError("storage_record", "Invalid graph version")
        _hash(scope_hash, "scope hash", optional=True)
        stamp = created_at or _now()
        try:
            with self.events._transaction():
                self.events._db.execute("""INSERT INTO task_graph_runs(
                    task_id,graph_id,graph_version,status,current_revision,
                    task_spec_artifact_id,plan_artifact_id,scope_hash,workspace_id,created_at,updated_at)
                    VALUES(?,?,?,?,1,?,?,?,?,?,?)""", (
                    task_id, graph_id, graph_version, status, task_spec_artifact_id,
                    plan_artifact_id, scope_hash, workspace_id, stamp, stamp,
                ))
            return self.get_graph_run(task_id)
        except sqlite3.IntegrityError:
            raise ForgePilotError("graph_conflict", "Graph run already exists or is invalid") from None

    def get_graph_run(self, task_id: str) -> dict[str, Any]:
        task_id = _identifier(task_id, "task ID")
        with self.events._lock:
            result = _row(self.events._db.execute(
                "SELECT * FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone())
        if result is None:
            raise ForgePilotError("unknown_graph", "Graph run does not exist")
        return result

    def update_graph_run(self, task_id: str, *, previous_revision: int, **changes) -> dict[str, Any]:
        task_id = _identifier(task_id, "task ID")
        if not changes or set(changes) - self._GRAPH_FIELDS:
            raise ForgePilotError("storage_record", "Graph update contains unsupported fields")
        if "status" in changes:
            _identifier(changes["status"], "graph status")
        if "scope_hash" in changes:
            _hash(changes["scope_hash"], "scope hash", optional=True)
        assignments = ",".join(f"{field}=?" for field in changes)
        values = [changes[field] for field in changes]
        try:
            with self.events._transaction():
                changed = self.events._db.execute(
                    f"UPDATE task_graph_runs SET {assignments},current_revision=current_revision+1,updated_at=? "
                    "WHERE task_id=? AND current_revision=?",
                    (*values, _now(), task_id, previous_revision),
                ).rowcount
                if changed != 1:
                    raise ForgePilotError("graph_conflict", "Graph revision changed concurrently")
            return self.get_graph_run(task_id)
        except sqlite3.Error:
            raise ForgePilotError("graph_storage", "Graph run was not saved") from None

    def start_node_attempt(self, *, task_id: str, node_id: str, attempt: int,
                           node_type: str, status: str, idempotency_key: str,
                           input_hash: str, agent_id: str | None = None,
                           started_at: str | None = None) -> dict[str, Any]:
        for value, label in ((task_id, "task ID"), (node_id, "node ID"),
                             (node_type, "node type"), (status, "node status")):
            _identifier(value, label)
        _hash(input_hash, "input hash")
        _hash(idempotency_key, "idempotency key")
        if type(attempt) is not int or attempt < 1:
            raise ForgePilotError("storage_record", "Invalid node attempt")
        try:
            with self.events._transaction():
                self.events._db.execute("""INSERT INTO task_node_attempts(
                    task_id,node_id,attempt,node_type,agent_id,status,idempotency_key,input_hash,started_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (
                    task_id, node_id, attempt, node_type, agent_id, status,
                    idempotency_key, input_hash, started_at or _now(),
                ))
            return self.get_node_attempt(task_id, node_id, attempt)
        except sqlite3.IntegrityError:
            raise ForgePilotError("node_conflict", "Node attempt already exists or has no graph run") from None

    def get_node_attempt(self, task_id: str, node_id: str, attempt: int) -> dict[str, Any]:
        with self.events._lock:
            result = _row(self.events._db.execute(
                "SELECT * FROM task_node_attempts WHERE task_id=? AND node_id=? AND attempt=?",
                (task_id, node_id, attempt),).fetchone())
        if result is None:
            raise ForgePilotError("unknown_node_attempt", "Node attempt does not exist")
        return result

    def finish_node_attempt(self, task_id: str, node_id: str, attempt: int, *, status: str,
                            duration_ms: int, error_code: str | None = None,
                            error_summary: str | None = None, checkpoint_id: str | None = None,
                            finished_at: str | None = None) -> dict[str, Any]:
        _identifier(status, "node status")
        if type(duration_ms) is not int or duration_ms < 0:
            raise ForgePilotError("storage_record", "Invalid node duration")
        summary = self.events.redactor.text(error_summary)[:2_048] if error_summary else None
        with self.events._transaction():
            changed = self.events._db.execute("""UPDATE task_node_attempts SET
                status=?,finished_at=?,duration_ms=?,error_code=?,error_summary=?,checkpoint_id=?
                WHERE task_id=? AND node_id=? AND attempt=? AND finished_at IS NULL""", (
                status, finished_at or _now(), duration_ms, error_code, summary, checkpoint_id,
                task_id, node_id, attempt,
            )).rowcount
            if changed != 1:
                raise ForgePilotError("node_conflict", "Node attempt is missing or already finished")
        return self.get_node_attempt(task_id, node_id, attempt)

    def save_approval(self, *, approval_id: str, task_id: str, task_spec_artifact_id: str,
                      plan_artifact_id: str, scope_hash: str, approved_by: str,
                      approval_type: str = "execution", created_at: str | None = None) -> dict[str, Any]:
        for value, label in ((approval_id, "approval ID"), (task_id, "task ID"),
                             (approved_by, "approver"), (approval_type, "approval type")):
            _identifier(value, label)
        _hash(scope_hash, "scope hash")
        try:
            with self.events._transaction():
                graph = self.events._db.execute(
                    "SELECT task_spec_artifact_id,plan_artifact_id,scope_hash FROM task_graph_runs WHERE task_id=?",
                    (task_id,),).fetchone()
                artifacts = self.events._db.execute("""SELECT artifact_id,artifact_type,content_sha256
                    FROM artifacts WHERE artifact_id IN (?,?) AND task_id=?""",
                    (task_spec_artifact_id, plan_artifact_id, task_id),).fetchall()
                by_id = {item["artifact_id"]: item for item in artifacts}
                if (graph is None or graph["task_spec_artifact_id"] != task_spec_artifact_id
                        or graph["plan_artifact_id"] != plan_artifact_id or graph["scope_hash"] != scope_hash
                        or task_spec_artifact_id not in by_id or plan_artifact_id not in by_id
                        or by_id[task_spec_artifact_id]["artifact_type"] != "TaskSpec"
                        or by_id[plan_artifact_id]["artifact_type"] != "ExecutionPlan"):
                    raise ForgePilotError("approval_stale", "Approval objects are not the current graph objects")
                self.events._db.execute("""INSERT INTO approvals(
                    approval_id,task_id,approval_type,status,task_spec_artifact_id,task_spec_hash,
                    plan_artifact_id,plan_hash,scope_hash,approved_by,created_at)
                    VALUES(?,?,?,'approved',?,?,?,?,?,?,?)""", (
                    approval_id, task_id, approval_type, task_spec_artifact_id,
                    by_id[task_spec_artifact_id]["content_sha256"], plan_artifact_id,
                    by_id[plan_artifact_id]["content_sha256"], scope_hash, approved_by,
                    created_at or _now(),
                ))
            return self.get_approval(approval_id)
        except sqlite3.IntegrityError:
            raise ForgePilotError("approval_conflict", "Approval already exists or is invalid") from None

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        _identifier(approval_id, "approval ID")
        with self.events._transaction():
            row = self.events._db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ForgePilotError("unknown_approval", "Approval does not exist")
            if row["status"] == "approved":
                current = self.events._db.execute("""SELECT g.task_spec_artifact_id,g.plan_artifact_id,g.scope_hash,
                    ts.content_sha256 AS task_spec_hash,p.content_sha256 AS plan_hash
                    FROM task_graph_runs g
                    LEFT JOIN artifacts ts ON ts.artifact_id=g.task_spec_artifact_id
                    LEFT JOIN artifacts p ON p.artifact_id=g.plan_artifact_id WHERE g.task_id=?""",
                    (row["task_id"],),).fetchone()
                valid = current is not None and all((
                    current["task_spec_artifact_id"] == row["task_spec_artifact_id"],
                    current["plan_artifact_id"] == row["plan_artifact_id"],
                    current["scope_hash"] == row["scope_hash"],
                    current["task_spec_hash"] == row["task_spec_hash"],
                    current["plan_hash"] == row["plan_hash"],
                ))
                if not valid:
                    self.events._db.execute("""UPDATE approvals SET status='invalidated',
                        invalidated_at=?,invalidation_reason='approved_objects_changed'
                        WHERE approval_id=? AND status='approved'""", (_now(), approval_id))
                    row = self.events._db.execute(
                        "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            result = dict(row)
        result["valid"] = result["status"] == "approved"
        return result

    def is_approval_valid(self, approval_id: str) -> bool:
        return bool(self.get_approval(approval_id)["valid"])

    @staticmethod
    def _safe_schema(value: Mapping[str, Any], redactor) -> str:
        forbidden = {
            "reasoning", "chain_of_thought", "hidden_thoughts", "thoughts",
            "dpapi", "dpapi_plaintext", "api_key", "apikey", "password",
            "authorization", "access_token", "refresh_token", "client_secret",
        }

        def clean(item):
            if isinstance(item, Mapping):
                result = {}
                for key, content in item.items():
                    name = str(key)
                    normalized = name.casefold().replace("-", "_")
                    sensitive = normalized in forbidden or "dpapi" in normalized
                    result[name] = "[REDACTED]" if sensitive else clean(content)
                return result
            if isinstance(item, (list, tuple)):
                return [clean(content) for content in item]
            return item

        try:
            redacted = redactor.data(clean(value))
            encoded = canonical_json_bytes(redacted)
        except (TypeError, ValueError, RecursionError):
            raise ForgePilotError("model_call_record", "Tool schema is not valid JSON") from None
        if len(encoded) > 1_048_576:
            raise ForgePilotError("model_call_record", "Tool schema is too large")
        return encoded.decode("utf-8")

    def record_model_call(self, *, call_id: str, task_id: str, role: str, model: str,
                          prompt_version: str, input_hash: str, tool_schema: Mapping[str, Any],
                          duration_ms: int, input_tokens: int | None = None,
                          output_tokens: int | None = None, total_tokens: int | None = None,
                          error_code: str | None = None, retry_count: int = 0,
                          fallback_model: str | None = None, response_hash: str | None = None,
                          node_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
        for value, label in ((call_id, "model call ID"), (task_id, "task ID"),
                             (role, "model role"), (model, "model"),
                             (prompt_version, "prompt version")):
            _identifier(value, label)
            if self.events.redactor.text(value) != value:
                raise ForgePilotError("model_call_record", f"{label} contains sensitive data")
        if fallback_model is not None:
            _identifier(fallback_model, "fallback model")
            if self.events.redactor.text(fallback_model) != fallback_model:
                raise ForgePilotError("model_call_record", "fallback model contains sensitive data")
        if error_code is not None:
            safe_error = self.events.redactor.text(error_code)
            error_code = "redacted_error" if safe_error != error_code else _identifier(error_code, "error code")
        _hash(input_hash, "input hash")
        _hash(response_hash, "response hash", optional=True)
        counts = (duration_ms, retry_count, input_tokens, output_tokens, total_tokens)
        if any(value is not None and (type(value) is not int or value < 0) for value in counts):
            raise ForgePilotError("model_call_record", "Model timing and token counts must be non-negative integers")
        schema_json = self._safe_schema(tool_schema, self.events.redactor)
        try:
            with self.events._transaction():
                self.events._db.execute("""INSERT INTO model_calls(
                    call_id,task_id,node_id,role,model,prompt_version,input_hash,tool_schema_json,
                    duration_ms,input_tokens,output_tokens,total_tokens,error_code,retry_count,
                    fallback_model,response_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    call_id, task_id, node_id, role, model, prompt_version, input_hash,
                    schema_json, duration_ms, input_tokens, output_tokens, total_tokens,
                    error_code, retry_count, fallback_model, response_hash, created_at or _now(),
                ))
            return self.get_model_call(call_id)
        except sqlite3.IntegrityError:
            raise ForgePilotError("model_call_conflict", "Model call already exists or is invalid") from None

    def get_model_call(self, call_id: str) -> dict[str, Any]:
        with self.events._lock:
            result = _row(self.events._db.execute(
                "SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone())
        if result is None:
            raise ForgePilotError("unknown_model_call", "Model call does not exist")
        result["tool_schema"] = json.loads(result.pop("tool_schema_json"))
        return result

    def record_checkpoint(self, *, checkpoint_id: str, task_id: str, checkpoint_type: str,
                          status: str, workspace_id: str | None = None,
                          commit_hash: str | None = None, artifact_id: str | None = None,
                          error_code: str | None = None, created_at: str | None = None) -> dict[str, Any]:
        return self._insert_record("checkpoints", "checkpoint_id", checkpoint_id, {
            "checkpoint_id": checkpoint_id, "task_id": task_id, "checkpoint_type": checkpoint_type,
            "status": status, "workspace_id": workspace_id, "commit_hash": commit_hash,
            "artifact_id": artifact_id, "created_at": created_at or _now(), "error_code": error_code,
        })

    def save_workspace(self, *, workspace_id: str, task_id: str, repository_path: str,
                       worktree_path: str, base_ref: str, status: str,
                       head_commit: str | None = None, created_at: str | None = None) -> dict[str, Any]:
        stamp = created_at or _now()
        return self._insert_record("workspace_records", "workspace_id", workspace_id, {
            "workspace_id": workspace_id, "task_id": task_id, "repository_path": repository_path,
            "worktree_path": worktree_path, "base_ref": base_ref, "head_commit": head_commit,
            "status": status, "created_at": stamp, "updated_at": stamp, "released_at": None,
        })

    def record_validation(self, *, validation_id: str, task_id: str, attempt: int, status: str,
                          started_at: str | None = None, finished_at: str | None = None,
                          duration_ms: int | None = None, report_artifact_id: str | None = None,
                          error_code: str | None = None) -> dict[str, Any]:
        return self._insert_record("validation_runs", "validation_id", validation_id, {
            "validation_id": validation_id, "task_id": task_id, "attempt": attempt,
            "status": status, "report_artifact_id": report_artifact_id,
            "started_at": started_at or _now(), "finished_at": finished_at,
            "duration_ms": duration_ms, "error_code": error_code,
        })

    def record_security_event(self, *, event_id: str, event_type: str, severity: str,
                              attributes: Mapping[str, Any] | None = None,
                              task_id: str | None = None, occurred_at: str | None = None) -> dict[str, Any]:
        # Security records retain bounded, redacted attributes, never the triggering payload.
        attributes_json = json.dumps(
            self.events.redactor.data(dict(attributes or {})), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        if len(attributes_json.encode("utf-8")) > 16_384:
            raise ForgePilotError("security_event_record", "Security event attributes are too large")
        return self._insert_record("security_events", "event_id", event_id, {
            "event_id": event_id, "task_id": task_id, "event_type": event_type,
            "severity": severity, "occurred_at": occurred_at or _now(),
            "attributes_json": attributes_json,
        }, json_field="attributes_json")

    def _insert_record(self, table: str, key_name: str, key_value: str,
                       values: dict[str, Any], *, json_field: str | None = None) -> dict[str, Any]:
        allowed = {"checkpoints", "workspace_records", "validation_runs", "security_events"}
        if table not in allowed:
            raise AssertionError("unsupported repository table")
        _identifier(key_value, key_name)
        columns = tuple(values)
        try:
            with self.events._transaction():
                self.events._db.execute(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            with self.events._lock:
                result = dict(self.events._db.execute(
                    f"SELECT * FROM {table} WHERE {key_name}=?", (key_value,)).fetchone())
            if json_field:
                result[json_field.removesuffix("_json")] = json.loads(result.pop(json_field))
            return result
        except (sqlite3.IntegrityError, TypeError, ValueError):
            raise ForgePilotError("storage_record", f"Invalid or duplicate {table} record") from None


__all__ = ["MissionRepository", "create_mission_schema"]
