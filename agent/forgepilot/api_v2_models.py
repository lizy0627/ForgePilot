"""Mission v2 transport contracts, deliberately separate from the v1 models."""
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MissionID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
MissionHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MissionRevision = Annotated[int, Field(ge=1, le=2**63 - 2)]
MissionNodeID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")]


class MissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def unicode_scalars(cls, value):
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        return value


class CreateMission(MissionRequest):
    repository_path: str = Field(min_length=1, max_length=4096)
    request: str = Field(min_length=1, max_length=8192)
    task_type: Literal["auto", "bug_fix", "feature"] = "auto"
    orchestration_mode: Literal["multi_agent"] = "multi_agent"
    base_ref: str = Field(default="HEAD", min_length=1, max_length=256)
    toolchain_profile: str = Field(default="auto", pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    language: str = Field(default="zh-CN", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$", max_length=12)
    mcp_server_ids: list[MissionID] = Field(default_factory=list, max_length=4)

    @field_validator("mcp_server_ids")
    @classmethod
    def unique_mcp_servers(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("MCP server selection contains duplicates")
        return value

    @field_validator("repository_path")
    @classmethod
    def local_absolute_path(cls, value):
        windows = PureWindowsPath(value)
        if (not (windows.is_absolute() or PurePosixPath(value).is_absolute())
                or value.startswith(("\\\\", "//"))
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or ":" in value[len(windows.drive):]
                or any(part in {".", ".."} for part in value.replace("\\", "/").split("/"))):
            raise ValueError("Repository must be a local absolute path")
        return value

    @field_validator("request")
    @classmethod
    def nonblank_request(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Request must be nonblank and contain no NUL")
        return value

    @field_validator("base_ref")
    @classmethod
    def bounded_git_ref(cls, value):
        if (not value.strip() or value.startswith("-")
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                or any(char in value for char in ("\x00", "\\", ":"))):
            raise ValueError("Invalid base ref")
        return value


class MissionControl(MissionRequest):
    expected_revision: MissionRevision


class MissionApproval(MissionControl):
    approval_type: Literal["execution"] = "execution"
    task_spec_hash: MissionHash
    plan_hash: MissionHash
    scope_hash: MissionHash
    decision: Literal["approved", "rejected"]


class MissionRetry(MissionControl):
    node_id: MissionNodeID | None = None


class MissionResume(MissionControl):
    recovery_token: MissionHash | None = None


class MissionDelivery(MissionControl):
    mode: Literal["preview", "squash", "keep_branch", "patch", "bundle", "abandon"] = "preview"
    token: MissionHash | None = None
    target_branch: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("target_branch")
    @classmethod
    def branch_name(cls, value):
        if value is not None and (value.startswith("-") or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError("Invalid target branch")
        return value


class MissionError(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = Field(default_factory=list)
    task_id: str | None = None


class MissionErrorResponse(BaseModel):
    error: MissionError


class MissionReview(BaseModel):
    task_id: str
    revision: int
    task_spec_artifact_id: str
    task_spec_hash: MissionHash
    plan_artifact_id: str
    plan_hash: MissionHash
    scope_hash: MissionHash
    review_hash: MissionHash


class MissionResponse(BaseModel):
    task_id: str
    status: str
    current_revision: int
    graph_id: str
    graph_version: int
    created_at: str
    updated_at: str
    finished_at: str | None
    repository_path: str
    request: str
    task_type: str
    orchestration_mode: str
    base_ref: str
    toolchain_profile: str
    review: MissionReview | None
    last_event_id: str | None
    recovery_required: bool


class MissionNode(BaseModel):
    node_id: str
    node_type: str
    status: str
    attempt: int
    result: dict[str, Any] | None


class MissionSummary(BaseModel):
    task_id: str
    status: str
    current_revision: int
    created_at: str
    updated_at: str
    finished_at: str | None
    repository_path: str
    request: str
    task_type: str
    orchestration_mode: str
    recovery_required: bool


class MissionPage(BaseModel):
    items: list[MissionSummary]
    total: int
    next_cursor: str | None


class MissionGraph(BaseModel):
    task_id: str
    graph_id: str
    graph_version: int
    definition_hash: str
    status: str
    current_revision: int
    nodes: list[MissionNode]
    definition: dict[str, Any]


class MissionArtifacts(BaseModel):
    task_id: str
    items: list[dict[str, Any]]


class MissionDiff(BaseModel):
    task_id: str
    patch: str
    change_sets: list[dict[str, Any]]


class MissionValidation(MissionArtifacts):
    latest: dict[str, Any] | None


class MissionRecoveryResponse(BaseModel):
    mission: MissionResponse
    recovery: dict[str, Any] | None


class MissionDeliveryResponse(BaseModel):
    mission: MissionResponse
    delivery: dict[str, Any]
