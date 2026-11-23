"""Bounded HTTP adapter for Missions and the local skill library."""
import logging
from typing import Annotated

from fastapi import Header, Path, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import Headers

from .api_v2_models import (
    CreateMission, MissionApproval, MissionArtifacts, MissionControl, MissionDelivery,
    MissionDeliveryResponse, MissionDiff, MissionErrorResponse, MissionGraph,
    MissionID, MissionHash, MissionPage, MissionRecoveryResponse, MissionResponse, MissionResume, MissionRetry,
    MissionValidation, PreviewMission, MissionSourcePreview, MissionScopeExpansion,
    SkillRecommendationRequest, SkillRecommendations, MissionSkills,
    CreateSkillDraft, SaveSkillDraft, PublishLocalSkill, CreateSkillRevisionDraft,
    DisableSkill, SkillDraftPage, SkillPage,
)
from .errors import ForgePilotError
from .mcp import decode
from .orchestration.models import NodeStatus
from .skill_api_contract import SKILL_ERRORS, skill_responses
from .skill_mission_drafts import create_mission_draft
from .skill_models import SkillDraft, SkillIdentity, SkillVersion
from .skill_recommendations import recommend_skills


MAX_MISSION_BODY = 65_536
V2_ERROR_STATUS = {
    "unknown_mission": 404, "unknown_graph": 404, "artifact_not_found": 404,
    "unknown_artifact": 404, "scope_expansion_not_found": 404,
    "mission_conflict": 409, "graph_conflict": 409,
    "revision_conflict": 409, "approval_conflict": 409, "approval_stale": 409,
    "mission_revision_conflict": 409, "mission_approval_conflict": 409,
    "mission_cancel_required": 409,
    "approval_required": 403, "permission_denied": 403, "policy_denied": 403,
    "tool_policy": 403, "tool_denied": 403, "command_denied": 403,
    "test_approval_required": 403, "delivery_approval": 403,
    "recovery_conflict": 409, "recovery_required": 409, "recovery_stale": 409,
    "graph_definition": 409, "workspace_conflict": 409, "workspace_busy": 409,
    "workspace_corrupted": 409, "workspace_changed": 409, "checkpoint_integrity": 409,
    "delivery_changed": 409, "delivery_recovery": 409,
    "mission_stream_capacity": 429, "graph_storage": 503,
    "artifact_storage": 503, "mission_unavailable": 503,
    "baseline_changed": 409, "baseline_approval": 403, "baseline_capacity": 429,
    "build_selection_changed": 409, "mission_frozen_config_missing": 409,
    "skill_selection_changed": 409, "skill_snapshot_invalid": 409,
    "skill_route_denied": 403, "invalid_skill_selection": 422,
    "skill_stage_unavailable": 422, "skill_budget_exceeded": 422,
    **{code: status for code, (status, _) in SKILL_ERRORS.items()},
}

SkillID = Annotated[str, Path(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$")]
PageSize = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=2**63 - 2)]


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


