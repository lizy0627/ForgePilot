"""Thin MCP management HTTP boundary; configuration policy lives in MCPService."""
import asyncio

from fastapi import Query, Request, Response
from starlette.datastructures import Headers

from .api_models import (MCPProbeInput, MCPSaveInput, MCPEnableInput, MCPTestInput,
    MCPServerID, MCPServerList, MCPServerDetail, MCPProbeResponse, MCPPresetList, MCPDeleted)
from .errors import ForgePilotError
from .mcp import decode

MAX_MANAGEMENT_BODY = 524_288


class MCPRequestGuard:
    """Bound raw JSON before FastAPI parses it; never log credential-bearing bodies."""
    def __init__(self, app, error):
        self.app, self.error = app, error

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not scope["path"].startswith("/api/v1/mcp/")
                or scope["method"] not in ("POST", "PUT", "PATCH")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return await self.error("invalid_request", "MCP management requires application/json", 415)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_MANAGEMENT_BODY:
                return await self.error("mcp_limit", "MCP request exceeds 512 KiB", 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            decode(body)  # Also reject duplicate object keys and NaN/Infinity.
        except (ValueError, UnicodeError, RecursionError):
            return await self.error("invalid_request", "Invalid MCP management JSON", 422)(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _request_operation(request, host, operation, *args, **kwargs):
    async def disconnected():
        # Body parsing has finished. Await the actual ASGI disconnect message;
        # a polling is_disconnected cancel scope can miss it through middleware.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    running = asyncio.create_task(host.run_mcp(operation, *args, **kwargs))
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((running, watcher), return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            return await running
        raise ForgePilotError("mcp_cancelled", "MCP management request disconnected")
    finally:
        watcher.cancel()
        if not running.done():
            running.cancel()
        cleanup = asyncio.gather(running, watcher, return_exceptions=True)
        # Joining the bridge waits for the worker's HTTP/stdio owner cleanup.
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()


def register_mcp_routes(app, host, errors):
    service = host.mcp_service

    @app.get("/api/v1/mcp/presets", response_model=MCPPresetList, responses=errors)
    def presets():
        return {"items": service.presets()}

    @app.get("/api/v1/mcp/servers", response_model=MCPServerList, responses=errors)
    def servers():
        return {**service.list_servers(), "disabled": host.mcp_disabled}

    @app.get("/api/v1/mcp/servers/{server_id}", response_model=MCPServerDetail, responses=errors)
    def detail(server_id: MCPServerID):
        return service.get(server_id)

    @app.post("/api/v1/mcp/probe", response_model=MCPProbeResponse, responses=errors)
    async def probe(request: Request, body: MCPProbeInput):
        return await _request_operation(request, host, "probe", body.configuration(), server_id=body.server_id,
            base_revision=body.base_revision, secret_updates=body.secret_updates)

    @app.put("/api/v1/mcp/servers/{server_id}", response_model=MCPServerDetail,
             responses={**errors, 201: {"model": MCPServerDetail, "description": "Created at the supplied UUID"}})
    async def save(server_id: MCPServerID, request: Request, response: Response, body: MCPSaveInput):
        result = await _request_operation(request, host, "save", server_id, body.configuration(),
            base_revision=body.base_revision, catalog_hash=body.catalog_hash, secret_updates=body.secret_updates)
        response.status_code = 201 if body.base_revision == 0 else 200
        return result

    @app.patch("/api/v1/mcp/servers/{server_id}", response_model=MCPServerDetail, responses=errors)
    async def enable(server_id: MCPServerID, request: Request, body: MCPEnableInput):
        return await _request_operation(request, host, "set_enabled", server_id, revision=body.revision, enabled=body.enabled)

    @app.post("/api/v1/mcp/servers/{server_id}/test", response_model=MCPServerDetail, responses=errors)
    async def test(server_id: MCPServerID, request: Request, body: MCPTestInput):
        return await _request_operation(request, host, "test", server_id, revision=body.revision)

    @app.delete("/api/v1/mcp/servers/{server_id}", response_model=MCPDeleted, responses=errors)
    async def delete(server_id: MCPServerID, request: Request, revision: int = Query(ge=1, le=2**63 - 2)):
        return await _request_operation(request, host, "delete", server_id, revision=revision)
