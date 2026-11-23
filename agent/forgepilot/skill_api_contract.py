"""Failure contracts for the local skill library's HTTP operations."""
from .api_v2_models import MissionErrorResponse


SKILL_ERRORS = {
    "unknown_mission": (404, "The source Mission does not exist."),
    "skill_draft_not_found": (404, "The draft does not exist."),
    "skill_not_found": (404, "The skill or requested version does not exist."),
    "mission_not_terminal": (409, "The Mission is still active or recoverable."),
    "skill_request_conflict": (409, "The request ID already identifies a different operation or input."),
    "skill_revision_conflict": (409, "The revision changed, the draft was closed, or the base version was superseded or disabled. Keep local edits and reload before merging."),
    "invalid_skill_evidence": (422, "The private reference failed source Mission, cutoff, artifact hash or item validation."),
    "skill_draft_incomplete": (422, "Review requires a name, guidance, stages, boundaries and a matching rule."),
}


def skill_responses(*codes: str, write: bool = False) -> dict:
    """Describe common transport failures and just the domain failures for this operation."""
    descriptions = {
        400: "host_not_allowed: use a local loopback address.",
        401: "unauthorized: supply X-ForgePilot-Token when token authentication is configured.",
        403: "origin_not_allowed: cross-origin access is disabled.",
        414: "request_too_large: the request target exceeds 8 KiB.",
        422: "invalid_request: parameters or fields failed validation.",
        500: "api_internal: unexpected service failure.",
        503: "storage_unavailable or storage_busy: the local database is unavailable.",
    }
    if write:
        descriptions.update({
            413: "request_too_large: the request body exceeds 64 KiB.",
            415: "invalid_request: use application/json with identity encoding.",
            422: "invalid_request: send a JSON object matching the schema, with unique keys and finite numbers.",
        })
    for code in codes:
        status, meaning = SKILL_ERRORS[code]
        descriptions[status] = (descriptions.get(status, "") + f" {code}: {meaning}").strip()
    return {status: {"model": MissionErrorResponse, "description": description}
            for status, description in descriptions.items()}
