"""Bounded HTTP adapter for mission application services and durable SSE."""
from fastapi import Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import Headers

from .api_v2_models import (
    CreateMission, MissionApproval, MissionArtifacts, MissionControl, MissionDelivery,
    MissionDeliveryResponse, MissionDiff, MissionErrorResponse, MissionGraph,
    MissionID, MissionPage, MissionRecoveryResponse, MissionResponse, MissionResume, MissionRetry,
    MissionValidation,
)
from .errors import ForgePilotError
from .mcp import decode
from .orchestration.models import NodeStatus


MAX_MISSION_BODY = 65_536
V2_ERROR_STATUS = {
    "unknown_mission": 404, "unknown_graph": 404, "artifact_not_found": 404,
    "unknown_artifact": 404, "mission_conflict": 409, "graph_conflict": 409,
    "revision_conflict": 409, "approval_conflict": 409, "approval_stale": 409,
    "mission_revision_conflict": 409, "mission_approval_conflict": 409,
    "approval_required": 403, "permission_denied": 403, "policy_denied": 403,
    "tool_policy": 403, "tool_denied": 403, "command_denied": 403,
    "test_approval_required": 403, "delivery_approval": 403,
    "recovery_conflict": 409, "recovery_required": 409, "recovery_stale": 409,
    "graph_definition": 409, "workspace_conflict": 409, "workspace_busy": 409,
    "workspace_corrupted": 409, "delivery_changed": 409, "delivery_recovery": 409,
    "mission_stream_capacity": 429, "graph_storage": 503,
    "artifact_storage": 503, "mission_unavailable": 503,
}


class MissionRequestGuard:
    """Enforce raw bytes, media type and unambiguous JSON before model parsing."""

    def __init__(self, app, error):
        self.app, self.error = app, error

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v2/"):
            return await self.app(scope, receive, send)
        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return await self.error("invalid_request", "Mission requests require application/json", 415)(scope, receive, send)
        if headers.get("content-encoding", "identity").lower() != "identity":
            return await self.error("invalid_request", "Encoded request bodies are unsupported", 415)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_MISSION_BODY:
                return await self.error("request_too_large", "Mission request exceeds 64 KiB", 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            if not isinstance(decode(body), dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            return await self.error("invalid_request", "Invalid mission JSON", 422)(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def register_mission_routes(app, host, streams):
    errors = {status: {"model": MissionErrorResponse} for status in (400, 403, 404, 409, 413, 415, 422, 429, 500, 503)}
    prefix = "/api/v2/missions"

    @app.post(prefix, response_model=MissionResponse, status_code=202, responses=errors)
    async def create_mission(body: CreateMission):
        return await host.create(**body.model_dump())

    @app.get(prefix, response_model=MissionPage, responses=errors)
    def list_missions(limit: int = Query(default=20, ge=1, le=100),
                      before: str | None = Query(default=None, min_length=1, max_length=512),
                      repository_path: str | None = Query(default=None, min_length=1, max_length=4096),
                      status: NodeStatus | None = Query(default=None)):
        if repository_path is not None:
            try:
                CreateMission.local_absolute_path(repository_path)
            except ValueError:
                raise ForgePilotError("invalid_request", "Repository must be a local absolute path") from None
        return host.list_page(limit, before, repository_path=repository_path, status=status)

    @app.get(prefix + "/{task_id}", response_model=MissionResponse, responses=errors)
    def get_mission(task_id: MissionID):
        return host.get(task_id)

    @app.get(prefix + "/{task_id}/graph", response_model=MissionGraph, responses=errors)
    def mission_graph(task_id: MissionID):
        return host.graph(task_id)

    @app.get(prefix + "/{task_id}/observability", response_model=dict, responses=errors)
    def mission_observability(task_id: MissionID):
        return host.observability(task_id)

    @app.get(prefix + "/{task_id}/artifacts", response_model=MissionArtifacts, responses=errors)
    def mission_artifacts(task_id: MissionID):
        return host.artifacts(task_id)

    @app.get(prefix + "/{task_id}/artifacts/{artifact_id}", response_model=dict, responses=errors)
    def mission_artifact(task_id: MissionID, artifact_id: MissionID):
        return host.artifact(task_id, artifact_id)

    @app.get(prefix + "/{task_id}/diff", response_model=MissionDiff, responses=errors)
    def mission_diff(task_id: MissionID):
        return host.diff(task_id)

    @app.get(prefix + "/{task_id}/validation", response_model=MissionValidation, responses=errors)
    def mission_validation(task_id: MissionID):
        return host.validation(task_id)

    @app.post(prefix + "/{task_id}/approvals", response_model=MissionResponse, status_code=202, responses=errors)
    async def mission_approval(task_id: MissionID, body: MissionApproval):
        return await host.approve(task_id, **body.model_dump())

    @app.post(prefix + "/{task_id}/cancel", response_model=MissionResponse, responses=errors)
    async def cancel_mission(task_id: MissionID, body: MissionControl):
        return await host.cancel(task_id, **body.model_dump())

    @app.post(prefix + "/{task_id}/retry", response_model=MissionResponse, status_code=202, responses=errors)
    async def retry_mission(task_id: MissionID, body: MissionRetry):
        return await host.retry(task_id, **body.model_dump())

    @app.post(prefix + "/{task_id}/resume", response_model=MissionRecoveryResponse, status_code=202, responses=errors)
    async def resume_mission(task_id: MissionID, body: MissionResume):
        return await host.resume(task_id, **body.model_dump())

    @app.post(prefix + "/{task_id}/delivery", response_model=MissionDeliveryResponse, responses=errors)
    async def deliver_mission(task_id: MissionID, body: MissionDelivery):
        return await host.delivery(task_id, **body.model_dump())

    @app.get(prefix + "/{task_id}/events", response_class=StreamingResponse, responses={**errors,
        200: {"description": "Durable mission events. Resume exclusively after Last-Event-ID.",
              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        204: {"description": "Terminal stream fully acknowledged"}})
    async def mission_events(task_id: MissionID, request: Request,
                             last_event_id: str | None = Header(default=None, max_length=128)):
        if len(request.headers.getlist("last-event-id")) > 1:
            raise ForgePilotError("invalid_event_id", "Supply a single Last-Event-ID")
        accept = request.headers.get("accept", "*/*")
        if "text/event-stream" not in accept and "*/*" not in accept:
            raise ForgePilotError("invalid_request", "Mission events require text/event-stream")
        host.get(task_id)
        subscription = streams.subscribe(task_id, last_event_id)
        try:
            initial = subscription.read()
        except BaseException:
            subscription.close()
            raise
        if initial[1] and not initial[0]:
            subscription.close()
            return Response(status_code=204)
        return StreamingResponse(subscription.frames(request, initial=initial), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
