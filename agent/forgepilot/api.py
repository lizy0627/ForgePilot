"""Thin FastAPI adapter for the existing task service and ordered session events."""

from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import sqlite3
from time import monotonic
from urllib.parse import urlsplit
from starlette.datastructures import Headers

from fastapi import Body, FastAPI, Header, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException

from .api_models import (ApproveTask, CancelTask, CreateTask, PlanFeedback, ErrorResponse,
                         TaskList, TaskResponse, TracePage, TraceDetail, RagIndexRequest,
                         RagIndexStatusResponse, RagIndexDeleted, checked_rag_repository_path)
from .errors import ForgePilotError
from .task_host import TaskHost
from .conversations import ConversationHost
from .api_models import CreateConversation, UpdateConversation, ConversationRequest
from .api_models import ConversationDetail, ConversationList, ConversationRequestState, ConversationMCPTools
from .api_models import ConversationDeleted
from .api_models import MemoryCreate,MemoryUpdate,MemoryRecord,MemoryList,MemoryDeleted,SummaryUpdate,SummaryState
from .mcp_api import MCPRequestGuard, register_mcp_routes
from .api_v2 import MissionRequestGuard, V2_ERROR_STATUS, register_mission_routes
from .mission_host import MissionHost
from .mission_events import MissionEventStream
from .runtime_control import register_runtime_routes

MAX_API_BODY = 1_048_576
MAX_API_TARGET = 8_192


class APIRequestGuard:
    """Bound legacy request bodies before FastAPI or an endpoint can parse them."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not scope["path"].startswith("/api/v1/")
                or scope["path"].startswith("/api/v1/mcp/")
                or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        lengths = headers.getlist("content-length")
        if lengths and (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()):
            return await error_response("invalid_request", "Invalid request length", 400)(scope, receive, send)
        if lengths and int(lengths[0]) > MAX_API_BODY:
            return await error_response("request_too_large", "Request exceeds 1 MiB", 413)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_API_BODY:
                return await error_response("request_too_large", "Request exceeds 1 MiB", 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, delivered = b"".join(chunks), False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class SafeExceptionGuard:
    """Contain unexpected ASGI failures before Uvicorn can log raw tracebacks."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False
        completed = False

        async def tracked_send(message):
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            # After a streaming response starts, finish its current body. A
            # second HTTP status is illegal; the SSE client can reconnect.
            if not started:
                await error_response("api_internal", "Request failed unexpectedly; inspect task status", 500)(
                    scope, receive, send)
            elif not completed:
                await send({"type": "http.response.body", "body": b"", "more_body": False})

ERROR_STATUS = {
    "step_limit": 409,
    "unknown_task": 404, "repository_busy": 409, "task_busy": 409, "invalid_state": 409,
    "stale_plan": 409, "already_approved": 409, "plan_changed": 409,
    "invalid_event_id": 400, "event_cursor_ahead": 409,
    "conversation_stream_capacity": 409, "conversation_stream_unavailable": 503,
    "conversation_stream_limit": 500, "conversation_stream_state": 500,
    "runtime_busy": 409, "task_capacity": 503, "server_stopping": 503, "llm_config": 503, "build_config": 503,
    "task_internal": 500,
    "unknown_trace": 404, "invalid_page": 400, "storage_busy": 503, "storage_unavailable": 503,
    "unknown_conversation": 404, "unknown_request": 404, "conversation_busy": 409, "request_conflict": 409,
    "conversation_deleting": 409,
    "unknown_memory":404,"memory_conflict":409,"summary_conflict":409,"memory_capacity":409,
    "unknown_mcp_server": 404, "mcp_catalog_changed": 409, "mcp_config_conflict": 409,
    "mcp_capacity": 429, "mcp_timeout": 504, "mcp_disabled": 403, "mcp_storage": 503,
    "mcp_cancelled": 499,
    "rag_disabled": 409, "rag_index_busy": 409, "rag_index_not_running": 409,
    "rag_index_conflict": 409, "rag_index_state": 409,
    "rag_unavailable": 503, "rag_storage": 503, "rag_index_clear_failed": 500,
}


def _checked_rag_query_path(value: str) -> str:
    try:
        return checked_rag_repository_path(value)
    except ValueError:
        raise ForgePilotError("invalid_request", "Request does not match the v1 schema") from None


