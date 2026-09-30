"""Task data only; no clients, registries, databases or execution handles."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from .planning import TaskPlan


class TaskState(StrEnum):
    CREATED = "created"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    MODIFYING = "modifying"
    BUILDING = "building"
    TESTING = "testing"
    REFLECTING = "reflecting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskContext:
    repository_path: str
    user_request: str
    task_id: str = field(default_factory=lambda: str(uuid4()))
    state: TaskState = TaskState.CREATED
    conversation: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    usage: list[dict | None] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    final_result: str | None = None
    error: str | None = None
    duration_ms: int = 0
    analysis_result: str | None = None
    plan: TaskPlan | None = None
    plan_version: int = 0
    approved_files: set[str] = field(default_factory=set)
    approved_plan_version: int | None = None
    outcome: str | None = None
    validation_errors: list[dict[str, str]] = field(default_factory=list)
    git_baseline: dict | None = None  # Metadata only; raw baseline/snapshot bytes stay private.
    task_diff: dict | None = None
    restore_result: dict | None = None
    build_result: dict | None = None  # Pure data: selection, command evidence and actual CTest counts.
    repair_round: int = 0  # Initial Change is 0; repairs are exactly 1, 2, 3.
    max_repair_rounds: int = 3
    repair_records: list[dict] = field(default_factory=list)
    validation_history: list[dict] = field(default_factory=list)
    last_validation_error: dict | None = None
    failure_kind: str | None = None

    @property
    def token_usage(self) -> dict[str, int | None]:
        """A missing server value makes that aggregate unknown, not zero."""
        return {
            key: sum(u[key] for u in self.usage) if self.usage and all(
                u is not None and type(u.get(key)) is int for u in self.usage
            ) else None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
