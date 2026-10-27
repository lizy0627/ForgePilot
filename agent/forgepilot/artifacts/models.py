"""Version 1 Artifact content models and their immutable envelopes."""
from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .canonical import content_sha256
from ..path_identity import path_key

SCHEMA_VERSION = "1.0"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
CRITERION_ID_PATTERN = r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$"
STEP_ID_PATTERN = r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$"

UuidText = Annotated[str, Field(pattern=UUID_PATTERN)]
Sha256Text = Annotated[str, Field(pattern=SHA256_PATTERN)]
ShortText = Annotated[str, Field(min_length=1, max_length=512)]
Text = Annotated[str, Field(min_length=1, max_length=8_192)]
ArtifactTypeName: TypeAlias = Literal[
    "TaskSpec", "EvidenceBundle", "ExecutionPlan", "ChangeSet",
    "ValidationReport", "ReviewReport", "DeliveryBundle",
]
VerificationMethod: TypeAlias = Literal[
    "build", "test", "ctest", "protected_test", "static_analysis", "inspection", "manual",
]


class ArtifactModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, str_strip_whitespace=True,
        revalidate_instances="always", hide_input_in_errors=True,
    )


def _tuple(value):
    return tuple(value) if isinstance(value, list) else value


def _unique(values, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _validate_iso8601(value: str) -> str:
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Expected an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return value


class RepositorySpec(ArtifactModel):
    base_ref: ShortText
    toolchain_profile: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]


class TaskProblem(ArtifactModel):
    observed_behavior: Text
    expected_behavior: Text
    reproduction_steps: tuple[ShortText, ...] = Field(default=(), max_length=32)

    _arrays = field_validator("reproduction_steps", mode="before")(_tuple)


class AcceptanceCriterion(ArtifactModel):
    id: Annotated[str, Field(pattern=CRITERION_ID_PATTERN, max_length=32)]
    description: Text
    verification: VerificationMethod


class AllowedScope(ArtifactModel):
    path_patterns: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(min_length=1, max_length=64)
    max_changed_files: int = Field(ge=1, le=1_000)

    _arrays = field_validator("path_patterns", mode="before")(_tuple)


class TestPolicy(ArtifactModel):
    existing_tests_read_only: bool = True
    allow_new_tests: bool = True
    modifiable_existing_test_patterns: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(default=(), max_length=64)

    _arrays = field_validator("modifiable_existing_test_patterns", mode="before")(_tuple)


class DataPolicy(ArtifactModel):
    cloud_model_allowed: bool = False
    external_mcp_allowed: bool = False


class TaskSpec(ArtifactModel):
    task_type: Literal["bug_fix", "feature", "maintenance"]
    title: ShortText
    objective: Text
    repository: RepositorySpec
    problem: TaskProblem
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=64)
    allowed_scope: AllowedScope
    forbidden_scope: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(default=(), max_length=64)
    test_policy: TestPolicy = TestPolicy()
    data_policy: DataPolicy = DataPolicy()
    assumptions: tuple[Text, ...] = Field(default=(), max_length=32)
    risk_level: Literal["low", "medium", "high", "critical"]

    _arrays = field_validator("acceptance_criteria", "forbidden_scope", "assumptions", mode="before")(_tuple)

    @model_validator(mode="after")
    def unique_criteria(self):
        _unique(tuple(item.id for item in self.acceptance_criteria), "Acceptance criterion IDs")
        return self


class EvidenceItem(ArtifactModel):
    evidence_id: Annotated[str, Field(pattern=r"^EV-[1-9][0-9]*$", max_length=32)]
    claim: Text
    path: Annotated[str, Field(min_length=1, max_length=1_024)] | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    file_hash: Sha256Text | None = None
    symbol: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    source: Literal["local_repository", "rag", "user", "approved_mcp", "tool_output"]
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    reason: Text

    @model_validator(mode="after")
    def valid_location(self):
        located = (self.path, self.start_line, self.end_line, self.file_hash)
        if any(value is not None for value in located) and self.path is None:
            raise ValueError("Located evidence requires path")
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if self.start_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class EvidenceBundle(ArtifactModel):
    items: tuple[EvidenceItem, ...] = Field(min_length=1, max_length=256)
    summary: Text

    _arrays = field_validator("items", mode="before")(_tuple)

    @model_validator(mode="after")
    def unique_evidence(self):
        _unique(tuple(item.evidence_id for item in self.items), "Evidence IDs")
        return self


