"""One local writer, versioned task/conversation tables and bounded history reads."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock

from .api_models import EventPayload, TaskSummary
from .errors import ForgePilotError
from .security import Redactor
from .trace import TraceEvent
from .conversation_store import ConversationStore
from .memory_store import MemoryStore
from .mcp_store import create_mcp_schema
from .rag.store import create_rag_schema, create_rag_run_schema, recover_rag_runs
from .mission_store import MissionRepository, create_mission_schema
from .artifacts.sqlite_store import SqliteArtifactStore
from .schema_versions import CURRENT_SCHEMA_VERSION, SchemaVersion, requires_upgrade_backup
from .history_cursor import encode_cursor, decode_cursor
from uuid import uuid4

TERMINAL = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class StoredEvent:
    id: str
    sequence: int
    payload: EventPayload

    def frame(self):
        return f"id: {self.id}\nevent: {self.payload.event}\ndata: {self.payload.model_dump_json()}\n\n"


def database_path():
    configured = os.environ.get("FORGEPILOT_DATABASE")
    return Path(configured) if configured else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ForgePilot" / "tasks.sqlite3"


class SqliteEventStore(ConversationStore, MemoryStore):
    def __init__(self, path=":memory:", *, redactor=None, artifact_root=None):
        self.path = str(path)
        self.redactor = redactor or Redactor()
        self._lock = RLock()
        self._waiters = {}
        self._live = set()
        self._unavailable_tasks: set[str] = set()
        self._unavailable_requests: set[str] = set()
        self._lease = None
        self._db = None
        self.migration_backup_path = None
        try:
            if self.path != ":memory:":
                path = Path(path).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                self.path = str(path)
                # A lifetime file lock avoids marking another running process's tasks interrupted.
                # Unlike a Windows mutex, this handle may be released on the shutdown thread.
                import msvcrt
                self._lease = open(str(path) + ".lock", "a+b")
                if self._lease.seek(0, 2) == 0:
                    self._lease.write(b"0"); self._lease.flush()
                self._lease.seek(0)
                try:
                    msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ForgePilotError("storage_busy", "Database already belongs to a running CLI or server; use another --database path") from None
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._migrate()
            self.artifacts = SqliteArtifactStore(self, artifact_root)
            self.artifact_store = self.artifacts
            self.missions = MissionRepository(self)
            self.mission_repository = self.missions
            self._recover()
            self._recover_conversations()
        except BaseException as exc:
            self.close()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise ForgePilotError("storage_unavailable", "Cannot open or initialize task database") from None
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _migrate(self):
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if requires_upgrade_backup(version) and self.path != ':memory:':
            self._assert_integrity("source")
            # Backup includes committed WAL pages and precedes both migration and recovery.
            backup = self.path + f'.v{version}-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8] + '.sqlite3'
            with sqlite3.connect(backup) as destination:
                self._db.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ForgePilotError('storage_backup', '数据库备份校验失败，未执行升级。')
            self.migration_backup_path = backup
        with self._transaction():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > int(CURRENT_SCHEMA_VERSION):
                raise ForgePilotError("storage_version", "Database schema is newer than this application")
            if version == 0:
                if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                    raise ForgePilotError("storage_version", "Unversioned nonempty database is not a ForgePilot database")
                statements = [
                    """CREATE TABLE tasks(task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                        state TEXT NOT NULL, updated_at TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0,
                        snapshot_json TEXT NOT NULL, summary_json TEXT NOT NULL,
                        event_sequence INTEGER NOT NULL DEFAULT 0, sse_sequence INTEGER NOT NULL DEFAULT 0,
                        trace_sequence INTEGER NOT NULL DEFAULT 0)""",
                    "CREATE INDEX tasks_history ON tasks(created_at DESC,task_id DESC)",
                    """CREATE TABLE task_events(task_id TEXT NOT NULL REFERENCES tasks(task_id),
                        sequence INTEGER NOT NULL, kind TEXT NOT NULL, timestamp TEXT NOT NULL,
                        data_json TEXT NOT NULL, sse_sequence INTEGER, trace_sequence INTEGER,
                        PRIMARY KEY(task_id,sequence), UNIQUE(task_id,sse_sequence), UNIQUE(task_id,trace_sequence))""",
                    """CREATE TABLE tool_calls(task_id TEXT NOT NULL REFERENCES tasks(task_id),
                        sequence INTEGER NOT NULL, name TEXT NOT NULL, call_id TEXT, source TEXT,
                        repair_round INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                        duration_ms INTEGER, status TEXT NOT NULL, arguments_json TEXT NOT NULL,
                        result_json TEXT, start_event_sequence INTEGER NOT NULL, end_event_sequence INTEGER,
                        PRIMARY KEY(task_id,sequence),
                        FOREIGN KEY(task_id,start_event_sequence) REFERENCES task_events(task_id,sequence),
                        FOREIGN KEY(task_id,end_event_sequence) REFERENCES task_events(task_id,sequence))""",
                ]
                for statement in statements:
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version={int(SchemaVersion.TASK_HISTORY)}")
            if version < SchemaVersion.CONVERSATIONS:
                self._conversation_schema()
                self._db.execute(f"PRAGMA user_version={int(SchemaVersion.CONVERSATIONS)}")
            if version < SchemaVersion.MEMORY:
                self._memory_schema()
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.MEMORY)}')
            if version < SchemaVersion.MCP_SERVERS:
                create_mcp_schema(self._db)
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.MCP_SERVERS)}')
            if version < SchemaVersion.MCP_CALL_RECORDS:
                self._db.execute("ALTER TABLE conversation_requests ADD COLUMN mcp_calls_json TEXT NOT NULL DEFAULT '[]'")
                self._db.execute("CREATE TABLE mcp_migrations(name TEXT PRIMARY KEY, completed_at TEXT NOT NULL)")
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.MCP_CALL_RECORDS)}')
            if version < SchemaVersion.RAG_FILES:
                create_rag_schema(self._db)
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.RAG_FILES)}')
            if version < SchemaVersion.RAG_INDEX_LIFECYCLE:
                create_rag_run_schema(self._db)
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.RAG_INDEX_LIFECYCLE)}')
            if version < SchemaVersion.GRAPH_ARTIFACT_STORE:
                create_mission_schema(self._db)
                self._db.execute(f'PRAGMA user_version={int(SchemaVersion.GRAPH_ARTIFACT_STORE)}')
            migrated = self._db.execute("PRAGMA user_version").fetchone()[0]
            if migrated != int(CURRENT_SCHEMA_VERSION):
                raise ForgePilotError(
                    "storage_version", "Database migration path does not reach the current schema")
            self._assert_integrity("migrated")

    def _assert_integrity(self, stage):
        try:
            rows = self._db.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error:
            raise ForgePilotError("storage_integrity", f"Database {stage} integrity check failed") from None
        if len(rows) != 1 or rows[0][0] != "ok":
            raise ForgePilotError("storage_integrity", f"Database {stage} integrity check failed")

    def _json(self, value):
        return json.dumps(self.redactor.data(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def _row(self, task_id):
        self._ensure_task_available(task_id)
        row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ForgePilotError("unknown_task", "Task does not exist in this database")
        return row

    def _ensure_task_available(self, task_id):
        with self._lock:
            if task_id in self._unavailable_tasks:
                raise ForgePilotError("storage_unavailable", "Task persistence failed; durable status is unavailable")

    def mark_task_unavailable(self, task_id):
        with self._lock:
            self._unavailable_tasks.add(task_id)
            self._live.discard(task_id)
            self._notify(task_id)

    def mark_request_unavailable(self, request_id):
        with self._lock:
            self._unavailable_requests.add(request_id)

    def _summary(self, snapshot):
        return TaskSummary.model_validate(snapshot).model_dump(mode="json")

    def create(self, task_id, snapshot, *, request_id=None):
        snapshot = self.redactor.data(snapshot)
        if snapshot["task_id"] != task_id:
            raise ForgePilotError("storage_identity", "Snapshot identity differs from task")
        with self._transaction():
            try:
                self._db.execute("INSERT INTO tasks(task_id,created_at,state,updated_at,snapshot_json,summary_json) VALUES(?,?,?,?,?,?)",
                    (task_id,snapshot["created_at"],snapshot["state"],snapshot["updated_at"],self._json(snapshot),self._json(self._summary(snapshot))))
                if request_id:
                    changed=self._db.execute("UPDATE conversation_requests SET task_id=? WHERE request_id=? AND status='running' AND task_id IS NULL", (task_id,request_id)).rowcount
                    if changed!=1:
                        raise ForgePilotError('request_conflict', '执行请求已绑定任务。')
                else:
                    self._import_task(snapshot)
            except sqlite3.IntegrityError:
                raise ForgePilotError("duplicate_task", "Task identity already exists") from None
        with self._lock:
            self._live.add(task_id)

    def _tool(self, task_id, trace, event_sequence):
        data = self.redactor.data(trace.data)
        if trace.kind == "tool_started":
            self._db.execute("""INSERT INTO tool_calls(task_id,sequence,name,call_id,source,repair_round,
                started_at,status,arguments_json,start_event_sequence) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (task_id,trace.sequence,data["name"],data.get("tool_call_id"),data.get("source"),data.get("repair_round",0),
                 trace.timestamp,"running",self._json(data.get("arguments",{})),event_sequence))
        elif trace.kind == "tool_finished":
            row = self._db.execute("""SELECT sequence FROM tool_calls WHERE task_id=? AND name=? AND status='running'
                AND call_id IS ? AND source IS ? AND repair_round=? ORDER BY sequence LIMIT 1""",
                (task_id,data["name"],data.get("tool_call_id"),data.get("source"),data.get("repair_round",0))).fetchone()
            if row is None:
                raise ForgePilotError("storage_trace", "Tool result has no matching tool start")
            status = "succeeded" if data.get("success") else "cancelled" if str(data.get("error","")).startswith("cancelled") else "failed"
            self._db.execute("""UPDATE tool_calls SET finished_at=?,duration_ms=?,status=?,result_json=?,end_event_sequence=?
                WHERE task_id=? AND sequence=?""",(trace.timestamp,data.get("duration_ms"),status,self._json(data),event_sequence,task_id,row[0]))

    def _publish(self, task_id, snapshot, events, traces, closed, *, amend=False):
        row = self._row(task_id)
        if row["closed"] and not amend:
            return False
        if amend and (not row["closed"] or snapshot["state"] != row["state"] or events):
            raise ForgePilotError("invalid_state", "Closed task amendments cannot restart execution or publish SSE")
        if snapshot["task_id"] != task_id or any(t.task_id != task_id for t in traces) or any(e.task_id != task_id for e in events):
            raise ForgePilotError("storage_identity", "Events and snapshot must belong to the same task")
        # Retrying an already committed trace batch is a no-op, including its SSE projection.
        if traces and all(t.sequence <= row["trace_sequence"] for t in traces):
            return False
        sequence, sse, trace_sequence = row["event_sequence"], row["sse_sequence"], row["trace_sequence"]
        for trace in traces:
            if trace.sequence != trace_sequence + 1:
                raise ForgePilotError("storage_sequence", "Trace order must be contiguous")
            trace_sequence = trace.sequence; sequence += 1
            self._db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?,?)",
                (task_id,sequence,trace.kind,trace.timestamp,self._json(trace.data),None,trace_sequence))
            if trace.kind in {"tool_started", "tool_finished"}:
                self._tool(task_id,trace,sequence)
        for event in events:
            sequence += 1; sse += 1
            self._db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?,?)",
                (task_id,sequence,event.event,event.timestamp,self._json(event.data),sse,None))
        snapshot = self.redactor.data(snapshot)
        snapshot["last_event_id"] = f"{task_id}:{sse}" if sse else None
        snapshot["tool_calls"] = self._db.execute("SELECT COUNT(*) FROM tool_calls WHERE task_id=?",(task_id,)).fetchone()[0]
        files = (snapshot.get("task_diff") or {}).get("files",[])
        snapshot["diff_summary"] = [{"path":f["path"],"created":bool(f.get("created")),"conflict":bool(f.get("conflict"))} for f in files]
        self._db.execute("""UPDATE tasks SET state=?,updated_at=?,closed=?,snapshot_json=?,summary_json=?,
            event_sequence=?,sse_sequence=?,trace_sequence=? WHERE task_id=?""",
            (snapshot["state"],snapshot["updated_at"],int(closed or row["closed"]),self._json(snapshot),
             self._json(self._summary(snapshot)),sequence,sse,trace_sequence,task_id))
        if closed:
            self._db.execute("""UPDATE tool_calls SET status=?,finished_at=? WHERE task_id=? AND status='running'""",
                ("cancelled" if snapshot["state"]=="cancelled" else "interrupted",snapshot.get("finished_at"),task_id))
            self._sync_conversation_task(snapshot)
        return True

    def publish(self, task_id, snapshot, events, *, closed=False, traces=()):
        with self._transaction():
            changed = self._publish(task_id,snapshot,events,traces,closed)
        if changed:
            self._notify(task_id)

    def amend_closed(self, task_id, snapshot, traces=()):
        """CLI-only explicit restore metadata; never restores execution or authority."""
        with self._transaction():
            self._publish(task_id,snapshot,[],traces,True,amend=True)

    def _recover(self):
        with self._transaction():
            recover_rag_runs(self._db)
            for row in self._db.execute("SELECT * FROM tasks WHERE closed=0").fetchall():
                stamp = datetime.now(timezone.utc).isoformat()
                snapshot = json.loads(row["snapshot_json"])
                previous = {"state":snapshot["state"],"error":snapshot.get("error"),"outcome":snapshot.get("outcome")}
                message = "service_interrupted: Previous process stopped before final publication; execution is not resumed"
                snapshot.update(state="failed",error=message,error_code="service_interrupted",outcome="service_interrupted",
                    failure_kind="service_interrupted",updated_at=stamp,finished_at=stamp,duration_complete=False)
                trace = TraceEvent(row["trace_sequence"]+1,row["task_id"],"service_interrupted",stamp,previous)
                events = [EventPayload(event=kind,task_id=row["task_id"],timestamp=stamp,
                    data={"state":"failed","error":message,"error_code":"service_interrupted","outcome":"service_interrupted"})
                    for kind in ("task_state_changed","task_failed")]
                self._publish(row["task_id"],snapshot,events,[trace],True)

    def get(self, task_id):
        with self._lock:
            result = json.loads(self._row(task_id)["snapshot_json"])
            result["read_only"] = task_id not in self._live
            return result

    def list_page(self, limit=20, before=None, conversation_id=None):
        if not 1 <= limit <= 100:
            raise ForgePilotError("invalid_page", "limit must be between 1 and 100")
        with self._lock:
            params = []
            where = ""
            if before:
                boundary=decode_cursor(before)
                if boundary is None:
                    boundary=self._db.execute("SELECT created_at,task_id FROM tasks WHERE task_id=?",(before,)).fetchone()
                if boundary is None:
                    raise ForgePilotError("invalid_page", "History cursor does not exist")
                where="WHERE (created_at,task_id) < (?,?)"; params.extend(boundary)
            if conversation_id:
                self._conversation(conversation_id)
                where+=(' AND ' if where else 'WHERE ')+"task_id IN (SELECT task_id FROM conversation_requests WHERE conversation_id=?)"
                params.append(conversation_id)
            rows=self._db.execute("SELECT task_id,summary_json FROM tasks "+where+" ORDER BY created_at DESC,task_id DESC LIMIT ?",(*params,limit+1)).fetchall()
            items=[]
            for row in rows[:limit]:
                self._ensure_task_available(row["task_id"])
                value=json.loads(row["summary_json"]); value["read_only"]=row["task_id"] not in self._live; items.append(value)
            total=self._db.execute("SELECT COUNT(*) FROM conversation_requests WHERE conversation_id=? AND task_id IS NOT NULL",(conversation_id,)).fetchone()[0] if conversation_id else self._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            return {"items":items,"total":total,
                "next_cursor":encode_cursor(items[-1]['created_at'],items[-1]['task_id']) if len(rows)>limit else None}

    def list(self):
        """Compatibility helper for test evidence; HTTP always uses list_page."""
        with self._lock:
            return [self.get(r[0]) for r in self._db.execute("SELECT task_id FROM tasks ORDER BY created_at DESC,task_id DESC").fetchall()]

    def read(self, task_id, after):
        with self._lock:
            row=self._row(task_id)
            events=self._db.execute("SELECT * FROM task_events WHERE task_id=? AND sse_sequence>? ORDER BY sse_sequence LIMIT 64",(task_id,after)).fetchall()
            return [StoredEvent(f"{task_id}:{e['sse_sequence']}",e["sse_sequence"],EventPayload(event=e["kind"],task_id=task_id,
                timestamp=e["timestamp"],data=json.loads(e["data_json"]))) for e in events],bool(row["closed"])

    def cursor(self, task_id, event_id):
        with self._lock:
            row=self._row(task_id)
            if not event_id: return 0
            prefix,sep,number=event_id.partition(":")
            if not sep or prefix!=task_id or not number.isascii() or not number.isdigit() or len(number)>10 or str(int(number))!=number or int(number)<1:
                raise ForgePilotError("invalid_event_id", "Last-Event-ID must be an emitted ID belonging to this task")
            if int(number)>row["sse_sequence"]:
                raise ForgePilotError("event_cursor_ahead", "Last-Event-ID is newer than this task's event stream")
            return int(number)

    def trace_page(self, task_id, after=0, limit=50):
        with self._lock:
            self._row(task_id)
            rows=self._db.execute("""SELECT sequence,trace_sequence,kind,timestamp,substr(data_json,1,240) AS preview,
                length(data_json) AS size FROM task_events WHERE task_id=? AND trace_sequence IS NOT NULL AND sequence>?
                ORDER BY sequence LIMIT ?""",(task_id,after,limit+1)).fetchall()
            items=[dict(r) for r in rows[:limit]]
            return {"items":items,"next_cursor":items[-1]["sequence"] if len(rows)>limit else None}

    def trace_detail(self, task_id, sequence):
        with self._lock:
            self._row(task_id)
            row=self._db.execute("SELECT sequence,trace_sequence,kind,timestamp,data_json FROM task_events WHERE task_id=? AND sequence=? AND trace_sequence IS NOT NULL",(task_id,sequence)).fetchone()
            if row is None: raise ForgePilotError("unknown_trace", "Trace event does not exist for this task")
            result=dict(row); result["data"]=json.loads(result.pop("data_json")); result["task_id"]=task_id
            return result

    def _notify(self, task_id):
        with self._lock:
            for loop,signal in self._waiters.get(task_id,set()):
                try: loop.call_soon_threadsafe(signal.set)
                except RuntimeError: pass

    def waiter_count(self, task_id):
        with self._lock: return len(self._waiters.get(task_id,set()))

    async def wait(self, task_id, after, timeout):
        waiter=(asyncio.get_running_loop(),asyncio.Event())
        # Disk queries run off the HTTP loop; registration and the cursor check share the writer lock.
        def register():
            with self._lock:
                row=self._row(task_id)
                if row["closed"] or row["sse_sequence"]>after: return False
                self._waiters.setdefault(task_id,set()).add(waiter); return True
        registration=asyncio.create_task(asyncio.to_thread(register))
        try:
            registered=await asyncio.shield(registration)
            if registered:
                try: await asyncio.wait_for(waiter[1].wait(),timeout)
                except TimeoutError: pass
            self._ensure_task_available(task_id)
        finally:
            await registration
            with self._lock:
                waiters=self._waiters.get(task_id,set()); waiters.discard(waiter)
                if not waiters: self._waiters.pop(task_id,None)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close(); self._db=None
            if self._lease is not None:
                self._lease.close(); self._lease=None
