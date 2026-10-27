"""Shared snapshot and public-event projection for CLI and HTTP persistence."""
from datetime import datetime, timezone

from .api_models import EventPayload, TaskResponse
from .trace import TraceEvent

TERMINAL = {"completed", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


def snapshot_context(context, *, created_at, updated_at, finished_at=None, cancel_requested=False):
    values = {name: getattr(context, name) for name in (
        "task_id", "repository_path", "state", "plan", "plan_version", "approved_plan_version",
        "analysis_result", "final_result", "steps", "tool_calls", "duration_ms", "outcome", "error",
        "repair_round", "max_repair_rounds", "repair_records", "validation_history", "last_validation_error",
        "failure_kind", "validation_errors", "build_result", "test_integrity", "task_diff",
        "rag_requested", "rag_used", "rag_status")}
    values.update(prompt=context.user_request, approved_files=sorted(context.approved_files),
        usage=context.token_usage, created_at=created_at, updated_at=updated_at,
        finished_at=finished_at, cancel_requested=cancel_requested,
        error_code=context.error.split(":", 1)[0] if context.error else None)
    return TaskResponse(**values).model_dump(mode="json")


def project_events(traces, snapshot):
    events = []
    for trace in traces:
        kind, data = trace.kind, trace.data
        def add(event, content):
            events.append(EventPayload(event=event, task_id=snapshot["task_id"], timestamp=trace.timestamp, data=content))
        if kind in {"task_state_changed", "plan_created", "approval_required", "tool_started", "tool_finished", "file_changed", "reflection"}:
            if kind == "task_state_changed":
                data = {**data,"outcome":snapshot["outcome"],"error":snapshot["error"]}
            add(kind,data)
        if kind == "rag_initial_retrieval":
            add("rag_usage", {
                "rag_used": snapshot["rag_used"],
                "rag_status": snapshot["rag_status"],
            })
        if kind == "tool_finished" and data.get("name") in {"retrieve_code_context", "rag_retrieve"}:
            add("rag_usage", {
                "rag_used": snapshot["rag_used"],
                "rag_status": snapshot["rag_status"],
            })
        if kind == "llm_response":
            message=data.get("message",{})
            if message.get("content") and not message.get("tool_calls") and snapshot["state"] in {"analyzing","modifying"}:
                add("message",{"role":"assistant","content":message["content"],"step":data["step"]})
        if kind == "mcp_status":
            add("message", {"kind": kind, **data, "role": "system", "content":
                f"MCP ready: {data['name']}" if data["status"] == "ready" else
                f"MCP unavailable; built-in tools remain available. {data['error']}"})
        if kind in {"task_error","build_selection"}:
            add("message",{"kind":kind,**data})
        if kind in {"tool_started","tool_finished"} and data.get("source")=="runtime":
            phase=data.get("name")
            if phase in {"cmake_configure","cmake_build","run_tests"}:
                prefix="test" if phase=="run_tests" else "build"
                suffix="started" if kind=="tool_started" else "finished"
                add(f"{prefix}_{suffix}",{"phase":phase,**data,**({"result":snapshot["build_result"]} if suffix=="finished" else {})})
        if kind=="task_finished" and snapshot["state"] in {"completed","failed"}:
            add("task_completed" if snapshot["state"]=="completed" else "task_failed",
                {**data,"final_result":snapshot["final_result"],"error_code":snapshot["error_code"]})
    return events


class TaskJournal:
    """CLI trace sink. Runtime owns metrics; the store never replays business operations."""
    def __init__(self, store, snapshot, redactor, on_event=None):
        self.store, self.snapshot, self.redactor, self.on_event=store,snapshot,redactor,on_event
        self.sequence=0
        self.created_at=now()
        self.pending=[]
        self.created=False
        self.closed=False
        self.finished_at=None

    def _snapshot(self):
        context=self.snapshot()
        if context is None: return None
        return self.redactor.data(snapshot_context(context,created_at=self.created_at,updated_at=now(),finished_at=self.finished_at))

    def emit(self, task_id, kind, data):
        self.sequence+=1
        event=TraceEvent(self.sequence,task_id,kind,now(),self.redactor.data(data))
        self.pending.append(event)
        if self.on_event: self.on_event(event)
        snapshot=self._snapshot()
        if not self.created:
            self.store.create(task_id,snapshot); self.created=True
        if snapshot["state"] not in TERMINAL:
            self._flush(snapshot)

    def flush(self):
        if self.created:
            self._flush(self._snapshot())

    def _flush(self, snapshot):
        terminal=snapshot["state"] in TERMINAL
        if terminal and not self.finished_at:
            self.finished_at=now(); snapshot["finished_at"]=self.finished_at
        if self.closed:
            self.store.amend_closed(snapshot["task_id"],snapshot,self.pending)
        else:
            self.store.publish(snapshot["task_id"],snapshot,project_events(self.pending,snapshot),traces=self.pending,closed=terminal)
        self.pending=[]; self.closed=terminal
