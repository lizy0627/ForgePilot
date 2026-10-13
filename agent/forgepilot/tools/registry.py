"""A small typed tool registry; schemas and validation share one source."""

from abc import ABC, abstractmethod
import asyncio
from copy import deepcopy
import json
import re
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ForgePilotError
from ..security import Redactor
from ..llm import ToolSpec


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
    def __init__(self, *, redactor: Redactor | None = None, timeout: float = 30.0) -> None:
        self._tools: dict[str, Tool] = {}
        self.redactor = redactor or Redactor()
        self.timeout = timeout

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name) or tool.name in self._tools:
            raise ValueError("Invalid or duplicate tool name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=t.name, description=t.description, input_schema=deepcopy(t.input_schema))
                for t in self._tools.values()]

    def concurrency_policy(self, name: str) -> tuple[bool, object | None]:
        """Return an explicit safe-parallel decision without executing a tool."""
        tool = self.get(name)
        if tool is None or not (tool.read_only and tool.concurrent_safe):
            return False, None
        return True, tool.concurrency_group

    async def execute(self, name: str, arguments: dict | str) -> ToolResult:
        started = perf_counter()
        try:
            tool = self.get(name)
            if tool is None:
                raise ForgePilotError("unknown_tool", "Tool is not registered")
            if isinstance(arguments, str):
                if len(arguments) > 16_384:
                    raise ForgePilotError("invalid_arguments", "Arguments exceed 16384 characters")
                arguments = json.loads(arguments)
            validated = tool.validate_arguments(arguments)
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
            output = tool.format_output(result.output, self.redactor)
            return result.model_copy(update={"output": output,
                "error": self.redactor.text(result.error) if result.error else None,
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
