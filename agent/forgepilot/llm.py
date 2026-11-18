"""Compatibility imports. New application calls use models.gateway.ModelGateway."""
import httpx  # Preserves the historical transport-injection seam.
from .models.contracts import (AssistantMessage, ChatReply, ChatStreamEvent, ContentDelta,
    FunctionCall, ModelClient, StreamFinished, ToolCall, ToolCallDelta, ToolSpec, context_size)
from .models.openai import OpenAICompatibleClient, OpenAIClientPool, PooledModelClient

__all__ = ["AssistantMessage", "ChatReply", "ChatStreamEvent", "ContentDelta",
    "FunctionCall", "ModelClient", "StreamFinished", "ToolCall", "ToolCallDelta",
    "ToolSpec", "context_size", "OpenAICompatibleClient", "OpenAIClientPool", "PooledModelClient"]
