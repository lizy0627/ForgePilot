"""A small typed tool registry; schemas and validation share one source."""

from abc import ABC, abstractmethod
import asyncio
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import re
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ForgePilotError
from ..security import Redactor
from ..llm import ToolSpec
from ..policy import (FORBIDDEN_TOOLS, LEGACY_ALIASES, PolicyEngine,
                      Principal, Role, TOOL_CAPABILITIES, VALIDATOR_TOOLS)
from ..models.tool_call_parser import parse_arguments


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    success: bool
    output: str
    error: str | None
    duration_ms: int = Field(ge=0)
    mcp_sources: list[dict] = Field(default_factory=list, exclude=True, max_length=10)


class Tool(ABC):
    name: str
    description: str
    arguments_model: type[BaseModel]
    read_only = False
    concurrent_safe = False
    concurrency_group: object | None = None
    mcp_approved = False

    @property
    def input_schema(self) -> dict:
        return self.arguments_model.model_json_schema()

    def validate_arguments(self, arguments: object) -> dict:
        return self.arguments_model.model_validate(arguments, strict=True).model_dump()

    def format_output(self, output: str, redactor: Redactor) -> str:
        output = redactor.text(output)
        if len(output) > 16_384:
            output = output[:16_384] + "\n[TRUNCATED: tool output limit]"
        return output

    @abstractmethod
    async def execute(self, arguments: dict) -> ToolResult: ...


class FunctionTool(Tool):
    """Adapt an async text-returning function to the registry's result format."""
    def __init__(self, name, description, arguments_model, execute, *,
                 read_only=False, concurrent_safe=False, concurrency_group=None):
        if (type(read_only) is not bool or type(concurrent_safe) is not bool
                or concurrent_safe and not read_only):
            raise ValueError("Concurrent tools must be explicitly read-only")
        if concurrency_group is not None:
            try:
                hash(concurrency_group)
            except TypeError:
                raise ValueError("Tool concurrency group must be hashable") from None
        self.name = name
        self.description = description
        self.arguments_model = arguments_model
        self._execute = execute
        self.read_only = read_only
        self.concurrent_safe = concurrent_safe
        self.concurrency_group = concurrency_group

    async def execute(self, arguments):
        return ToolResult(success=True, output=await self._execute(**arguments), error=None, duration_ms=0)