class PlanStep(ArtifactModel):
    step_id: Annotated[str, Field(pattern=STEP_ID_PATTERN, max_length=32)]
    owner: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    depends_on: tuple[Annotated[str, Field(pattern=STEP_ID_PATTERN, max_length=32)], ...] = Field(default=(), max_length=32)
    expected_files: tuple[Annotated[str, Field(min_length=1, max_length=1_024)], ...] = Field(default=(), max_length=128)
    action: Text
    verification: tuple[VerificationMethod, ...] = Field(min_length=1, max_length=16)
    risk: Text
    adds_tests: bool
    acceptance_criteria_ids: tuple[Annotated[str, Field(pattern=CRITERION_ID_PATTERN, max_length=32)], ...] = Field(min_length=1, max_length=64)

    _arrays = field_validator("depends_on", "expected_files", "verification", "acceptance_criteria_ids", mode="before")(_tuple)

    @model_validator(mode="after")
    def unique_references(self):
        _unique(self.depends_on, "Step dependencies")
        _unique(self.expected_files, "Expected files")
        _unique(self.verification, "Verification methods")
        _unique(self.acceptance_criteria_ids, "Acceptance criterion references")
        if self.step_id in self.depends_on:
            raise ValueError("A step cannot depend on itself")
        return self


class ExecutionPlan(ArtifactModel):
    task_spec_artifact_id: UuidText
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=128)
    summary: Text

    _arrays = field_validator("steps", mode="before")(_tuple)

    @model_validator(mode="after")
    def valid_graph(self):
        ids = tuple(step.step_id for step in self.steps)
        _unique(ids, "Plan step IDs")
        known = set(ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"Unknown step dependencies: {sorted(unknown)}")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {step.step_id: step.depends_on for step in self.steps}

        def visit(step_id: str):
            if step_id in visiting:
                raise ValueError("Plan dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        return self


class ChangedFile(ArtifactModel):
    path: Annotated[str, Field(min_length=1, max_length=1_024)]
    change_type: Literal["added", "modified", "deleted"]
    unified_diff: Annotated[str, Field(min_length=1, max_length=1_048_576)]
    before_sha256: Sha256Text | None = None
    after_sha256: Sha256Text | None = None
    plan_step_ids: tuple[Annotated[str, Field(pattern=STEP_ID_PATTERN, max_length=32)], ...] = Field(min_length=1, max_length=64)
    acceptance_criteria_ids: tuple[Annotated[str, Field(pattern=CRITERION_ID_PATTERN, max_length=32)], ...] = Field(min_length=1, max_length=64)
    test_change: Literal["none", "new_test", "existing_test", "production_and_test"] = "none"

    _arrays = field_validator("plan_step_ids", "acceptance_criteria_ids", mode="before")(_tuple)

    @model_validator(mode="after")
    def hashes_match_change(self):
        if self.change_type == "added" and (self.before_sha256 is not None or self.after_sha256 is None):
            raise ValueError("Added files require only after_sha256")
        if self.change_type == "deleted" and (self.before_sha256 is None or self.after_sha256 is not None):
            raise ValueError("Deleted files require only before_sha256")
        if self.change_type == "modified" and (self.before_sha256 is None or self.after_sha256 is None):
            raise ValueError("Modified files require both hashes")
        _unique(self.plan_step_ids, "Plan step references")
        _unique(self.acceptance_criteria_ids, "Acceptance criterion references")
        return self


class ChangeSet(ArtifactModel):
    execution_plan_artifact_id: UuidText
    files: tuple[ChangedFile, ...] = Field(min_length=1, max_length=1_000)
    requires_additional_approval: bool = False
    summary: Text

    _arrays = field_validator("files", mode="before")(_tuple)

    @model_validator(mode="after")
    def unique_paths(self):
        folded = tuple(path_key(item.path) for item in self.files)
        _unique(folded, "Changed file paths")
        return self


class CommandResult(ArtifactModel):
    kind: Literal["configure", "build", "test", "protected_test", "integrity", "custom"]
    command_summary: ShortText
    started_at: Annotated[str, Field(max_length=64)]
    finished_at: Annotated[str, Field(max_length=64)]
    exit_code: int = Field(ge=-2_147_483_648, le=2_147_483_647)
    output_summary: Annotated[str, Field(max_length=8_192)] = ""

    _timestamps = field_validator("started_at", "finished_at")(_validate_iso8601)


class ValidationReport(ArtifactModel):
    toolchain_profile: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    commands: tuple[CommandResult, ...] = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "cancelled", "timed_out"]
    compile_errors: tuple[Text, ...] = Field(default=(), max_length=128)
    tests_passed: int = Field(ge=0)
    tests_failed: int = Field(ge=0)
    hidden_oracle: Literal["not_run", "passed", "failed"] = "not_run"
    test_integrity: Literal["not_checked", "passed", "failed"] = "not_checked"
    failure_classification: Literal[
        "none", "compile", "test", "timeout", "policy", "infrastructure", "cancelled", "unknown"
    ] = "none"
    log_artifact_ids: tuple[UuidText, ...] = Field(default=(), max_length=128)
    summary: Text

    _arrays = field_validator("commands", "compile_errors", "log_artifact_ids", mode="before")(_tuple)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "passed" and (self.tests_failed or any(item.exit_code for item in self.commands)):
            raise ValueError("Passed validation cannot contain failed commands or tests")
        if self.status == "passed" and self.failure_classification != "none":
            raise ValueError("Passed validation cannot have a failure classification")
        if self.status != "passed" and self.failure_classification == "none":
            raise ValueError("Non-passing validation requires a failure classification")
        return self