def register_skill_routes(app, store):
    skills = store.skills
    errors = {status: {"model": MissionErrorResponse} for status in (400, 401, 403, 413, 415, 422, 500, 503)}

    @app.post("/api/v2/skills/recommendations", response_model=SkillRecommendations, responses=errors)
    def skill_recommendations(body: SkillRecommendationRequest):
        """Return up to three enabled, reviewed skills for the task description.

        Exclusion phrases veto a match. A symptom phrase or a complete keyword group
        qualifies a candidate. Rank by counts of matched symptoms, complete groups and
        project tags, in that order. Break ties by ascending skill ID.
        """
        matches = recommend_skills(skills, **body.model_dump())
        logging.getLogger(__name__).info("Skill recommendation count=%d", len(matches),
                                         extra={"recommendation_count": len(matches)})
        return {"recommendations": matches}

    @app.post("/api/v2/missions/{task_id}/skill-drafts", response_model=SkillDraft, status_code=201,
              responses=skill_responses("unknown_mission", "mission_not_terminal", "skill_request_conflict",
                                        "invalid_skill_evidence", write=True))
    def create_mission_skill_draft(task_id: MissionID, body: CreateSkillDraft):
        """Freeze the terminal Mission revision and private evidence in a manual draft.

        Succeeded, historical completed, failed and cancelled Missions can supply up to eight
        evidence items. A skipped Mission accepts a note only. Interrupted and retryable
        Missions return mission_not_terminal. No model configuration is required.

        Supply the same request_id on retries to recover the original draft and its latest edits.
        Omitting the ID creates a new draft on each call.
        """
        return create_mission_draft(store, task_id, **body.model_dump())

    @app.get("/api/v2/skill-drafts", response_model=SkillDraftPage, responses=skill_responses("invalid_skill_evidence"))
    def list_skill_drafts(limit: PageSize = 20, offset: PageOffset = 0, source_mission_id: MissionID | None = None):
        """List local drafts and their review receipts, most recently edited first."""
        return skills.list_draft_page(source_mission_id=source_mission_id, limit=limit, offset=offset)

    @app.get("/api/v2/skill-drafts/{draft_id}", response_model=SkillDraft,
             responses=skill_responses("skill_draft_not_found", "invalid_skill_evidence"))
    def get_skill_draft(draft_id: MissionID):
        """Reopen the saved draft. An artifact locator is a JSON Pointer relative to the
        source Mission artifact's content. A verified result describes the recorded validation
        outcome at that locator. All private references stay with the draft after review.
        """
        return skills.get_draft(draft_id)

    @app.patch("/api/v2/skill-drafts/{draft_id}", response_model=SkillDraft,
               responses=skill_responses("skill_draft_not_found", "skill_revision_conflict",
                                         "invalid_skill_evidence", write=True))
    def save_skill_draft(draft_id: MissionID, body: SaveSkillDraft):
        """Save editor fields at expected_revision. Saving leaves recommendations unchanged.

        Omitted top-level fields keep their values. Sending content replaces the complete
        guidance document. Repeating the last successful save returns the saved revision.
        On skill_revision_conflict retain local edits and reload the draft before merging.
        """
        return skills.patch_draft(draft_id, expected_revision=body.expected_revision,
                                  changes=body.model_dump(exclude_unset=True, exclude={"expected_revision"}))

    @app.post("/api/v2/skill-drafts/{draft_id}/publish-local", response_model=SkillVersion,
              responses=skill_responses("skill_draft_not_found", "skill_request_conflict", "skill_revision_conflict",
                                        "skill_draft_incomplete", "invalid_skill_evidence", write=True))
    def publish_local_skill(draft_id: MissionID, body: PublishLocalSkill):
        """Approve the saved revision. Replay the same request_id and revision for the committed version."""
        return skills.publish_local(draft_id, **body.model_dump())

    @app.get("/api/v2/skills", response_model=SkillPage, responses=skill_responses())
    def list_skills(limit: PageSize = 20, offset: PageOffset = 0):
        """List local library identities, including disabled skills and their current revisions."""
        return skills.list_skill_page(limit=limit, offset=offset)

    @app.get("/api/v2/skills/{skill_id}/versions/{version}", response_model=SkillVersion,
             responses=skill_responses("skill_not_found"))
    def get_skill_version(skill_id: SkillID, version: Annotated[int, Path(ge=1, le=2**63 - 2)]):
        """Read the immutable reviewed content of an exact version."""
        return skills.get_version(skill_id, version)

    @app.patch("/api/v2/skills/{skill_id}", response_model=SkillIdentity,
               responses=skill_responses("skill_not_found", "skill_revision_conflict", write=True))
    def disable_skill(skill_id: SkillID, body: DisableSkill):
        """Disable the skill at its current revision. Existing version details remain available."""
        return skills.disable(skill_id, expected_revision=body.expected_revision)

    @app.post("/api/v2/skills/{skill_id}/drafts", response_model=SkillDraft, status_code=201,
              responses=skill_responses("skill_not_found", "skill_request_conflict", write=True))
    def create_skill_revision_draft(skill_id: SkillID, body: CreateSkillRevisionDraft):
        """Open a revision draft from the exact base version. Publish checks that the base is still current."""
        return skills.draft_from_version(skill_id, **body.model_dump())


def register_mission_routes(app, host, streams):
    errors = {status: {"model": MissionErrorResponse} for status in (400, 403, 404, 409, 413, 415, 422, 429, 500, 503)}
    prefix = "/api/v2/missions"

    @app.post(prefix, response_model=MissionResponse, status_code=202, responses=errors)
    async def create_mission(body: CreateMission):
        """Freeze the selected reviewed versions at admission. Refresh choices after a 409 skill_selection_changed."""
        return await host.create(**body.model_dump())

    @app.post(prefix + "/preview", response_model=MissionSourcePreview, responses=errors)
    async def preview_mission(body: PreviewMission):
        return await host.preview(**body.model_dump())

    @app.delete(prefix + "/previews/{token}", responses=errors)
    async def discard_mission_preview(token: MissionHash):
        await host.discard_preview(token)
        return {"discarded": True}

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

    @app.get(prefix + "/{task_id}/skills", response_model=MissionSkills, responses=errors)
    def mission_skills(task_id: MissionID):
        """Show admitted selections and actual model input by analysis node and attempt."""
        return host.mission_skills(task_id)

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

    @app.get(prefix + "/{task_id}/scope-expansion", response_model=MissionScopeExpansion, responses=errors)
    def mission_scope_expansion(task_id: MissionID):
        return host.scope_expansion(task_id)

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
