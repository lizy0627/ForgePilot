"""One model boundary for routing, bounded recovery, streaming and accounting.

Provider objects perform exactly one attempt. This layer alone owns retries;
fallback never selects or changes a tool registry, principal, or capability set.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
from threading import Lock
from time import monotonic
from typing import Callable
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from ..errors import ForgePilotError
from ..security import Redactor
from .accounting import Accounting, AttemptRecord, ModelBudget, input_reservation, response_hash
from .contracts import AssistantMessage, ChatReply, ContentDelta, StreamFinished, ToolCallDelta
from .errors import ModelError, category_of, http_error
from .openai import OpenAICompatibleClient
from .profiles import ModelConfiguration, ModelProfile, ModelRoute, SecretResolver
from .tool_call_parser import validate_tool_calls


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _output_validator(schema):
    if schema is None:
        return None
    try:
        schema = deepcopy(schema)
        if len(json.dumps(schema, allow_nan=False)) > 262_144:
            raise ValueError("Schema too large")
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, registry=Registry(retrieve=_no_remote))
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise ForgePilotError("llm_config", "Invalid output JSON Schema") from None


def _validate_reply(reply, tools, validator):
    try:
        reply = ChatReply.model_validate(reply)
        usage = OpenAICompatibleClient._validate_usage(reply.usage) if reply.usage is not None else None
        # Never propagate vendor extras (including reasoning or service metadata).
        if usage is not None:
            usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                     if key in usage}
        reply = reply.model_copy(update={"usage": usage})
    except (ValueError, TypeError):
        raise ModelError("llm_protocol", "Invalid model response", category="schema") from None
    calls = reply.message.tool_calls or []
    if calls:
        validate_tool_calls(calls, tools or [])
    elif not (reply.message.content or "").strip():
        raise ModelError("llm_schema", "Empty model response", category="schema")
    elif validator is not None:
        try:
            value = json.loads(reply.message.content, object_pairs_hook=_json_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            validator.validate(value)
        except (ValueError, TypeError, RecursionError, ValidationError, Unresolvable):
            raise ModelError("llm_schema", "Response does not match output JSON Schema",
                             category="schema") from None
    return reply


class _StreamReply:
    def __init__(self):
        self.content = ""
        self.calls = []
        self.finished = False
        self.usage = None

    def add(self, event):
        if self.finished:
            raise ModelError("llm_protocol", "Event after stream finish", category="schema")
        if isinstance(event, ContentDelta):
            self.content += event.text
            if len(self.content) > 32_768:
                raise ForgePilotError("llm_response_limit", "Assistant content limit exceeded")
        elif isinstance(event, ToolCallDelta):
            if type(event.index) is not int or not 0 <= event.index <= len(self.calls) or event.index >= 8:
                raise ModelError("llm_protocol", "Invalid stream tool index", category="schema")
            if event.index == len(self.calls):
                self.calls.append({"id": None, "type": "function", "function": {"name": None, "arguments": ""}})
            call = self.calls[event.index]
            if event.call_id is not None:
                if call["id"] not in (None, event.call_id):
                    raise ModelError("llm_protocol", "Changed stream tool identity", category="schema")
                call["id"] = event.call_id
            if event.name is not None:
                if call["function"]["name"] not in (None, event.name):
                    raise ModelError("llm_protocol", "Changed stream tool name", category="schema")
                call["function"]["name"] = event.name
            call["function"]["arguments"] += event.arguments
            if len(call["function"]["arguments"]) > 16_384:
                raise ForgePilotError("llm_response_limit", "Tool argument limit exceeded")
        elif isinstance(event, StreamFinished):
            if event.finish_reason not in ("stop", "tool_calls") or (event.finish_reason == "tool_calls") != bool(self.calls):
                raise ModelError("llm_protocol", "Invalid stream finish", category="schema")
            self.finished = True
            self.usage = event.usage
        else:
            raise ModelError("llm_protocol", "Unknown stream event", category="schema")

    def reply(self):
        if not self.finished:
            raise ModelError("llm_protocol", "Stream was truncated", category="incomplete")
        try:
            return ChatReply(message=AssistantMessage(role="assistant", content=self.content or None,
                                                      tool_calls=self.calls or None), usage=self.usage)
        except ValueError:
            raise ModelError("llm_protocol", "Invalid assembled stream", category="schema") from None

    def hash(self):
        return response_hash({"content": self.content, "tool_calls": self.calls}) if self.content or self.calls else None


class ModelGateway:
    def __init__(self, configuration: ModelConfiguration, *, secret_resolver=None,
                 provider_factory=None, transport_factory=None, accounting=None):
        self.configuration = ModelConfiguration.model_validate(configuration)
        self.router = self.configuration.router()
        resolver = secret_resolver or SecretResolver()
        try:
            self._keys = {p.profile_id: resolver(p.api_key_ref) for p in self.configuration.profiles}
            if any(not isinstance(k, str) or not k.strip() or len(k) > 4096
                   or any(ord(c) < 32 or ord(c) == 127 for c in k) for k in self._keys.values()):
                raise ValueError("Invalid credential")
        except Exception:
            raise ForgePilotError("llm_config", "Model credential reference could not be resolved") from None
        self.redactor = Redactor(tuple(self._keys.values()))
        self.accounting = accounting if accounting is not None else Accounting()
        self._provider_factory = provider_factory
        self._transport_factory = transport_factory
        self._providers = {}
        self._lock = Lock()
        self._closed = False

    @classmethod
    def from_legacy(cls, base_url, api_key, models, *, timeout=60.0, transport=None,
                    transport_factory=None, accounting=None):
        try:
            profiles, routes, identifiers = [], [], {}
            for use_case, model in models.items():
                if model not in identifiers:
                    identifier = f"legacy-{len(identifiers)}"
                    identifiers[model] = identifier
                    profiles.append(ModelProfile(profile_id=identifier, base_url=base_url,
                        api_key_ref="env:FORGEPILOT_API_KEY", model=model, family="legacy",
                        timeout_seconds=timeout))
                routes.append(ModelRoute(route_id=use_case, primary=identifiers[model]))
            config = ModelConfiguration(profiles=tuple(profiles), routes=tuple(routes),
                                        enforce_review_family_separation=False)
        except (ValueError, TypeError, AttributeError):
            raise ForgePilotError("llm_config", "Invalid legacy model configuration") from None
        return cls(config, secret_resolver=lambda _: api_key, accounting=accounting,
                   transport_factory=(transport_factory or (lambda _: transport)) if transport is not None or transport_factory else None)

    def _provider(self, profile):
        with self._lock:
            if self._closed:
                raise ForgePilotError("server_stopping", "Model gateway is closed")
            if profile.profile_id not in self._providers:
                key = self._keys[profile.profile_id]
                if self._provider_factory:
                    provider = self._provider_factory(profile, key)
                else:
                    provider = OpenAICompatibleClient(profile.base_url, key, profile.model,
                        timeout=profile.timeout_seconds, max_output_tokens=profile.max_output_tokens,
                        temperature=profile.temperature, stream_include_usage=True,
                        transport=self._transport_factory(profile) if self._transport_factory else None)
                self._providers[profile.profile_id] = provider
            return self._providers[profile.profile_id]

    def for_route(self, use_case, *, role=None, budget=None, prompt_version=None, capabilities=None):
        if self._closed:
            raise ForgePilotError("server_stopping", "Model gateway is closed")
        route = self.router.resolve(use_case, role)
        if prompt_version is not None and not re.fullmatch(r"[a-z][a-z0-9._-]*\.v[1-9][0-9]*", prompt_version):
            raise ForgePilotError("llm_config", "Invalid prompt version")
        return GatewayClient(self, use_case, route, role=role, budget=budget,
                             prompt_version=prompt_version, capabilities=capabilities)

    def for_agent(self, definition, *, budget=None):
        from ..agents.definitions import AgentDefinition
        definition = AgentDefinition.model_validate(definition)
        self.router.validate_agent(definition)
        if budget is not None:
            if (not isinstance(budget, ModelBudget) or any(
                    getattr(budget, name) > getattr(definition.limits, name)
                    for name in ("max_model_calls", "max_tool_calls"))
                    or budget.timeout_seconds > definition.limits.timeout_seconds):
                raise ForgePilotError("llm_budget", "Budget exceeds AgentDefinition limits")
        return self.for_route(definition.model_route, role=definition.role,
            prompt_version=definition.prompt_version, capabilities=definition.capabilities,
            budget=budget if budget is not None else ModelBudget.from_limits(definition.limits))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            providers = tuple(self._providers.values())
            self._providers.clear()
            self._keys.clear()
        results = await asyncio.gather(*(p.aclose() for p in providers), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise ForgePilotError("llm_cleanup", "Model gateway cleanup failed")


class GatewayClient:
    """Non-owning compatibility handle; Agent/node budgets persist on this handle."""
    supports_stream_diagnostics = True

    def __init__(self, gateway, use_case, route, *, role, budget, prompt_version, capabilities):
        self.gateway, self.use_case, self.route = gateway, use_case, route
        self.role, self.budget, self.prompt_version = role, budget, prompt_version
        self.capabilities = frozenset(capabilities) if capabilities is not None else None
        self.model = route.primary.model
        self.redactor = gateway.redactor

    async def aclose(self):
        return None

    async def chat(self, messages, tools=None, *, output_schema=None,
                   is_cancelled: Callable[[], bool] | None = None):
        iterator = self._request(messages, tools, stream=False, output_schema=output_schema,
                                 is_cancelled=is_cancelled)
        try:
            async for reply in iterator:
                return reply
        finally:
            await iterator.aclose()

    async def stream_chat(self, messages, tools=None, *, diagnostics=None, model_started=None,
                          output_schema=None, is_cancelled: Callable[[], bool] | None = None):
        iterator = self._request(messages, tools, stream=True, output_schema=output_schema,
                                 diagnostics=diagnostics, model_started=model_started,
                                 is_cancelled=is_cancelled)
        try:
            async for event in iterator:
                yield event
        finally:
            await iterator.aclose()

    def _check_tools(self, tools):
        if self.role is None:
            return
        from ..policy import Capability, LEGACY_ALIASES, PolicyEngine, Principal, TOOL_CAPABILITIES
        for tool in tools or []:
            name = LEGACY_ALIASES.get(tool.name, tool.name)
            capability = TOOL_CAPABILITIES.get(name)
            if capability is None:
                # MCP admission and read-only approval must happen in the trusted registry.
                if name.startswith("mcp_"):
                    capability = Capability.MCP_READ
                else:
                    raise ForgePilotError("policy_denied", "Tool outside Agent capability policy")
            if self.capabilities is not None and capability not in self.capabilities:
                raise ForgePilotError("policy_denied", "Tool outside Agent declared capabilities")
            if capability != Capability.MCP_READ:
                PolicyEngine().require(Principal(self.role), capability)
            elif self.role not in ("root_cause_analyst", "impact_test_analyst"):
                raise ForgePilotError("policy_denied", "MCP is unavailable to this Agent")

    async def _request(self, messages, tools, *, stream, output_schema, diagnostics=None, model_started=None,
                       is_cancelled: Callable[[], bool] | None = None):
        messages, tools = deepcopy(messages), deepcopy(tools)
        self._check_tools(tools)
        validator = _output_validator(output_schema)
        if validator is not None:
            messages = [*messages, {"role": "system", "content":
                "Return the final response as JSON matching this output schema: "
                + json.dumps(output_schema, ensure_ascii=False)}]
        profile, attempt = self.route.primary, "primary"
        retry_count = fallback_count = 0
        repaired = False
        call_id = uuid4().hex
        while True:
            # A durable Mission cancel can precede its coroutine monitor. Check
            # every provider boundary, including recovery attempts, before
            # allocating a provider or reserving any additional model budget.
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError()
            if tools and not profile.supports_tool_calling or stream and not profile.supports_streaming:
                raise ForgePilotError("llm_config", "Model lacks a required request capability")
            provider = self.gateway._provider(profile)
            reserved = (input_reservation(messages, tools), 0)
            if self.budget is not None:
                self.budget.reserve(*reserved)
            started = monotonic()
            reply = None
            assembled = _StreamReply()
            emitted = False
            status = "success"
            error = None
            pending_events = []
            request_hash = response_hash({"messages": messages, "tools": [t.model_dump() for t in tools or []]})
            try:
                timeout = profile.timeout_seconds
                if self.budget is not None:
                    timeout = min(timeout, self.budget.remaining_seconds())
                async with asyncio.timeout(timeout):
                    if stream:
                        kwargs = {"tools": deepcopy(tools)}
                        if getattr(provider, "supports_stream_diagnostics", False):
                            kwargs.update(diagnostics=diagnostics, model_started=model_started)
                        iterator = provider.stream_chat(deepcopy(messages), **kwargs)
                        try:
                            async for event in iterator:
                                assembled.add(event)
                                if not isinstance(event, StreamFinished):
                                    if validator is not None or isinstance(event, ToolCallDelta):
                                        pending_events.append(event)
                                    else:
                                        emitted = True
                                        yield event
                        finally:
                            await iterator.aclose()
                        reply = assembled.reply()
                    else:
                        reply = await provider.chat(deepcopy(messages), tools=deepcopy(tools))
                    reply = _validate_reply(reply, tools, validator)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except (GeneratorExit, KeyboardInterrupt):
                status = "cancelled"
                raise
            except (TimeoutError, asyncio.TimeoutError):
                error = ModelError("llm_timeout", "LLM request timed out", category="network")
            except ForgePilotError as exc:
                category = category_of(exc)
                code = exc.code if re.fullmatch(r"[a-z_]{1,64}", exc.code) else "llm_protocol"
                status_code = getattr(exc, "status_code", None)
                if code == "llm_http" and type(status_code) is int and 100 <= status_code <= 599:
                    error = http_error(status_code)
                else:
                    error = ModelError(code, "Model request failed", category=category)
            except Exception:
                error = ModelError("llm_protocol", "Provider failed unexpectedly", category="request")
            finally:
                usage = reply.usage if isinstance(reply, ChatReply) else None
                # Only validated numeric fields can affect shared counters or persisted records.
                try:
                    usage = OpenAICompatibleClient._validate_usage(usage) if usage is not None else None
                except ValueError:
                    usage = None
                status = category_of(error) if error is not None else status
                counters = usage or {}
                input_tokens = counters.get("prompt_tokens", reserved[0])
                output_tokens = counters.get("completion_tokens")
                if output_tokens is None:
                    # In the absence of usage, estimate only the visible reply by UTF-8 bytes.
                    if isinstance(reply, ChatReply):
                        content, calls = reply.message.content or "", reply.message.tool_calls or []
                    else:
                        content, calls = assembled.content, assembled.calls
                    output_tokens = len(content.encode("utf-8", errors="replace"))
                    if calls:
                        serialized = [c.model_dump() if hasattr(c, "model_dump") else c for c in calls]
                        output_tokens += len(json.dumps(serialized, ensure_ascii=False).encode("utf-8", errors="replace"))
                if self.budget is not None:
                    self.budget.settle(reserved, {"prompt_tokens": input_tokens, "completion_tokens": output_tokens})
                record = AttemptRecord(call_id=call_id, profile_id=profile.profile_id,
                    model=self.redactor.text(profile.model), route=self.use_case, role=self.role,
                    prompt_version=self.prompt_version, attempt=attempt, status=status,
                    duration_ms=max(0, int((monotonic() - started) * 1000)),
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    usage_estimated=usage is None or "prompt_tokens" not in usage or "completion_tokens" not in usage,
                    input_hash=request_hash,
                    response_hash=response_hash(reply.message.model_dump()) if isinstance(reply, ChatReply) else assembled.hash(),
                    retry_count=retry_count, fallback_count=fallback_count)
                self.gateway.accounting.record(record)
            if error is None:
                if stream:
                    for event in pending_events:
                        yield event
                    yield StreamFinished("tool_calls" if reply.message.tool_calls else "stop", reply.usage)
                else:
                    yield reply
                return
            category = category_of(error)
            if emitted or repaired:
                raise error
            if category == "schema" or error.code == "llm_tool_schema":
                repaired = True
                attempt = "schema_repair"
                instruction = "Return one corrected response matching the supplied tool and output schemas. Use only the provided tools."
                if isinstance(reply, ChatReply):
                    # Invalid calls are quoted as data, never appended as an
                    # assistant tool batch without corresponding observations.
                    instruction += " Previous response to correct: " + json.dumps(
                        reply.message.model_dump(exclude_none=True), ensure_ascii=False)
                if output_schema is not None:
                    instruction += " Output JSON Schema: " + json.dumps(output_schema, ensure_ascii=False)
                messages = [*messages, {"role": "user", "content": instruction}]
                continue
            if category == "network" and profile == self.route.primary and retry_count == 0:
                retry_count = 1
                attempt = "retry"
                continue
            if category in ("network", "rate_limit", "server") and fallback_count == 0 and self.route.fallback is not None:
                fallback_count = 1
                profile = self.route.fallback
                attempt = "fallback"
                continue
            raise error

    async def run_tool_loop(self, messages, registry, *, output_schema=None, principal=None,
                            path_validator=None):
        from .tool_call_parser import execute_tool_calls
        from ..policy import Principal
        principal = principal or (Principal(self.role) if self.role else registry._principal())
        if self.role is not None and principal.role != self.role:
            raise ForgePilotError("policy_denied", "Agent role cannot be changed by a tool loop")
        budget = self.budget or ModelBudget()
        # Use the same immutable route/role/capabilities and budget for every round.
        client = GatewayClient(self.gateway, self.use_case, self.route, role=self.role,
                               budget=budget, prompt_version=self.prompt_version, capabilities=self.capabilities)
        history = deepcopy(messages)
        while True:
            with registry.as_capabilities(self.capabilities):
                specs = registry.get_specs(principal=principal)
            reply = await client.chat(history, specs, output_schema=output_schema)
            if not reply.message.tool_calls:
                return reply
            budget.consume_tools(len(reply.message.tool_calls))
            try:
                async with asyncio.timeout(budget.remaining_seconds()):
                    results = await execute_tool_calls(reply.message.tool_calls, specs, registry,
                        principal=principal, capabilities=self.capabilities, path_validator=path_validator)
            except TimeoutError:
                raise ForgePilotError("llm_budget", "Agent time budget exhausted during tools") from None
            # The registry may be shared or configured with only generic redaction.
            # Apply this gateway's primary/fallback secret set without mutating it.
            for result in results:
                observation = self.redactor.data(json.loads(result["content"]))
                if len(observation["output"]) > 16_384:
                    observation["output"] = observation["output"][:16_384] + "\n[TRUNCATED: tool output limit]"
                if observation.get("error"):
                    observation["error"] = observation["error"][:4_096]
                result["content"] = json.dumps(observation, ensure_ascii=False)
            history.append(reply.message.model_dump(exclude_none=True))
            history.extend(results)