class CriterionReview(ArtifactModel):
    criterion_id: Annotated[str, Field(pattern=CRITERION_ID_PATTERN, max_length=32)]
    status: Literal["met", "not_met", "not_verified"]
    evidence: Text


class ReviewIssue(ArtifactModel):
    issue_id: Annotated[str, Field(pattern=r"^RI-[1-9][0-9]*$", max_length=32)]
    description: Text
    path: Annotated[str, Field(min_length=1, max_length=1_024)] | None = None


class ReviewReport(ArtifactModel):
    verdict: Literal["approve", "request_changes", "reject"]
    acceptance_criteria: tuple[CriterionReview, ...] = Field(min_length=1, max_length=64)
    blocking_issues: tuple[ReviewIssue, ...] = Field(default=(), max_length=128)
    non_blocking_suggestions: tuple[Text, ...] = Field(default=(), max_length=128)
    test_review: Text
    security_review: Text
    recommend_delivery: bool
    summary: Text

    _arrays = field_validator("acceptance_criteria", "blocking_issues", "non_blocking_suggestions", mode="before")(_tuple)

    @model_validator(mode="after")
    def consistent_recommendation(self):
        _unique(tuple(item.criterion_id for item in self.acceptance_criteria), "Reviewed criterion IDs")
        _unique(tuple(item.issue_id for item in self.blocking_issues), "Review issue IDs")
        if self.recommend_delivery and (self.verdict != "approve" or self.blocking_issues):
            raise ValueError("Delivery can only be recommended with approval and no blocking issues")
        return self


