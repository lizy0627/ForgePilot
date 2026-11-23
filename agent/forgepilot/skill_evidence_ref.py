"""Verifiable local evidence references shared by drafts and model suggestions."""
from operator import not_
from typing import Literal

from pydantic import Field, model_validator

from .artifacts.models import ArtifactModel, Sha256Text


class SkillEvidenceRef(ArtifactModel):
    """Artifact ID, MissionEvent.event_id (task_id:sequence), or graph node ID."""

    kind: Literal["artifact", "event", "node"]
    record_id: str = Field(min_length=1, max_length=300)
    content_sha256: Sha256Text | None = None
    # New manual drafts locate an allowlisted field within immutable artifact
    # content. Omit these extensions for old, whole-record references.
    locator: str | None = Field(default=None, min_length=1, max_length=128, exclude_if=not_)
    classification: Literal["source_context", "developer_observation", "pending_inference",
                            "verified_result"] | None = Field(default=None, exclude_if=not_)
    recorded_value: str | None = Field(default=None, min_length=1, max_length=128, exclude_if=not_)

    @model_validator(mode="after")
    def require_artifact_digest(self):
        if (self.kind == "artifact") != (self.content_sha256 is not None):
            raise ValueError("Only artifact references carry a mandatory content SHA-256")
        if (self.locator is None) != (self.classification is None) or (
                self.locator is not None and self.kind != "artifact"):
            raise ValueError("An item locator and classification require a specific artifact field")
        if (self.recorded_value is not None) != (self.classification == "verified_result"):
            raise ValueError("Only a verified result carries its recorded value")
        return self
