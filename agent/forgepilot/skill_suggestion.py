"""Model-generated lesson fields and their locally resolved conclusion citations."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .skill_evidence_ref import SkillEvidenceRef


class _SuggestionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True,
                              hide_input_in_errors=True)


Section = Annotated[str, Field(min_length=1, max_length=512)]
EvidenceKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.:-]+$")]


class LessonFields(_SuggestionModel):
    phenomenon: Section
    priority_checks: Section
    decision_branches: Section
    stop_conditions: Section
    boundaries: Section
    conclusion: Section


class ModelLesson(LessonFields):
    evidence_keys: list[EvidenceKey] = Field(max_length=8)


class EditedLesson(LessonFields):
    """Private editor copy. The attempt retains the original citation receipts."""

    attempt_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    basis_note: str = Field(default="", max_length=1_024)


class SuggestionBasis(_SuggestionModel):
    key: EvidenceKey | None
    classification: Literal["source_context", "developer_observation", "pending_inference", "verified_result"]
    reference: SkillEvidenceRef | None

    @model_validator(mode="after")
    def match_record(self):
        if self.reference is None:
            if self.classification != "pending_inference":
                raise ValueError("Unresolved evidence is pending confirmation")
        elif self.key is None or self.reference.classification != self.classification:
            raise ValueError("Resolved evidence must match the referenced field")
        return self


class SkillSuggestion(LessonFields):
    basis: list[SuggestionBasis] = Field(min_length=1, max_length=8)