class ToolRegistry:
    def __init__(self, *, redactor: Redactor | None = None, timeout: float = 30.0,
                 principal: Principal | None = None, policy: PolicyEngine | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.redactor = redactor or Redactor()
        self.timeout = timeout
        self.policy = policy or PolicyEngine()
        self.principal = principal or Principal(Role.ANALYST)
        self.principal_provider = None
        self.repository = None
        self._approved_mcp = False
        self._principal_override = ContextVar("forgepilot_principal", default=None)
        self._capabilities_override = ContextVar("forgepilot_tool_capabilities", default=None)

    @contextmanager
    def as_capabilities(self, capabilities):
        """Narrow the role grant to this AgentDefinition's capabilities."""
        current = self._capabilities_override.get()
        allowed = None if capabilities is None else frozenset(capabilities)
        if current is not None:
            allowed = current if allowed is None else current & allowed
        token = self._capabilities_override.set(allowed)
        try:
            yield
        finally:
            self._capabilities_override.reset(token)

    @contextmanager
    def as_principal(self, principal: Principal):
        """Trusted service boundary; never reachable through tool arguments."""
        token = self._principal_override.set(principal)
        try:
            yield
        finally:
            self._principal_override.reset(token)

    def _principal(self):
        override = self._principal_override.get()
        if override is not None:
            return override
        principal = self.principal_provider() if self.principal_provider else self.principal
        return Principal(principal.role, principal.mcp_approved or self._approved_mcp)

    def register_approved_mcp(self, tool: Tool) -> None:
        """Trusted admission path for a user-selected, reviewed read-only adapter."""
        if tool.mcp_approved is not True or tool.read_only is not True:
            raise ForgePilotError("policy_denied", "MCP selection must be explicitly approved and read-only")
        self.register(tool)
        self._approved_mcp = True

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name) or tool.name in self._tools:
            raise ValueError("Invalid or duplicate tool name")
        if tool.name.casefold() in FORBIDDEN_TOOLS:
            raise ForgePilotError("policy_denied", "Command and Git mutation tools cannot be registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        canonical = LEGACY_ALIASES.get(name, name)
        return next((item for key, item in self._tools.items()
                     if LEGACY_ALIASES.get(key, key) == canonical), None)

    def get_specs(self, *, principal: Principal | None = None) -> list[ToolSpec]:
        specs = {}
        for tool in self._tools.values():
            name = LEGACY_ALIASES.get(tool.name, tool.name)
            if name in VALIDATOR_TOOLS:
                continue  # Deterministic runtime actions are never model tools.
            try:
                self.policy.require_tool(principal or self._principal(), name,
                    mcp_approved=tool.mcp_approved, mcp_read_only=tool.read_only)
                allowed = self._capabilities_override.get()
                capability = TOOL_CAPABILITIES.get(name, "mcp.read" if name.startswith("mcp_") else None)
                if allowed is not None and capability not in allowed:
                    continue
            except ForgePilotError:
                continue
            specs[name] = ToolSpec(name=name, description=tool.description,
                                   input_schema=deepcopy(tool.input_schema))
        return list(specs.values())

    def concurrency_policy(self, name: str) -> tuple[bool, object | None]:
        """Return an explicit safe-parallel decision without executing a tool."""
        tool = self.get(name)
        if tool is None or not (tool.read_only and tool.concurrent_safe):
            return False, None
        return True, tool.concurrency_group

    def validate_call(self, name: str, arguments: dict | str, *, capabilities=None) -> dict:
        """Side-effect-free schema and authority check, also used before batches."""
        tool = self.get(name)
        if tool is None:
            raise ForgePilotError("unknown_tool", "Tool is not registered")
        self.policy.require_tool(self._principal(), name,
            mcp_approved=tool.mcp_approved, mcp_read_only=tool.read_only)
        canonical = LEGACY_ALIASES.get(name, name)
        capability = TOOL_CAPABILITIES.get(canonical, "mcp.read" if name.startswith("mcp_") else None)
        for allowed in (capabilities, self._capabilities_override.get()):
            if allowed is not None and capability not in allowed:
                raise ForgePilotError("policy_denied", "Agent does not declare the required capability")
        try:
            parsed = parse_arguments(arguments)
            validated = tool.validate_arguments(parsed)
        except (ValidationError, ValueError, TypeError, RecursionError, ForgePilotError):
            raise ForgePilotError("invalid_arguments", "Arguments must match the tool schema (strict types, no extra fields)") from None
        # Built-in repository adapters perform the same check at actual access.
        # This hook lets a complete batch be checked before any tool side effect.
        check = getattr(tool, "validate_paths", None)
        if check is not None:
            check(validated)
        if (self.repository is not None and canonical.startswith("repository_")
                and "path" in validated):
            self.repository._path(validated["path"], allow_missing=capability == "repository.patch")
        return validated

    async def execute(self, name: str, arguments: dict | str) -> ToolResult:
        started = perf_counter()
        try:
            validated = self.validate_call(name, arguments)
            tool = self.get(name)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return ToolResult(success=False, output="",
                error="invalid_arguments: Arguments must match the tool schema (strict types, no extra fields)",
                duration_ms=int((perf_counter() - started) * 1_000))
        except ForgePilotError as exc:
            return ToolResult(success=False, output="", error=self.redactor.text(str(exc)),
                              duration_ms=int((perf_counter() - started) * 1_000))
        try:
            async with asyncio.timeout(getattr(tool, "timeout_seconds", self.timeout)):
                result = ToolResult.model_validate(await tool.execute(validated))
            output = self.redactor.text(tool.format_output(result.output, self.redactor))
            if len(output) > 16_424:
                output = output[:16_384] + "\n[TRUNCATED: tool output limit]"
            return result.model_copy(update={"output": output,
                "error": self.redactor.text(result.error)[:4_096] if result.error else None,
                "duration_ms": int((perf_counter() - started) * 1_000)})
        except TimeoutError:
            error = "tool_timeout: Tool execution timed out"
        except ForgePilotError as exc:
            error = self.redactor.text(str(exc))
        except Exception:
            error = "tool_internal: Tool failed unexpectedly"
        # CancelledError intentionally propagates; Runtime owns terminal state.
        return ToolResult(success=False, output="", error=error,
                          duration_ms=int((perf_counter() - started) * 1_000))
