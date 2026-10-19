"""Artifact creation, version dispatch, and cross-artifact validation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping
from uuid import uuid4

from pydantic import ValidationError

from .canonical import canonical_json_bytes, content_sha256
from .models import (
    ARTIFACT_ADAPTER, CONTENT_MODELS, ENVELOPE_MODELS, ArtifactContent,
    ArtifactEnvelope, ArtifactEnvelopeBase, ExecutionPlan, ReviewReport,
    SCHEMA_VERSION, TaskSpec,
)


class ArtifactProtocolError(ValueError):
    pass


class UnknownArtifactType(ArtifactProtocolError):
    pass


class UnsupportedSchemaVersion(ArtifactProtocolError):
    pass


class ArtifactRelationshipError(ArtifactProtocolError):
    pass


def _loads_unique(value: str | bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ArtifactProtocolError(f"Duplicate JSON key: {key}")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique)


def validate_artifact(value: ArtifactEnvelopeBase | Mapping[str, Any] | str | bytes) -> ArtifactEnvelope:
    if isinstance(value, ArtifactEnvelopeBase):
        return ARTIFACT_ADAPTER.validate_python(value)
    data = _loads_unique(value) if isinstance(value, (str, bytes)) else dict(value)
    artifact_type = data.get("artifact_type")
    if artifact_type not in CONTENT_MODELS:
        raise UnknownArtifactType(f"Unsupported artifact_type: {artifact_type!r}")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"Unsupported {artifact_type} schema_version {version!r}; supported: {SCHEMA_VERSION}")
    return ARTIFACT_ADAPTER.validate_json(json.dumps(data, ensure_ascii=False, allow_nan=False))


def create_artifact(
    *, task_id: str, producer: str, producer_node: str, content: ArtifactContent,
    parent_artifact_ids: tuple[str, ...] = (), artifact_id: str | None = None,
    created_at: str | None = None,
) -> ArtifactEnvelope:
    artifact_type = next((name for name, model in CONTENT_MODELS.items() if type(content) is model), None)
    if artifact_type is None:
        raise UnknownArtifactType(f"No registered artifact type for {type(content).__name__}")
    envelope = ENVELOPE_MODELS[artifact_type]
    return envelope(
        artifact_id=artifact_id or str(uuid4()),
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        producer=producer,
        producer_node=producer_node,
        created_at=created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        parent_artifact_ids=parent_artifact_ids,
        content_sha256=content_sha256(content),
        content=content,
    )


def validate_plan_coverage(task_spec: TaskSpec, plan: ExecutionPlan) -> None:
    expected = {item.id for item in task_spec.acceptance_criteria}
    covered = {item for step in plan.steps for item in step.acceptance_criteria_ids}
    missing, unknown = expected - covered, covered - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown)}")
        raise ArtifactRelationshipError("ExecutionPlan acceptance coverage is invalid: " + ", ".join(details))


def validate_review_coverage(task_spec: TaskSpec, review: ReviewReport) -> None:
    expected = {item.id for item in task_spec.acceptance_criteria}
    reviewed = {item.criterion_id for item in review.acceptance_criteria}
    if expected != reviewed:
        raise ArtifactRelationshipError(
            f"ReviewReport criterion coverage differs: missing={sorted(expected-reviewed)}, "
            f"unknown={sorted(reviewed-expected)}")


__all__ = [
    "ArtifactProtocolError", "ArtifactRelationshipError", "UnknownArtifactType",
    "UnsupportedSchemaVersion", "ValidationError", "canonical_json_bytes", "content_sha256",
    "create_artifact", "validate_artifact", "validate_plan_coverage", "validate_review_coverage",
]
