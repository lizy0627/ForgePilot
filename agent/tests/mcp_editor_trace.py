"""Opt-in, test-only observations for the two-editor HTTP timeout.

Never retain HTTP trace payloads, headers, exception messages, source text or
frame locals. Coroutine/frame inspection is synchronous so a blocked worker
loop can still be observed from the test loop before disconnect cleanup.
"""
import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
import sys
from threading import RLock, current_thread, get_ident
from time import monotonic_ns
from uuid import uuid4

import httpcore


class EditorTrace:
    header_name = "x-forgepilot-editor-trace"
    editors = ("editor-a", "editor-b")

    def __init__(self, path):
        self.path = Path(path)
        self.timeout_paths = {editor: self.path.with_name(f"{self.path.stem}-timeout-{editor}.json")
                              for editor in self.editors}
        for target in (self.path, *self.timeout_paths.values()):
            if target.exists() or target.is_symlink():
                raise FileExistsError(target)
        self.editor = ContextVar("mcp_editor_trace", default=None)
        self.origin_ns = monotonic_ns()
        self.events = []
        self._tasks = {}
        self._threads = {get_ident(): current_thread().name}
        self._lock = RLock()
        self._header_values = {name: f"{uuid4().hex}:{name}" for name in self.editors}

    def event(self, event, **fields):
        editor = fields.pop("editor", self.editor.get())
        if editor not in (None, *self.editors):
            raise ValueError("Unknown diagnostic editor")
        with self._lock:
            self.events.append({"ns": monotonic_ns() - self.origin_ns,
                                "event": event, "editor": editor,
                                "thread": current_thread().name, **fields})

    def track(self, role, task):
        if task is not None:
            with self._lock:
                self._tasks[(role, id(task))] = (self.editor.get(), task)
                self._threads[get_ident()] = current_thread().name
        return task

    @staticmethod
    def _frame(frame):
        return {"file": frame.f_code.co_filename, "function": frame.f_code.co_name,
                "line": frame.f_lineno}

    @classmethod
    def _await_stack(cls, task):
        result, seen = [], set()
        pending = task.get_coro()
        while pending is not None and id(pending) not in seen and len(result) < 64:
            seen.add(id(pending))
            frame = getattr(pending, "cr_frame", None) or getattr(pending, "gi_frame", None)
            if frame is not None:
                result.append(cls._frame(frame))
            pending = getattr(pending, "cr_await", None) or getattr(pending, "gi_yieldfrom", None)
        return result

    def snapshot(self, reason):
        with self._lock:
            tracked = list(self._tasks.items())
            threads = dict(self._threads)
        tasks = []
        for (role, task_id), (editor, task) in tracked:
            tasks.append({"role": role, "task": task_id, "editor": editor,
                          "done": task.done(), "cancelled": task.cancelled(),
                          "cancelling": task.cancelling(), "await_stack": self._await_stack(task)})
        stacks = []
        for thread_id, frame in sys._current_frames().items():
            if thread_id not in threads:
                continue
            stack = []
            while frame is not None and len(stack) < 64:
                stack.append(self._frame(frame))
                frame = frame.f_back
            stacks.append({"thread": threads[thread_id], "stack": stack})
        self.event("task_snapshot", reason=reason, tasks=tasks, threads=stacks)

    def request_options(self, editor):
        if editor not in self.editors:
            raise ValueError("Unknown diagnostic editor")

        async def observe(name, info):
            stage = {
                "http11.send_request_body.complete": "request_body_sent",
                "http11.receive_response_headers.started": "response_headers_wait",
                "http11.receive_response_headers.complete": "response_headers_received",
                "http11.receive_response_headers.failed": "response_headers_failed",
            }.get(name)
            if stage is None:
                return
            token = self.editor.set(editor)
            try:
                self.track("client", asyncio.current_task())
                exception = info.get("exception")
                self.event(stage, **({"error": type(exception).__name__} if exception else {}))
                if stage == "response_headers_failed" and isinstance(exception, httpcore.ReadTimeout):
                    try:
                        self.snapshot("response_headers_read_timeout")
                        self._write(self.timeout_paths[editor])
                    except Exception as failure:
                        # Diagnostics must not replace the original transport error.
                        self.event("timeout_evidence_write_failed", error=type(failure).__name__)
            finally:
                self.editor.reset(token)

        return {"headers": {self.header_name: self._header_values[editor]},
                "extensions": {"trace": observe}}

    def install_asgi(self, app):
        trace = self
        editor_headers = {value.encode("ascii"): name for name, value in self._header_values.items()}

        class ResponseTrace:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                editor = None
                if scope["type"] == "http":
                    editor = next((editor_headers.get(value) for key, value in scope.get("headers", ())
                                   if key == trace.header_name.encode("ascii")), None)
                if editor is None:
                    return await self.app(scope, receive, send)
                token = trace.editor.set(editor)
                trace.track("asgi", asyncio.current_task())
                trace.event("asgi_request_entered")

                async def observed_send(message):
                    headers = message["type"] == "http.response.start"
                    if headers:
                        trace.event("asgi_response_headers_send", status=message["status"])
                    await send(message)
                    if headers:
                        trace.event("asgi_response_headers_sent", status=message["status"])

                try:
                    await self.app(scope, receive, observed_send)
                finally:
                    trace.event("asgi_request_finished")
                    trace.editor.reset(token)

        app.add_middleware(ResponseTrace)

    def _write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            events = list(self.events)
        with path.open("x", encoding="utf-8") as output:
            json.dump({"clock": "time.monotonic_ns", "origin_ns": self.origin_ns,
                       "events": events}, output, ensure_ascii=False, indent=2)
            output.write("\n")

    def write(self):
        self._write(self.path)
