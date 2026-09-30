"""One OpenAI-compatible Chat Completions client, with injectable transport."""

import asyncio
import json
import math
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ForgePilotError
from .security import Redactor


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


class ToolSpec(BaseModel):
    """Provider-neutral definition. Call/result messages use the ChatReply contract."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    description: str
    input_schema: dict


class ModelClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None) -> ChatReply: ...
    async def aclose(self) -> None: ...


def context_size(messages, tools):
    """Count definitions as well as messages against the request context budget."""
    return len(json.dumps({"messages": messages, "tools": [t.model_dump() for t in tools or []]}, ensure_ascii=False))


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str, *,
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if (not all(isinstance(v, str) and v.strip() for v in (base_url, api_key, model))
                or len(model) > 256 or len(api_key) > 4_096
                or any(ord(c) < 32 for c in api_key)):
            raise ForgePilotError("llm_config", "base_url, api_key and model are required")
        try:
            url = httpx.URL(base_url)
        except (httpx.InvalidURL, ValueError):
            raise ForgePilotError("llm_config", "base_url is not a valid API root URL") from None
        if (url.scheme not in ("http", "https") or not url.host or url.userinfo
                or url.query or url.fragment):
            raise ForgePilotError("llm_config", "Use an http(s) API root without credentials/query/fragment")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ForgePilotError("llm_config", "Request timeout must be in (0, 600] seconds")
        self._url = str(url).rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.redactor = Redactor((api_key,))
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout,
                                       follow_redirects=False, trust_env=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None) -> ChatReply:
        payload = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema,
            }} for t in tools]
        try:
            # Wall timeout also covers mock/custom transports and slow body streams.
            async with asyncio.timeout(self.timeout):
                async with self._client.stream(
                    "POST", self._url, json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        status = response.status_code
                        reason = ("模型鉴权失败，当前 API Key 未被服务接受。请核对密钥与 Base URL 是否属于同一服务；"
                                  "运行 Configure-ForgePilot.cmd 更新密钥后，依次运行 Stop-ForgePilot.cmd 和 Start-ForgePilot.cmd。"
                                  if status == 401 else
                                  "模型访问被拒绝，请检查当前 API Key 的模型访问权限及服务端访问限制。" if status == 403 else
                                  "Rate limited" if status == 429 else
                                  "Redirects are disabled" if 300 <= status < 400 else
                                  "Check API endpoint, model and service availability")
                        # Never include the URL, headers, response body or raw exception.
                        raise ForgePilotError("llm_http", f"HTTP {status}. {reason}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1_048_576:
                            raise ForgePilotError("llm_response_limit", "Response exceeds 1 MiB")
                        body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
                raise ValueError("Expected exactly one choice")
            choice = data["choices"][0]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ForgePilotError("llm_incomplete", "Response was truncated, filtered or not completed")
            message = AssistantMessage.model_validate(choice["message"])
            calls = message.tool_calls or []
            if len({c.id for c in calls}) != len(calls):
                raise ValueError("Duplicate tool call ids")
            if not calls and not (message.content or "").strip():
                raise ValueError("Empty response")
            if choice["finish_reason"] == "tool_calls" and not calls:
                raise ValueError("Missing tool calls")
            usage = data.get("usage")
            if usage is not None:
                if not isinstance(usage, dict):
                    raise ValueError("Invalid usage")
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                        raise ValueError("Invalid token count")
            return ChatReply(message=message, usage=usage)
        except (TimeoutError, httpx.TimeoutException):
            raise ForgePilotError("llm_timeout", "LLM request timed out") from None
        except httpx.HTTPError:
            raise ForgePilotError("llm_network", "LLM transport failed; check service and network") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ValidationError):
            raise ForgePilotError("llm_protocol", "Invalid Chat Completions response structure") from None
