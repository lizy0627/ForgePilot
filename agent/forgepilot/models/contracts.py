"""Provider-neutral model messages and streaming events."""
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol
import json
from pydantic import BaseModel, ConfigDict, Field
from ..stream_metrics import StreamMetrics


class FunctionCall(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(max_length=16_384)


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"]
    function: FunctionCall


class AssistantMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["assistant"]
    content: str | None = Field(default=None, max_length=32_768)
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=8)


class ChatReply(BaseModel):
    message: AssistantMessage
    usage: dict | None = None


@dataclass(frozen=True, slots=True)
class ContentDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    call_id: str | None
    name: str | None
    arguments: str


@dataclass(frozen=True, slots=True)
class StreamFinished:
    finish_reason: Literal["stop", "tool_calls"]
    usage: dict | None


ChatStreamEvent = ContentDelta | ToolCallDelta | StreamFinished


class ToolSpec(BaseModel):
    """Provider-neutral definition. Call/result messages use the ChatReply contract."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    description: str
    input_schema: dict


class ModelClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None) -> ChatReply: ...
    def stream_chat(self, messages: list[dict], tools: list[ToolSpec] | None = None, *,
                    diagnostics: StreamMetrics | None = None,
                    model_started: float | None = None) -> AsyncIterator[ChatStreamEvent]: ...
    async def aclose(self) -> None: ...


def context_size(messages, tools):
    """Count definitions as well as messages against the request context budget."""
    return len(json.dumps({"messages": messages, "tools": [t.model_dump() for t in tools or []]}, ensure_ascii=False))