class DeliveryBundle(ArtifactModel):
    change_set_artifact_id: UuidText
    validation_report_artifact_id: UuidText
    review_report_artifact_id: UuidText
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")] | None = None
    build_test_summary: Text
    reviewer_conclusion: Text
    changed_files: tuple[Annotated[str, Field(min_length=1, max_length=1_024)], ...] = Field(min_length=1, max_length=1_000)
    usage_instructions: Text
    risks: tuple[Text, ...] = Field(default=(), max_length=128)
    rollback: Text
    patch_path: Annotated[str, Field(min_length=1, max_length=1_024)] | None = None

    _arrays = field_validator("changed_files", "risks", mode="before")(_tuple)

    @model_validator(mode="after")
    def unique_files(self):
        _unique(tuple(path_key(path) for path in self.changed_files), "Delivery file paths")
        return self


ArtifactContent: TypeAlias = TaskSpec | EvidenceBundle | ExecutionPlan | ChangeSet | ValidationReport | ReviewReport | DeliveryBundle


class ArtifactEnvelopeBase(ArtifactModel):
    artifact_id: UuidText
    artifact_type: ArtifactTypeName
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: UuidText
    producer: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    producer_node: Annotated[str, Field(pattern=IDENTIFIER_PATTERN, max_length=128)]
    created_at: Annotated[str, Field(max_length=64)]
    parent_artifact_ids: tuple[UuidText, ...] = Field(default=(), max_length=256)
    content_sha256: Sha256Text
    content: ArtifactContent

    _arrays = field_validator("parent_artifact_ids", mode="before")(_tuple)
    _timestamp = field_validator("created_at")(_validate_iso8601)

    @model_validator(mode="after")
    def valid_envelope(self):
        _unique(self.parent_artifact_ids, "Parent Artifact IDs")
        if self.artifact_id in self.parent_artifact_ids:
            raise ValueError("Artifact cannot be its own parent")
        if content_sha256(self.content) != self.content_sha256:
            raise ValueError("content_sha256 does not match canonical content")
        return self


class TaskSpecArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["TaskSpec"] = "TaskSpec"
    content: TaskSpec


class EvidenceBundleArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["EvidenceBundle"] = "EvidenceBundle"
    content: EvidenceBundle


class ExecutionPlanArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["ExecutionPlan"] = "ExecutionPlan"
    content: ExecutionPlan


class ChangeSetArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["ChangeSet"] = "ChangeSet"
    content: ChangeSet


class ValidationReportArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["ValidationReport"] = "ValidationReport"
    content: ValidationReport


class ReviewReportArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["ReviewReport"] = "ReviewReport"
    content: ReviewReport


class DeliveryBundleArtifact(ArtifactEnvelopeBase):
    artifact_type: Literal["DeliveryBundle"] = "DeliveryBundle"
    content: DeliveryBundle


ArtifactEnvelope: TypeAlias = Annotated[
    TaskSpecArtifact | EvidenceBundleArtifact | ExecutionPlanArtifact | ChangeSetArtifact |
    ValidationReportArtifact | ReviewReportArtifact | DeliveryBundleArtifact,
    Field(discriminator="artifact_type"),
]
ARTIFACT_ADAPTER = TypeAdapter(ArtifactEnvelope)

CONTENT_MODELS: dict[str, type[ArtifactModel]] = {
    "TaskSpec": TaskSpec,
    "EvidenceBundle": EvidenceBundle,
    "ExecutionPlan": ExecutionPlan,
    "ChangeSet": ChangeSet,
    "ValidationReport": ValidationReport,
    "ReviewReport": ReviewReport,
    "DeliveryBundle": DeliveryBundle,
}
ENVELOPE_MODELS: dict[str, type[ArtifactEnvelopeBase]] = {
    "TaskSpec": TaskSpecArtifact,
    "EvidenceBundle": EvidenceBundleArtifact,
    "ExecutionPlan": ExecutionPlanArtifact,
    "ChangeSet": ChangeSetArtifact,
    "ValidationReport": ValidationReportArtifact,
    "ReviewReport": ReviewReportArtifact,
    "DeliveryBundle": DeliveryBundleArtifact,
}
