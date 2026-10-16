"""v1 transport schemas. Runtime state/authority never comes from request JSON."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .context import TaskState
from .planning import TaskPlan
from .mcp_config import (MCPHTTPServer, MCPStdioServer, MCPHTTPSettings, MCPStdioSettings,
                         CredentialUpdates, Revision)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateTask(RequestModel):
    repository_path: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=16_384)

    @field_validator("repository_path", "prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Value must be nonblank and contain no NUL")
        return value


class ApproveTask(RequestModel):
    plan_version: int = Field(ge=1)


class PlanFeedback(RequestModel):
    plan_version: int = Field(ge=1)
    feedback: str = Field(min_length=1, max_length=4000)

    @field_validator("feedback")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Feedback must be nonblank and contain no NUL")
        return value.strip()


class CreateConversation(RequestModel):
    title: str = Field(default='新对话',min_length=1,max_length=80)
    repository_path: str | None = Field(default=None,min_length=1,max_length=4096)

    @field_validator('title','repository_path')
    @classmethod
    def clean(cls,value):
        if value is not None and (not value.strip() or '\x00' in value):
            raise ValueError('Value must be nonblank and contain no NUL')
        return value


class UpdateConversation(CreateConversation):
    pass  # model_fields_set distinguishes an omitted field from unlinking the project with null.


class ConversationRequest(RequestModel):
    request_id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    mode: Literal['chat','task'] = 'chat'
    tool_mode: Literal['fast','web','project','auto'] = 'auto'
    prompt: str = Field(min_length=1,max_length=16_384)

    @field_validator('prompt')
    @classmethod
    def nonblank(cls,value):
        if not value.strip() or '\x00' in value: raise ValueError('Invalid prompt')
        return value


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    repository_path: str | None
    created_at: str
    updated_at: str


class MCPToolActivity(BaseModel):
    sequence: int = Field(ge=1,le=8)
    server_name: str
    tool_name: str
    status: Literal['running','succeeded','failed','cancelled','interrupted']
    duration_ms: int = Field(ge=0)
    error_code: str | None


class MCPSource(BaseModel):
    title: str = Field(max_length=256)
    url: str = Field(max_length=2048)


class MCPCallRecord(MCPToolActivity):
    server_id: str
    tool_call_id: str
    arguments_excerpt: str = Field(max_length=1024)
    result_excerpt: str = Field(max_length=2048)
    sources: list[MCPSource] = Field(max_length=10)


class ConversationMCPTools(BaseModel):
    conversation_id: str
    request_id: str
    items: list[MCPCallRecord] = Field(max_length=8)


class ConversationRequestState(BaseModel):
    request_id: str
    conversation_id: str
    mode: Literal['chat','task']
    prompt: str
    repository_path: str | None
    status: Literal['running','completed','failed','cancelled','interrupted']
    task_id: str | None
    created_at: str
    updated_at: str
    error: str | None
    context_notice: str
    tool_activity: MCPToolActivity | None = None


class ConversationMessage(BaseModel):
    message_id: int
    conversation_id: str
    request_id: str
    role: Literal['user','assistant']
    content: str
    created_at: str


class ConversationDetail(ConversationSummary):
    messages: list[ConversationMessage]
    next_before: int | None
    active_request: ConversationRequestState | None
    last_request: ConversationRequestState | None
    context_notice: str


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    total: int
    next_cursor: str | None


class ConversationDeleted(BaseModel):
    conversation_id: str
    deleted: Literal[True]


class MemoryCreate(RequestModel):
    content: str = Field(min_length=1,max_length=500)
    scope: Literal['global','project'] = 'global'
    repository_path: str | None = Field(default=None,min_length=1,max_length=4096)
    source_conversation_id: str | None = Field(default=None,min_length=1,max_length=36)
    source_message_id: int | None = Field(default=None,ge=1)


class MemoryUpdate(RequestModel):
    content: str = Field(min_length=1,max_length=500)
    scope: Literal['global','project']
    repository_path: str | None = Field(default=None,min_length=1,max_length=4096)
    revision: int = Field(ge=1)


class MemoryRecord(BaseModel):
    memory_id: str
    content: str
    scope: Literal['global','project']
    project_key: str | None
    source_conversation_id: str | None
    source_message_id: int | None
    source_title: str | None
    revision: int
    created_at: str
    updated_at: str


class MemoryList(BaseModel):
    items: list[MemoryRecord]
    total: int
    project_key: str | None
    injection_limit: int
    injection_chars: int


class MemoryDeleted(BaseModel):
    memory_id: str
    deleted: Literal[True]


class ContextSummary(BaseModel):
    conversation_id: str
    content: str
    through_message_id: int
    project_key: str | None
    updated_at: str


class SummaryState(BaseModel):
    conversation_id: str
    revision: int
    enabled: bool
    summary: ContextSummary | None


class SummaryUpdate(RequestModel):
    revision: int = Field(ge=0)
    content: str | None = Field(default=None,min_length=1,max_length=4000)
    enabled: bool | None = None

    @model_validator(mode='after')
    def require_change(self):
        if self.content is None and self.enabled is None: raise ValueError('A summary change is required')
        return self


class CancelTask(RequestModel):
    reason: Literal["user", "reject"] = "user"
    plan_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def reject_requires_version(self):
        if (self.reason == "reject") != (self.plan_version is not None):
            raise ValueError("Reject requires plan_version; ordinary cancel must omit it")
        return self


def checked_rag_repository_path(value: str) -> str:
    if (not value or len(value) > 4096 or value != value.strip()
            or any(not character.isprintable() for character in value)):
        raise ValueError("Repository path must be bounded text without control characters or surrounding whitespace")
    return value


class RagIndexRequest(RequestModel):
    repository_path: str = Field(min_length=1, max_length=4096)

    @field_validator("repository_path")
    @classmethod
    def valid_repository_path(cls, value):
        return checked_rag_repository_path(value)


class RagIndexStatusResponse(BaseModel):
    repository_id: str = Field(min_length=64, max_length=64)
    enabled: bool
    available: bool
    status: Literal[
        "disabled", "unavailable", "empty", "pending", "indexing", "cancelling",
        "ready", "degraded", "failed", "cancelled",
    ]
    total_files: int = Field(ge=0)
    processed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    chunks: int = Field(ge=0)
    file_count: int = Field(ge=0)
    dirty_file_count: int = Field(ge=0)
    failed_file_count: int = Field(ge=0)
    last_indexed_at: str | None = None
    running: bool
    operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    operation: Literal["index", "clear"] | None = None
    revision: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=128)


class RagIndexDeleted(RagIndexStatusResponse):
    deleted_files: int = Field(ge=0)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict] = Field(default_factory=list)
    task_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class TaskSummary(BaseModel):
    task_id: str
    repository_path: str
    prompt: str
    state: TaskState
    created_at: str
    updated_at: str
    finished_at: str | None = None
    plan_version: int
    repair_round: int
    outcome: str | None
    error: str | None
    error_code: str | None
    cancel_requested: bool = False
    last_event_id: str | None = None
    read_only: bool = False


class TaskResponse(TaskSummary):
    analysis_result: str | None
    final_result: str | None
    plan: TaskPlan | None
    approved_plan_version: int | None
    approved_files: list[str]
    steps: int
    tool_calls: int
    usage: dict[str, int | None]
    duration_ms: int
    max_repair_rounds: int
    repair_records: list[dict]
    validation_history: list[dict]
    last_validation_error: dict | None
    failure_kind: str | None
    validation_errors: list[dict]
    build_result: dict | None
    task_diff: dict | None
    diff_summary: list[dict] = Field(default_factory=list)
    duration_complete: bool = True


class TaskList(BaseModel):
    items: list[TaskSummary]
    total: int
    next_cursor: str | None = None


class TraceSummary(BaseModel):
    sequence: int
    trace_sequence: int
    kind: str
    timestamp: str
    preview: str
    size: int


class TracePage(BaseModel):
    items: list[TraceSummary]
    next_cursor: int | None = None


class TraceDetail(BaseModel):
    task_id: str
    sequence: int
    trace_sequence: int
    kind: str
    timestamp: str
    data: dict


EventName = Literal["task_state_changed", "message", "plan_created", "approval_required",
    "tool_started", "tool_finished", "file_changed", "build_started", "build_finished",
    "test_started", "test_finished", "reflection", "task_completed", "task_failed"]


class EventPayload(BaseModel):
    event: EventName
    task_id: str
    timestamp: str
    data: dict


ConversationEventType = Literal[
    "answer_started", "answer_delta", "answer_snapshot",
    "answer_segment_discarded", "answer_segment_committed", "tool_activity",
    "answer_completed", "answer_cancelled", "answer_failed",
]
ConversationId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
SegmentId = Annotated[str, Field(min_length=1, max_length=128)]


class ConversationEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: ConversationEventType
    conversation_id: ConversationId
    request_id: ConversationId
    revision: int = Field(ge=1)
    segment_id: SegmentId | None


class AnswerStarted(ConversationEventBase):
    type: Literal["answer_started"]
    segment_id: SegmentId


class AnswerDelta(ConversationEventBase):
    type: Literal["answer_delta"]
    segment_id: SegmentId
    previous_revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=32_768)


class AnswerSnapshot(ConversationEventBase):
    type: Literal["answer_snapshot"]
    content: str = Field(max_length=32_768)
    status: Literal["running", "streaming", "working", "committed", "stopping",
                    "completed", "cancelled", "failed"]
    terminal: bool


class AnswerSegmentDiscarded(ConversationEventBase):
    type: Literal["answer_segment_discarded"]
    segment_id: SegmentId


class AnswerSegmentCommitted(ConversationEventBase):
    type: Literal["answer_segment_committed"]
    segment_id: SegmentId
    content: str = Field(max_length=32_768)


class ToolActivityEvent(ConversationEventBase):
    type: Literal["tool_activity"]
    segment_id: SegmentId
    activity: Literal["started", "finished"]
    tool_call_id: str | None = Field(max_length=128)
    tool_name: str | None = Field(max_length=64)
    success: bool | None = None


class _AnswerTerminal(ConversationEventBase):
    content: str | None = Field(default=None, max_length=32_768)
    message_id: int | None = Field(default=None, ge=1)
    released: bool | None = None


class AnswerCompleted(_AnswerTerminal):
    type: Literal["answer_completed"]


class AnswerCancelled(_AnswerTerminal):
    type: Literal["answer_cancelled"]


class AnswerFailed(_AnswerTerminal):
    type: Literal["answer_failed"]
    error_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    persisted: bool | None = None


ConversationEvent = Annotated[
    AnswerStarted | AnswerDelta | AnswerSnapshot | AnswerSegmentDiscarded |
    AnswerSegmentCommitted | ToolActivityEvent | AnswerCompleted |
    AnswerCancelled | AnswerFailed,
    Field(discriminator="type"),
]


# MCP management has fixed wire models; secrets are write-only request fields.
MCPServerID = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]
MCPCatalogHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _MCPInput(RequestModel):
    secret_updates: CredentialUpdates = Field(default_factory=dict, repr=False, json_schema_extra={"writeOnly": True},
        description="api_key or env:NAME: omit to retain, string to replace, null to clear. Never send a mask.")

    def configuration(self):
        return self.model_dump(mode="json", exclude={"server_id", "base_revision", "catalog_hash", "secret_updates"})


class _MCPProbeInput(_MCPInput):
    server_id: MCPServerID | None = None
    base_revision: Revision = 0


class _MCPSaveInput(_MCPInput):
    base_revision: Revision
    catalog_hash: MCPCatalogHash


class MCPHTTPProbe(MCPHTTPServer, _MCPProbeInput):
    pass


class MCPStdioProbe(MCPStdioServer, _MCPProbeInput):
    pass


class MCPHTTPSave(MCPHTTPServer, _MCPSaveInput):
    pass


class MCPStdioSave(MCPStdioServer, _MCPSaveInput):
    pass


MCPProbeInput = Annotated[MCPHTTPProbe | MCPStdioProbe, Field(discriminator="transport")]
MCPSaveInput = Annotated[MCPHTTPSave | MCPStdioSave, Field(discriminator="transport")]


class MCPTestInput(RequestModel):
    revision: Revision = Field(ge=1)


class MCPEnableInput(MCPTestInput):
    enabled: bool


class MCPToolInfo(BaseModel):
    name: str
    title: str | None
    description: str
    compatible: bool
    reason: str | None
    input_schema: dict | None
    output_schema: dict | None
    # Malformed optional metadata is retained for incompatible discovery entries.
    execution: object
    annotations: object
    fingerprint: MCPCatalogHash


class MCPServerSummary(BaseModel):
    server_id: MCPServerID
    revision: int = Field(ge=1)
    name: str
    transport: Literal["streamable_http", "stdio"]
    enabled: bool
    selected_tool_count: int = Field(ge=0, le=20)
    last_checked_at: str | None
    last_check_status: Literal["success", "failed"] | None
    last_error_code: str | None
    created_at: str
    updated_at: str


class MCPServerList(BaseModel):
    items: list[MCPServerSummary] = Field(max_length=8)
    disabled: bool = Field(description="Startup --disable-mcp; viewing, disabling and deleting remain available.")


class _MCPDetail(MCPServerSummary):
    tools: list[MCPToolInfo] = Field(max_length=128)
    catalog_hash: MCPCatalogHash
    has_secret: bool


class MCPHTTPDetail(MCPHTTPServer, _MCPDetail):
    pass


class MCPStdioDetail(MCPStdioServer, _MCPDetail):
    pass


MCPServerDetail = Annotated[MCPHTTPDetail | MCPStdioDetail, Field(discriminator="transport")]


class MCPServerInfo(BaseModel):
    name: str
    version: str


class MCPProbeResponse(BaseModel):
    tools: list[MCPToolInfo] = Field(max_length=128)
    catalog_hash: MCPCatalogHash
    server_info: MCPServerInfo
    checked_at: str
    connection: MCPHTTPSettings | MCPStdioSettings
    default_selected_tools: list[str] = Field(max_length=20)


class MCPPreset(BaseModel):
    preset_id: Literal["tavily"]
    name: str
    transport: Literal["streamable_http"]
    connection: MCPHTTPSettings
    default_tools: list[str]
    required_secrets: list[str]
    real_service_verified: bool


class MCPPresetList(BaseModel):
    items: list[MCPPreset] = Field(max_length=1)


class MCPDeleted(BaseModel):
    server_id: MCPServerID
    deleted: Literal[True]
