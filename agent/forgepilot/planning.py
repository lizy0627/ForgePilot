"""Validated immutable plan data. No model-supplied authorization fields."""

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ForgePilotError

Text = Annotated[str, Field(min_length=1, max_length=4_096)]


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True,
                              revalidate_instances="always")
    path: str = Field(min_length=1, max_length=1_024)
    reason: Text

    @field_validator("path", mode="before")
    @classmethod
    def unambiguous_file_path(cls, value):
        if isinstance(value, str) and (value != value.strip() or value.endswith(("/", "\\"))):
            raise ValueError("Path must name a file without leading/trailing whitespace or a trailing separator")
        return value


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True,
                              revalidate_instances="always")
    summary: Text
    root_cause: Text | None
    files_to_modify: tuple[FileChange, ...] = Field(max_length=100)
    implementation_steps: tuple[Text, ...] = Field(min_length=1, max_length=32)
    validation_steps: tuple[Text, ...] = Field(min_length=1, max_length=32)
    risks: tuple[Text, ...] = Field(max_length=32)

    @field_validator("files_to_modify", "implementation_steps", "validation_steps", "risks", mode="before")
    @classmethod
    def immutable_arrays(cls, value):
        # JSON arrays are immutable tuples internally; strings/maps are not coerced.
        return tuple(value) if isinstance(value, list) else value


class PlanValidationError(ForgePilotError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("invalid_plan", "; ".join(f"{e['field']}: {e['message']}" for e in errors))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def validate_plan(value: str | dict, normalize_path: Callable[[str], str]) -> TaskPlan:
    try:
        if isinstance(value, dict) and len(json.dumps(value, ensure_ascii=False)) > 32_768:
            raise ValueError("Plan too large")
        if isinstance(value, str):
            if len(value) > 32_768:
                raise ValueError("Plan too large")
            value = json.loads(value, object_pairs_hook=_unique_object)
        plan = TaskPlan.model_validate(value)
    except ValidationError as exc:
        raise PlanValidationError([
            {"field": ".".join(map(str, error["loc"])) or "plan",
             "type": error["type"], "message": error["msg"]}
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        ]) from None
    except (ValueError, TypeError, RecursionError):
        raise PlanValidationError([{"field": "plan", "type": "json_invalid",
                                    "message": "Expected one JSON object (no fences/duplicate keys), at most 32768 characters"}]) from None
    normalized = []
    seen = set()
    errors = []
    for index, change in enumerate(plan.files_to_modify):
        try:
            path = normalize_path(change.path)
            if path.casefold() in seen:
                raise ForgePilotError("duplicate_path", "Duplicate file after Windows path normalization")
            seen.add(path.casefold())
            normalized.append({"path": path, "reason": change.reason})
        except ForgePilotError as exc:
            errors.append({"field": f"files_to_modify.{index}.path", "type": exc.code, "message": exc.message})
    if errors:
        raise PlanValidationError(errors)
    return TaskPlan.model_validate({**plan.model_dump(mode="json"), "files_to_modify": normalized})


@dataclass(frozen=True)
class PlanReview:
    """Program-owned task/version binding, not a field accepted from the LLM."""
    task_id: str
    repository_path: str
    version: int
    plan: TaskPlan
