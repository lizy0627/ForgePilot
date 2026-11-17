"""One OpenAI-compatible Chat Completions client, with injectable transport."""

import asyncio
import codecs
import json
import math
import threading
from collections.abc import AsyncIterator, Callable
from typing import Literal

import httpx
from pydantic import ValidationError

from ..errors import ForgePilotError
from ..security import Redactor
from ..stream_metrics import StreamMetrics


from .contracts import (AssistantMessage, ChatReply, ChatStreamEvent, ContentDelta,
                        StreamFinished, ToolCall, ToolCallDelta, ToolSpec)
from .errors import ModelError, http_error


class OpenAICompatibleClient:
    _RESPONSE_LIMIT = 1_048_576
    _EVENT_LIMIT = 65_536
    _CONTENT_LIMIT = 32_768
    _TOOL_CALL_LIMIT = 8
    _ARGUMENTS_LIMIT = 16_384
    supports_stream_diagnostics = True
    supports_output_token_limit = True

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 limits: httpx.Limits | None = None,
                 stream_include_usage: bool = False,
                 max_output_tokens: int | None = None, temperature: float | None = None) -> None:
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
        if type(stream_include_usage) is not bool:
            raise ForgePilotError("llm_config", "stream_include_usage must be a boolean")
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
            raise ForgePilotError("llm_config", "Invalid output token limit")
        if temperature is not None and (type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ForgePilotError("llm_config", "Invalid temperature")
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._url = str(url).rstrip("/") + "/chat/completions"
        # An explicit Doubao 2.1 output limit includes reasoning and answer tokens.
        self.output_token_parameter = ("max_completion_tokens"
            if url.host == "ark.cn-beijing.volces.com" and model.startswith("doubao-seed-2-1-")
            else "max_tokens")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.stream_include_usage = stream_include_usage
        self.redactor = Redactor((api_key,))
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout,
                                       limits=limits or httpx.Limits(max_connections=20,
                                           max_keepalive_connections=10, keepalive_expiry=30.0),
                                       follow_redirects=False, trust_env=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _output_limit(self, requested):
        if requested is None:
            return self.max_output_tokens
        if (type(requested) is not int or requested < 1
                or self.max_output_tokens is not None and requested > self.max_output_tokens):
            raise ForgePilotError("llm_config", "Request output limit must not exceed the configured limit")
        return requested

    async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None, *,
                   max_output_tokens: int | None = None) -> ChatReply:
        payload = {"model": self.model, "messages": messages, "stream": False}
        output_limit = self._output_limit(max_output_tokens)
        if output_limit is not None:
            payload[self.output_token_parameter] = output_limit
        if self.temperature is not None:
            payload["temperature"] = self.temperature
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
                        raise http_error(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1_048_576:
                            raise ForgePilotError("llm_response_limit", "Response exceeds 1 MiB")
                        body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
                raise ValueError("Expected exactly one choice")
            choice = data["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                raise ModelError("llm_incomplete", "Response was blocked by content filtering", category="policy")
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ForgePilotError("llm_incomplete", "Response was truncated, filtered or not completed")
            message = AssistantMessage.model_validate(choice["message"])
            calls = message.tool_calls or []
            if len({c.id for c in calls}) != len(calls):
                raise ValueError("Duplicate tool call ids")
            if not calls and not (message.content or "").strip():
                raise ValueError("Empty response")
            if choice["finish_reason"] == "stop" and calls:
                raise ValueError("Unexpected tool calls")
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
            raise ModelError("llm_timeout", "LLM request timed out", category="network") from None
        except httpx.HTTPError:
            raise ModelError("llm_network", "LLM transport failed; check service and network", category="network") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ValidationError):
            raise ForgePilotError("llm_protocol", "Invalid Chat Completions response structure") from None
        except ForgePilotError:
            raise
        except Exception:
            raise ModelError("llm_protocol", "LLM provider failed unexpectedly", category="request") from None

    async def stream_chat(self, messages: list[dict],
                          tools: list[ToolSpec] | None = None, *,
                          diagnostics: StreamMetrics | None = None,
                          model_started: float | None = None,
                          max_output_tokens: int | None = None) -> AsyncIterator[ChatStreamEvent]:
        """Yield validated, provider-neutral deltas from a Chat Completions SSE stream."""
        payload = {"model": self.model, "messages": messages, "stream": True}
        if self.stream_include_usage:
            payload["stream_options"] = {"include_usage": True}
        output_limit = self._output_limit(max_output_tokens)
        if output_limit is not None:
            payload[self.output_token_parameter] = output_limit
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema,
            }} for t in tools]

        content_parts: list[str] = []
        content_length = 0
        tool_states: list[dict[str, str | None]] = []
        finish_reason: Literal["stop", "tool_calls"] | None = None
        usage: dict | None = None
        saw_done = False

        try:
            async with asyncio.timeout(self.timeout):
                async with self._client.stream(
                    "POST", self._url, json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as response:
                    self._validate_stream_response(response)
                    async for event_data in self._iter_sse_data(
                            response, diagnostics=diagnostics, model_started=model_started):
                        if event_data == "[DONE]":
                            saw_done = True
                            break
                        data = self._load_stream_event(event_data)
                        choices = data.get("choices")
                        event_usage = data.get("usage")
                        if choices == [] and event_usage is not None:
                            if finish_reason is None or usage is not None:
                                raise ValueError("Unexpected usage event")
                            usage = self._validate_usage(event_usage)
                            continue
                        if not isinstance(choices, list) or len(choices) != 1:
                            raise ValueError("Expected exactly one choice")
                        if finish_reason is not None:
                            raise ValueError("Choice received after finish")
                        choice = choices[0]
                        if (not isinstance(choice, dict) or type(choice.get("index")) is not int
                                or choice["index"] != 0):
                            raise ValueError("Invalid choice index")
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            raise ValueError("Invalid delta")
                        if "role" in delta and delta["role"] != "assistant":
                            raise ValueError("Invalid delta role")

                        content = delta.get("content")
                        if content is not None:
                            if not isinstance(content, str):
                                raise ValueError("Invalid content delta")
                            content_length += len(content)
                            if content_length > self._CONTENT_LIMIT:
                                raise ForgePilotError("llm_response_limit", "Assistant content exceeds 32768 characters")
                            if content:
                                content_parts.append(content)
                                yield ContentDelta(content)

                        call_deltas = delta.get("tool_calls")
                        if call_deltas is not None:
                            if not isinstance(call_deltas, list):
                                raise ValueError("Invalid tool call deltas")
                            for call_delta in call_deltas:
                                event = self._apply_tool_delta(tool_states, call_delta)
                                yield event

                        raw_finish = choice.get("finish_reason")
                        if raw_finish is not None:
                            finish_reason = self._validate_finish_reason(raw_finish)
                            if event_usage is not None:
                                usage = self._validate_usage(event_usage)

            if not saw_done:
                raise ForgePilotError("llm_protocol", "Chat Completions stream ended without [DONE]")
            if finish_reason is None:
                raise ForgePilotError("llm_protocol", "Chat Completions stream ended without finish_reason")

            calls = self._validate_assembled_stream(finish_reason, content_parts, tool_states)
            if not calls and not "".join(content_parts).strip():
                raise ForgePilotError("llm_protocol", "Chat Completions stream contained no valid response")
            yield StreamFinished(finish_reason, usage)
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("llm_timeout", "LLM request timed out", category="network") from None
        except httpx.HTTPError:
            raise ModelError("llm_network", "LLM transport failed; check service and network", category="network") from None
        except ForgePilotError:
            raise
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError,
                AttributeError, RecursionError, ValidationError):
            raise ForgePilotError("llm_protocol", "Invalid streaming Chat Completions response") from None
        except Exception:
            # Custom transports must not make their exception text part of the public error.
            raise ModelError("llm_protocol", "LLM provider failed unexpectedly", category="request") from None

    def _validate_stream_response(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise http_error(response.status_code)
        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "text/event-stream":
            raise ForgePilotError("llm_protocol", "Streaming response must use text/event-stream")

    async def _iter_sse_data(self, response: httpx.Response, *,
                             diagnostics: StreamMetrics | None = None,
                             model_started: float | None = None) -> AsyncIterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text_buffer = ""
        data_lines: list[str] = []
        event_size = 0
        total_size = 0

        async for chunk in response.aiter_bytes():
            total_size += len(chunk)
            if total_size > self._RESPONSE_LIMIT:
                raise ForgePilotError("llm_response_limit", "Response exceeds 1 MiB")
            text_buffer += decoder.decode(chunk, final=False)
            while True:
                newline = text_buffer.find("\n")
                if newline < 0:
                    if len(text_buffer.encode("utf-8")) + event_size > self._EVENT_LIMIT:
                        raise ForgePilotError("llm_response_limit", "SSE event exceeds 65536 bytes")
                    break
                line, text_buffer = text_buffer[:newline], text_buffer[newline + 1:]
                if line.endswith("\r"):
                    line = line[:-1]
                event_size += len(line.encode("utf-8")) + 1
                if event_size > self._EVENT_LIMIT:
                    raise ForgePilotError("llm_response_limit", "SSE event exceeds 65536 bytes")
                if line == "":
                    if data_lines:
                        event_data = "\n".join(data_lines)
                        if event_data:
                            if diagnostics is not None and model_started is not None:
                                diagnostics.model_chunk(model_started)
                            yield event_data
                    data_lines = []
                    event_size = 0
                elif line.startswith(":"):
                    continue
                else:
                    field, separator, value = line.partition(":")
                    if separator and value.startswith(" "):
                        value = value[1:]
                    if field == "data":
                        data_lines.append(value)

        text_buffer += decoder.decode(b"", final=True)
        if text_buffer:
            if text_buffer.endswith("\r"):
                text_buffer = text_buffer[:-1]
            event_size += len(text_buffer.encode("utf-8"))
            if event_size > self._EVENT_LIMIT:
                raise ForgePilotError("llm_response_limit", "SSE event exceeds 65536 bytes")
            if text_buffer.startswith("data:"):
                value = text_buffer[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if data_lines:
            event_data = "\n".join(data_lines)
            if event_data:
                if diagnostics is not None and model_started is not None:
                    diagnostics.model_chunk(model_started)
                yield event_data

    @staticmethod
    def _load_stream_event(event_data: str) -> dict:
        data = json.loads(event_data)
        if not isinstance(data, dict):
            raise ValueError("Invalid event object")
        return data

    @staticmethod
    def _validate_usage(usage: object) -> dict:
        if not isinstance(usage, dict):
            raise ValueError("Invalid usage")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                raise ValueError("Invalid token count")
        return dict(usage)

    @staticmethod
    def _validate_finish_reason(reason: object) -> Literal["stop", "tool_calls"]:
        if reason == "stop":
            return "stop"
        if reason == "tool_calls":
            return "tool_calls"
        if reason == "length":
            raise ForgePilotError("llm_incomplete", "Streaming response reached the model length limit")
        if reason == "content_filter":
            raise ModelError("llm_incomplete", "Streaming response was blocked by content filtering", category="policy")
        raise ForgePilotError("llm_incomplete", "Streaming response used an unsupported finish reason")

    def _apply_tool_delta(self, states: list[dict[str, str | None]], raw: object) -> ToolCallDelta:
        if not isinstance(raw, dict):
            raise ValueError("Invalid tool call delta")
        index = raw.get("index")
        if type(index) is not int or index < 0 or index >= self._TOOL_CALL_LIMIT:
            raise ValueError("Invalid tool call index")
        if index > len(states):
            raise ValueError("Tool call index skipped")
        if index == len(states):
            states.append({"id": None, "name": None, "arguments": ""})
        state = states[index]
        call_type = raw.get("type")
        if call_type is not None and call_type != "function":
            raise ValueError("Invalid tool call type")
        call_id = raw.get("id")
        if call_id is not None:
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Invalid tool call id")
            if state["id"] is not None and state["id"] != call_id:
                raise ValueError("Tool call id changed")
            state["id"] = call_id
        function = raw.get("function")
        if function is None:
            function = {}
        if not isinstance(function, dict):
            raise ValueError("Invalid function delta")
        name = function.get("name")
        if name is not None:
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid function name")
            if state["name"] is not None and state["name"] != name:
                raise ValueError("Function name changed")
            state["name"] = name
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            raise ValueError("Invalid function arguments delta")
        current_arguments = state["arguments"]
        assert isinstance(current_arguments, str)
        if len(current_arguments) + len(arguments) > self._ARGUMENTS_LIMIT:
            raise ForgePilotError("llm_response_limit", "Tool arguments exceed 16384 characters")
        state["arguments"] = current_arguments + arguments
        return ToolCallDelta(index, call_id, name, arguments)

    @staticmethod
    def _validate_assembled_stream(finish_reason: Literal["stop", "tool_calls"],
                                   content_parts: list[str],
                                   states: list[dict[str, str | None]]) -> list[ToolCall]:
        raw_calls = [{"id": state["id"], "type": "function", "function": {
            "name": state["name"], "arguments": state["arguments"],
        }} for state in states]
        message = AssistantMessage.model_validate({
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "tool_calls": raw_calls or None,
        })
        calls = message.tool_calls or []
        if finish_reason == "tool_calls" and not calls:
            raise ValueError("Missing tool calls")
        if finish_reason == "stop" and calls:
            raise ValueError("Unexpected tool calls")
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("Duplicate tool call ids")
        return calls


class PooledModelClient:
    """A non-owning, model-specific view of a shared HTTP client.

    Request services may close this handle using their existing ownership rules;
    only OpenAIClientPool owns and closes the underlying connection pool.
    """
    supports_stream_diagnostics = True

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client
        self.model = client.model
        self.redactor = client.redactor

    async def chat(self, messages: list[dict],
                   tools: list[ToolSpec] | None = None) -> ChatReply:
        return await self._client.chat(messages, tools=tools)

    async def stream_chat(self, messages: list[dict],
                          tools: list[ToolSpec] | None = None, *,
                          diagnostics: StreamMetrics | None = None,
                          model_started: float | None = None) -> AsyncIterator[ChatStreamEvent]:
        async for event in self._client.stream_chat(
                messages, tools=tools, diagnostics=diagnostics, model_started=model_started):
            yield event

    async def aclose(self) -> None:
        # The application-level pool owns the transport lifetime.
        return None


class OpenAIClientPool:
    """Own one reusable OpenAI-compatible HTTP client per configured model."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0,
                 max_connections: int = 20, max_keepalive_connections: int = 10,
                 keepalive_expiry: float = 30.0,
                 stream_include_usage: bool = False,
                 transport_factory: Callable[[str], httpx.AsyncBaseTransport | None] | None = None) -> None:
        if (type(max_connections) is not int or type(max_keepalive_connections) is not int
                or max_connections < 1 or not 0 <= max_keepalive_connections <= max_connections
                or type(keepalive_expiry) not in (int, float)
                or not math.isfinite(keepalive_expiry) or keepalive_expiry < 0):
            raise ForgePilotError("llm_config", "Invalid HTTP connection pool limits")
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._limits = httpx.Limits(max_connections=max_connections,
                                    max_keepalive_connections=max_keepalive_connections,
                                    keepalive_expiry=float(keepalive_expiry))
        self._stream_include_usage = stream_include_usage
        self._transport_factory = transport_factory
        self._clients: dict[str, OpenAICompatibleClient] = {}
        self._closed = False
        self._lock = threading.Lock()

    def lease(self, model: str) -> PooledModelClient:
        with self._lock:
            if self._closed:
                raise ForgePilotError("server_stopping", "Model connection pool is closed")
            client = self._clients.get(model)
            if client is None:
                transport = self._transport_factory(model) if self._transport_factory is not None else None
                client = OpenAICompatibleClient(
                    self._base_url, self._api_key, model, timeout=self._timeout,
                    transport=transport, limits=self._limits,
                    stream_include_usage=self._stream_include_usage)
                self._clients[model] = client
            return PooledModelClient(client)

    async def aclose(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        results = await asyncio.gather(*(client.aclose() for client in clients),
                                       return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise ForgePilotError("llm_cleanup", "One or more model connection pools could not be closed")