def error_response(code, message, status, *, details=None, task_id=None):
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "message": message, "details": details or [], "task_id": task_id,
    }})


def create_app(service_factory, *, store=None, max_tasks=32, heartbeat_seconds=15.0,
               conversation_stream_idle_seconds=60.0,
               conversation_stream_retention_seconds=60.0,
               conversation_stream_max_subscribers=8,
               chat_factory=None, summary_factory=None, mcp_disabled=False, legacy_mcp_config=None,
               model_client_closer=None, rag_coordinator=None, mission_factory=None,
               mission_stream_max_subscribers=8, api_token=None):
    if api_token is not None and (not isinstance(api_token, str) or not api_token or not api_token.isascii()):
        raise ValueError("Local API token must be nonempty ASCII")
    host = TaskHost(service_factory, store=store, max_tasks=max_tasks, mcp_disabled=mcp_disabled,
                    legacy_mcp_config=legacy_mcp_config, rag_coordinator=rag_coordinator)
    conversations = ConversationHost(host,chat_factory,summary_factory=summary_factory,
        stream_retention_seconds=conversation_stream_retention_seconds,
        stream_max_subscribers=conversation_stream_max_subscribers,
        runtime=host.runtime, capacity=host.capacity, mcp_service=host.mcp_service,
        mcp_disabled=mcp_disabled)
    missions = MissionHost(workflow_factory=mission_factory, runtime=host.runtime,
        capacity=host.capacity, store=host.store, mcp_service=host.mcp_service,
        mcp_disabled=mcp_disabled)
    mission_streams = MissionEventStream(host.store, heartbeat_seconds=heartbeat_seconds,
                                        max_subscribers=mission_stream_max_subscribers)

    @asynccontextmanager
    async def lifespan(app):
        await host.start()
        try:
            yield
        finally:
            try:
                try:
                    await host.runtime.begin_shutdown()
                finally:
                    await missions.close()
            finally:
                try:
                    await conversations.close()
                finally:
                    await host.close(close_resources=model_client_closer)

    app = FastAPI(title="ForgePilot local Agent API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.task_host = host
    app.state.conversation_host = conversations
    app.state.mission_host = missions
    app.state.mission_events = mission_streams
    app.add_middleware(APIRequestGuard)
    app.add_middleware(MCPRequestGuard, error=error_response)
    app.add_middleware(MissionRequestGuard, error=error_response)

    @app.middleware("http")
    async def local_origin(request, call_next):
        if (len(request.scope.get("raw_path", b"")) +
                len(request.scope.get("query_string", b"")) > MAX_API_TARGET):
            return error_response("request_too_large", "Request target exceeds 8 KiB", 414)
        authority = request.headers.get("host", "")
        try:
            parsed = urlsplit("http://" + authority)
            valid = (len(request.headers.getlist("host")) == 1 and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                     and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            return error_response("host_not_allowed", "Use the local loopback host", 400)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{authority}":
            return error_response("origin_not_allowed", "Cross-origin access is not enabled", 403)
        if api_token is not None and request.url.path.startswith("/api/"):
            supplied = request.headers.getlist("x-forgepilot-token")
            if (len(supplied) != 1 or not supplied[0].isascii()
                    or not hmac.compare_digest(supplied[0], api_token)):
                return error_response("unauthorized", "Local API token is required", 401)
        return await call_next(request)

    # add_middleware prepends. Keep this outermost among user middleware so
    # Starlette's ServerErrorMiddleware never receives the original exception.
    app.add_middleware(SafeExceptionGuard)

    @app.exception_handler(ForgePilotError)
    async def service_error(request, exc):
        status = ERROR_STATUS.get(exc.code, 422)
        message = host.store.redactor.text(exc.message)
        if request.url.path.startswith("/api/v2/"):
            status = V2_ERROR_STATUS.get(exc.code, status)
        return error_response(exc.code, message, status,
                              task_id=getattr(exc, "task_id", None))

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return error_response("storage_unavailable", "Task database is unavailable", 503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo invalid input, request bodies, credentials, or raw validation contexts.
        if request.url.path.startswith("/api/v2/"):
            return error_response("invalid_request", "Request does not match the v2 schema", 422)
        if request.url.path.startswith("/api/v1/mcp/"):
            # Even field names/loc entries may be an attacker-supplied secret.
            return error_response("invalid_request", "Request does not match the MCP management schema", 422)
        return error_response("invalid_request", "Request does not match the v1 schema", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response("route_not_found" if exc.status_code == 404 else "http_error",
                              "HTTP request could not be handled", exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error_response("api_internal", "Request failed unexpectedly; inspect task status", 500)

    errors = {code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 422, 500, 503)}
    register_mcp_routes(app, host, {**errors, **{code: {"model": ErrorResponse} for code in (413, 415, 429, 499, 504)}})
    register_mission_routes(app, missions, mission_streams)
    register_runtime_routes(app, host)

    @app.post("/api/v1/tasks", response_model=TaskResponse, status_code=202, responses=errors)
    async def create_task(body: CreateTask):
        return await host.create(body.repository_path, body.prompt, use_rag=body.use_rag)

    @app.get("/api/v1/tasks", response_model=TaskList, responses=errors)
    def list_tasks(limit: int = Query(default=20,ge=1,le=100), before: str | None = Query(default=None,max_length=512),
                   conversation_id: str | None = Query(default=None,max_length=36)):
        return host.store.list_page(limit,before,conversation_id)

    @app.post("/api/v1/rag/index", response_model=RagIndexStatusResponse,
              status_code=202, responses=errors)
    async def start_rag_index(body: RagIndexRequest):
        return await host.run_rag("start", body.repository_path)

    @app.get("/api/v1/rag/status", response_model=RagIndexStatusResponse, responses=errors)
    async def rag_index_status(repository_path: str = Query(min_length=1, max_length=4096)):
        return await host.run_rag("status", _checked_rag_query_path(repository_path))

    @app.post("/api/v1/rag/index/cancel", response_model=RagIndexStatusResponse,
              responses=errors)
    async def cancel_rag_index(body: RagIndexRequest):
        return await host.run_rag("cancel", body.repository_path)

    @app.delete("/api/v1/rag/index", response_model=RagIndexDeleted, responses=errors)
    async def delete_rag_index(repository_path: str = Query(min_length=1, max_length=4096)):
        return await host.run_rag("clear", _checked_rag_query_path(repository_path))

    @app.get("/api/v1/rag/index/events", responses={**errors,
        200: {"description": "Repository-scoped code index progress SSE snapshots",
              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        204: {"description": "No active or newer index progress"}})
    async def rag_index_events(request: Request,
                               repository_path: str = Query(min_length=1, max_length=4096),
                               last_event_id: str | None = Header(default=None)):
        initial = await host.run_rag("status", _checked_rag_query_path(repository_path))
        operation_id = initial.get("operation_id")
        if operation_id is None:
            return Response(status_code=204)
        cursor = 0
        if last_event_id:
            prefix, separator, revision = last_event_id.partition(":")
            if (separator != ":" or prefix != operation_id or not revision.isascii()
                    or not revision.isdecimal()):
                raise ForgePilotError("invalid_event_id", "Last-Event-ID format is invalid")
            cursor = int(revision)
            if cursor > initial["revision"]:
                raise ForgePilotError("event_cursor_ahead", "Event cursor is ahead of index progress")
        if not initial["running"] and cursor >= initial["revision"]:
            return Response(status_code=204)

        async def stream():
            snapshot = initial
            last_heartbeat = monotonic()
            yield "retry: 1000\n\n"
            while True:
                revision = snapshot["revision"]
                if revision > cursor_state[0]:
                    event_id = f"{snapshot['operation_id']}:{revision}"
                    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event_id}\nevent: rag_index_progress\ndata: {payload}\n\n"
                    cursor_state[0] = revision
                if not snapshot["running"] or await request.is_disconnected():
                    return
                await asyncio.sleep(min(0.25, heartbeat_seconds))
                snapshot = await host.rag_snapshot(snapshot["repository_id"])
                if monotonic() - last_heartbeat >= heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    last_heartbeat = monotonic()

        cursor_state = [cursor]
        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @app.post('/api/v1/conversations',status_code=201,response_model=ConversationDetail,responses=errors)
    def new_conversation(body: CreateConversation):
        return host.store.create_conversation(body.title,body.repository_path)

    @app.get('/api/v1/conversations',response_model=ConversationList,responses=errors)
    def conversation_list(limit: int = Query(default=20,ge=1,le=100), before: str | None = Query(default=None,max_length=512)):
        return host.store.conversation_page(limit,before)

    @app.get('/api/v1/conversations/{conversation_id}',response_model=ConversationDetail,responses=errors)
    def conversation_detail(conversation_id: str, before: int | None = Query(default=None,ge=1), limit: int = Query(default=100,ge=1,le=100)):
        return host.store.conversation_detail(conversation_id,before,limit)

    @app.patch('/api/v1/conversations/{conversation_id}',response_model=ConversationDetail,responses=errors)
    def edit_conversation(conversation_id: str, body: UpdateConversation):
        return host.store.update_conversation(conversation_id,body.model_dump(exclude_unset=True))

    @app.delete('/api/v1/conversations/{conversation_id}',response_model=ConversationDeleted,responses=errors)
    async def delete_conversation(conversation_id: str, stop_active: bool = False):
        return await conversations.delete(conversation_id,stop_active)

    @app.post('/api/v1/conversations/{conversation_id}/requests',status_code=202,response_model=ConversationRequestState,responses=errors)
    async def conversation_request(conversation_id: str, body: ConversationRequest):
        return await conversations.submit(conversation_id,body.request_id,body.mode,body.prompt,body.tool_mode,body.use_rag)

    @app.get('/api/v1/memories',response_model=MemoryList,responses=errors)
    def memory_list(repository_path: str | None = Query(default=None,max_length=4096)):
        return host.store.memory_list(repository_path)

    @app.post('/api/v1/memories',status_code=201,response_model=MemoryRecord,responses=errors)
    async def add_memory(body:MemoryCreate):
        return await conversations.save_memory(body.model_dump())

    @app.patch('/api/v1/memories/{memory_id}',response_model=MemoryRecord,responses=errors)
    async def edit_memory(memory_id:str,body:MemoryUpdate):
        return await conversations.save_memory(body.model_dump(),memory_id)

    @app.delete('/api/v1/memories/{memory_id}',response_model=MemoryDeleted,responses=errors)
    def delete_memory(memory_id:str):
        return host.store.delete_memory(memory_id)

    @app.get('/api/v1/conversations/{conversation_id}/context',response_model=SummaryState,responses=errors)
    def get_summary(conversation_id:str):
        return host.store.summary_state(conversation_id)

    @app.patch('/api/v1/conversations/{conversation_id}/context',response_model=SummaryState,responses=errors)
    async def edit_summary(conversation_id:str,body:SummaryUpdate):
        return await conversations.edit_summary(conversation_id,**body.model_dump())

    @app.delete('/api/v1/conversations/{conversation_id}/context',response_model=SummaryState,responses=errors)
    async def delete_summary(conversation_id:str,revision:int=Query(ge=0)):
        return await conversations.edit_summary(conversation_id,revision,delete=True)

    @app.get('/api/v1/conversations/{conversation_id}/requests/{request_id}',response_model=ConversationRequestState,responses=errors)
    def request_detail(conversation_id: str,request_id: str):
        return host.store.conversation_request(conversation_id,request_id)

    @app.get('/api/v1/conversations/{conversation_id}/requests/{request_id}/mcp-tools',response_model=ConversationMCPTools,responses=errors)
    def request_mcp_tools(conversation_id: str,request_id: str):
        return host.store.conversation_mcp_tools(conversation_id,request_id)

    @app.post('/api/v1/conversations/{conversation_id}/requests/{request_id}/stop',response_model=ConversationRequestState,responses=errors)
    async def stop_answer(conversation_id: str, request_id: str):
        return await conversations.stop(conversation_id,request_id)

    @app.get('/api/v1/conversations/{conversation_id}/requests/{request_id}/events',
        summary='Subscribe to one conversation answer stream',responses={**errors,
        200:{"description":"Bounded, resumable conversation answer SSE stream",
             "content":{"text/event-stream":{"schema":{"type":"string"}}}},
        204:{"description":"Terminal stream already fully acknowledged"}})
    async def conversation_events(conversation_id: str, request_id: str, request: Request,
                                  last_event_id: str | None = Header(default=None)):
        accept=request.headers.get('accept','*/*')
        if 'text/event-stream' not in accept and '*/*' not in accept:
            raise ForgePilotError('invalid_request','此接口只提供 text/event-stream。')
        subscription=conversations.conversation_stream(conversation_id,request_id,last_event_id)
        initial=subscription.read()
        if initial.terminal and not initial.events:
            subscription.close()
            return Response(status_code=204)

        async def stream():
            idle_at=monotonic()
            pending=initial
            try:
                yield 'retry: 1000\n\n'
                while True:
                    batch=pending
                    pending=None
                    if batch is None:
                        batch=subscription.read()
                    for event in batch.events:
                        yield event.frame()
                    if batch.events:
                        idle_at=monotonic()
                        continue
                    # StreamingResponse cancels this generator when the socket
                    # closes.  Polling Request.is_disconnected() here is not
                    # reliable for every ASGI transport and could suppress the
                    # first heartbeat before an idle reconnect.
                    if batch.terminal:
                        return
                    remaining=conversation_stream_idle_seconds-(monotonic()-idle_at)
                    if remaining<=0:
                        return
                    changed=await subscription.wait(min(heartbeat_seconds,remaining))
                    if not changed:
                        yield ': heartbeat\n\n'
            finally:
                subscription.close()

        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'Cache-Control':'no-cache','X-Accel-Buffering':'no',
        })

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskResponse, responses=errors)
    def get_task(task_id: str):
        return host.store.get(task_id)

    @app.get("/api/v1/tasks/{task_id}/trace", response_model=TracePage, responses=errors)
    def trace_page(task_id: str, after: int = Query(default=0,ge=0), limit: int = Query(default=50,ge=1,le=100)):
        return host.store.trace_page(task_id,after,limit)

    @app.get("/api/v1/tasks/{task_id}/trace/{sequence}", response_model=TraceDetail, responses=errors)
    def trace_detail(task_id: str, sequence: int):
        return host.store.trace_detail(task_id,sequence)

    @app.post("/api/v1/tasks/{task_id}/approve", response_model=TaskResponse, status_code=202, responses=errors)
    async def approve(task_id: str, body: ApproveTask):
        return await host.approve(task_id, body.plan_version, approved_test_files=body.approved_test_files)

    @app.post("/api/v1/tasks/{task_id}/feedback", response_model=TaskResponse, status_code=202, responses=errors)
    async def feedback(task_id: str, body: PlanFeedback):
        return await host.feedback(task_id, body.plan_version, body.feedback)

    @app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskResponse, responses=errors)
    async def cancel(task_id: str, body: CancelTask = Body(default=CancelTask())):
        return await host.cancel(task_id, reason=body.reason, plan_version=body.plan_version)

    @app.get("/api/v1/tasks/{task_id}/events", responses={**errors,
        200: {"description": "Ordered SSE stream; JSON EventPayload in data, task-scoped sequence in id",
              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        204: {"description": "Terminal stream already fully acknowledged"}})
    async def events(task_id: str, request: Request, last_event_id: str | None = Header(default=None)):
        cursor = await asyncio.to_thread(host.store.cursor,task_id,last_event_id)
        available, closed = await asyncio.to_thread(host.store.read,task_id,cursor)
        if closed and not available:
            return Response(status_code=204)

        async def stream():
            nonlocal cursor
            heartbeat_at = monotonic()
            yield "retry: 1000\n\n"
            while True:
                try:
                    batch, closed = await asyncio.to_thread(host.store.read,task_id,cursor)
                except ForgePilotError as exc:
                    if exc.code in {'unknown_task', 'storage_unavailable'}: return
                    raise
                for event in batch:
                    yield event.frame()
                    cursor = event.sequence
                if batch:
                    continue  # Drain all chunks, including when the terminal stream has >64 events.
                if closed or await request.is_disconnected():
                    return
                if monotonic() - heartbeat_at >= heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    heartbeat_at = monotonic()
                try:
                    await host.store.wait(task_id, cursor, heartbeat_seconds)
                except ForgePilotError as exc:
                    if exc.code in {'unknown_task', 'storage_unavailable'}: return
                    raise

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    return app
