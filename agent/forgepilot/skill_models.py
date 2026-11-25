"""Editable local drafts and the version-one distributable skill content schema."""
from __future__ import annotations

from operator import not_
from typing import Annotated, Literal, get_args
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts.models import Sha256Text
from .skill_evidence_ref import SkillEvidenceRef
from .skill_suggestion import EditedLesson, SkillSuggestion


class _SkillModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, str_strip_whitespace=True,
        revalidate_instances="always", hide_input_in_errors=True,
    )


SkillStage = Literal["root_cause_analysis", "impact_test_analysis"]
SKILL_STAGES: tuple[SkillStage, ...] = get_args(SkillStage)
MISSION_SKILL_SELECTION_LIMIT = 2
# The v1 snapshot format is read during replay and through the Mission history API.
FROZEN_SKILL_SELECTION_LIMIT = 2
SkillPhrase = Annotated[str, Field(min_length=1, max_length=256)]
KeywordGroup = Annotated[tuple[SkillPhrase, ...], Field(min_length=2, max_length=8)]


def normalize_match_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _as_tuple(value):
    return tuple(value) if isinstance(value, list) else value


class SkillDraftContent(_SkillModel):
    """An editor can save any subset of the execution fields."""

    schema_version: int = Field(default=1, ge=1, le=1)
    body: str = Field(default="", max_length=2_000)
    stages: tuple[SkillStage, ...] = Field(default=(), max_length=len(SKILL_STAGES))
    triggers: tuple[SkillPhrase, ...] = Field(default=(), max_length=16)
    boundaries: tuple[SkillPhrase, ...] = Field(default=(), max_length=16)
    exclusions: tuple[SkillPhrase, ...] = Field(default=(), max_length=16)
    usage_scope: Literal["local_only", "allowed_by_policy"] = "local_only"
    # Omitting unused extensions preserves the byte identity of existing reviewed
    # versions and the create-request fingerprint of their source drafts.
    summary: str = Field(default="", max_length=256, exclude_if=not_)
    keyword_groups: tuple[KeywordGroup, ...] = Field(default=(), max_length=8, exclude_if=not_)
    project_tags: tuple[SkillPhrase, ...] = Field(default=(), max_length=16, exclude_if=not_)
    task_types: tuple[Literal["bug_fix", "feature"], ...] = Field(default=(), max_length=2, exclude_if=not_)

    _arrays = field_validator("stages", "triggers", "boundaries", "exclusions", "project_tags",
                              "task_types", mode="before")(_as_tuple)

    @field_validator("keyword_groups", mode="before")
    @classmethod
    def read_groups(cls, value):
        return tuple(_as_tuple(group) for group in value) if isinstance(value, (list, tuple)) else value

    @field_validator("keyword_groups")
    @classmethod
    def normalize_groups(cls, groups):
        for group in groups:
            if len({normalize_match_text(term) for term in group}) < len(group):
                raise ValueError("Each keyword in a group must be distinct")
        return tuple(sorted({tuple(sorted(group)) for group in groups}))

    @field_validator("stages", "triggers", "exclusions", "project_tags", "task_types")
    @classmethod
    def normalize_match_sets(cls, values):
        # Matching is independent of stage or phrase order. Keep authored prose
        # and its ordered boundary clauses in the order the developer reviewed.
        return tuple(sorted(set(values)))


class SkillContent(SkillDraftContent):
    """Only this validated payload is serialized and hashed for a version."""

    body: str = Field(min_length=1, max_length=2_000)
    stages: tuple[SkillStage, ...] = Field(min_length=1, max_length=len(SKILL_STAGES))
    boundaries: tuple[SkillPhrase, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def require_match_rule(self):
        if not self.triggers and not self.keyword_groups:
            raise ValueError("A reviewed skill requires a symptom phrase or keyword group")
        return self


class SkillDraftData(_SkillModel):
    """Local editor data. Only name and reviewed content are promoted."""

    name: str = Field(default="", max_length=128)
    content: SkillDraftContent = SkillDraftContent()
    developer_note: str = Field(default="", max_length=4_096)
    pending_inference: str = Field(default="", max_length=4_096, exclude_if=not_)
    evidence_revision: int | None = Field(default=None, ge=1)
    evidence_refs: tuple[SkillEvidenceRef, ...] = Field(default=(), max_length=8)
    edited_lesson: EditedLesson | None = Field(default=None, exclude_if=not_)
    model_suggestion: str = Field(default="", max_length=4_096)
    generation_status: Literal["manual", "generating", "ready", "failed", "interrupted", "cancelled", "timed_out"] = "manual"

    _refs = field_validator("evidence_refs", mode="before")(_as_tuple)

    def reviewed_content(self) -> SkillContent:
        if not self.name:
            raise ValueError("A reviewed skill requires a display name")
        return SkillContent.model_validate(self.content.model_dump())


class SkillGenerationAttempt(_SkillModel):
    attempt_id: str
    draft_id: str
    start_revision: int = Field(ge=1)
    status: Literal["generating", "ready", "failed", "interrupted", "cancelled", "timed_out"]
    template_version: str = Field(min_length=1, max_length=128)
    route: str | None = Field(default=None, max_length=256)
    elapsed_ms: int | None = Field(default=None, ge=0)
    usage: dict[str, Annotated[int, Field(ge=0)]] | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,128}$")
    suggestion: SkillSuggestion | None = None


class SkillDraft(_SkillModel):
    latest_attempt: SkillGenerationAttempt | None = Field(default=None, exclude_if=not_)
    draft_id: str
    source_mission_id: str | None = None
    revision: int = Field(ge=1)
    data: SkillDraftData
    base_skill_id: str | None = None
    base_version: int | None = Field(default=None, ge=1)
    result_skill_id: str | None = None
    result_version: int | None = Field(default=None, ge=1)
    created_at: str
    updated_at: str


class SkillVersion(_SkillModel):
    skill_id: str
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    content: SkillContent
    content_sha256: Sha256Text
    created_at: str


class SelectedSkill(_SkillModel):
    """Exact reviewed identity accepted from the recommendation client."""

    model_config = ConfigDict(str_strip_whitespace=False)
    skill_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$")
    version: int = Field(ge=1, le=2**63 - 2)
    content_sha256: Sha256Text


class SkillIdentity(_SkillModel):
    skill_id: str
    source: Literal["local", "builtin", "team"]
    current_version: int = Field(ge=1)
    enabled: bool
    revision: int = Field(ge=1)
