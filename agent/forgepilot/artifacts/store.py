"""Storage boundary and an immutable in-memory reference implementation."""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Iterable, Literal, Protocol

from .models import (
    ArtifactContent, ArtifactEnvelope, ArtifactEnvelopeBase, ChangeSet, DeliveryBundle,
    ExecutionPlan, ReviewReport, TaskSpec, ValidationReport,
)
from .validation import ArtifactProtocolError, ArtifactRelationshipError, create_artifact, validate_artifact, validate_plan_coverage, validate_review_coverage


class ArtifactIntegrityError(ArtifactProtocolError):
    pass


class ArtifactStorageError(ArtifactProtocolError):
    pass


class ArtifactNotFoundError(KeyError):
    pass


class DuplicateArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactRelation:
    parent_artifact_id: str
    child_artifact_id: str
    relation: Literal["derived_from", "revision_of"]


def validate_relationships(artifact: ArtifactEnvelope, parents: Iterable[ArtifactEnvelope]) -> tuple[ArtifactEnvelope, ...]:
    """Validate immutable protocol relationships for every store implementation."""
    parents = tuple(parents)
    for parent in parents:
        if parent.task_id != artifact.task_id:
            raise ArtifactRelationshipError("Parent and child artifacts must belong to the same task")
    if isinstance(artifact.content, ExecutionPlan):
        specs = [parent for parent in parents if isinstance(parent.content, TaskSpec)]
        if len(specs) != 1 or artifact.content.task_spec_artifact_id != specs[0].artifact_id:
            raise ArtifactRelationshipError("ExecutionPlan requires its declared TaskSpec as one parent")
        validate_plan_coverage(specs[0].content, artifact.content)
    if isinstance(artifact.content, ChangeSet):
        plans = [parent for parent in parents if isinstance(parent.content, ExecutionPlan)]
        if len(plans) != 1 or artifact.content.execution_plan_artifact_id != plans[0].artifact_id:
            raise ArtifactRelationshipError("ChangeSet requires its declared ExecutionPlan as one parent")
        step_ids = {step.step_id for step in plans[0].content.steps}
        criterion_ids = {
            criterion_id for step in plans[0].content.steps
            for criterion_id in step.acceptance_criteria_ids
        }
        for changed_file in artifact.content.files:
            if not set(changed_file.plan_step_ids) <= step_ids:
                raise ArtifactRelationshipError("ChangeSet references an unknown plan step")
            if not set(changed_file.acceptance_criteria_ids) <= criterion_ids:
                raise ArtifactRelationshipError("ChangeSet references an unknown acceptance criterion")
    if isinstance(artifact.content, ReviewReport):
        specs = [parent for parent in parents if isinstance(parent.content, TaskSpec)]
        if len(specs) != 1:
            raise ArtifactRelationshipError("ReviewReport requires exactly one TaskSpec parent")
        validate_review_coverage(specs[0].content, artifact.content)
    if isinstance(artifact.content, DeliveryBundle):
        declared = {
            artifact.content.change_set_artifact_id: ChangeSet,
            artifact.content.validation_report_artifact_id: ValidationReport,
            artifact.content.review_report_artifact_id: ReviewReport,
        }
        parent_by_id = {parent.artifact_id: parent for parent in parents}
        if set(declared) - set(parent_by_id):
            raise ArtifactRelationshipError("DeliveryBundle requires all declared reports as parents")
        for parent_id, content_type in declared.items():
            if not isinstance(parent_by_id[parent_id].content, content_type):
                raise ArtifactRelationshipError("DeliveryBundle parent type does not match its declared reference")
    return parents


class ArtifactStore(Protocol):
    def put(self, artifact: ArtifactEnvelopeBase) -> None: ...
    def get(self, artifact_id: str) -> ArtifactEnvelope: ...
    def relations_from(self, artifact_id: str) -> tuple[ArtifactRelation, ...]: ...
    def revise(self, artifact_id: str, *, content: ArtifactContent, producer: str,
               producer_node: str, artifact_id_override: str | None = None) -> ArtifactEnvelope: ...


class InMemoryArtifactStore:
    """Reference semantics for a durable store; artifacts never expose mutation."""

    def __init__(self):
        self._artifacts: dict[str, ArtifactEnvelope] = {}
        self._relations: list[ArtifactRelation] = []
        self._lock = RLock()

    def put(self, artifact: ArtifactEnvelopeBase) -> None:
        artifact = validate_artifact(artifact)
        with self._lock:
            if artifact.artifact_id in self._artifacts:
                raise DuplicateArtifactError(f"Artifact already exists: {artifact.artifact_id}")
            parents = []
            for parent_id in artifact.parent_artifact_ids:
                parent = self._artifacts.get(parent_id)
                if parent is None:
                    raise ArtifactNotFoundError(parent_id)
                parents.append(parent)
            validate_relationships(artifact, parents)
            self._artifacts[artifact.artifact_id] = artifact
            self._relations.extend(ArtifactRelation(parent.artifact_id, artifact.artifact_id, "derived_from") for parent in parents)

    def get(self, artifact_id: str) -> ArtifactEnvelope:
        with self._lock:
            try:
                return self._artifacts[artifact_id]
            except KeyError:
                raise ArtifactNotFoundError(artifact_id) from None

    def relations_from(self, artifact_id: str) -> tuple[ArtifactRelation, ...]:
        with self._lock:
            if artifact_id not in self._artifacts:
                raise ArtifactNotFoundError(artifact_id)
            return tuple(item for item in self._relations if item.parent_artifact_id == artifact_id)

    def parents_of(self, artifact_id: str) -> tuple[ArtifactRelation, ...]:
        with self._lock:
            if artifact_id not in self._artifacts:
                raise ArtifactNotFoundError(artifact_id)
            return tuple(item for item in self._relations if item.child_artifact_id == artifact_id)

    def is_referenced(self, artifact_id: str) -> bool:
        return bool(self.relations_from(artifact_id))

    def revise(self, artifact_id: str, *, content: ArtifactContent, producer: str,
               producer_node: str, artifact_id_override: str | None = None) -> ArtifactEnvelope:
        original = self.get(artifact_id)
        if type(content) is not type(original.content):
            raise ArtifactRelationshipError("A revision must preserve artifact_type")
        revision = create_artifact(
            artifact_id=artifact_id_override,
            task_id=original.task_id,
            producer=producer,
            producer_node=producer_node,
            parent_artifact_ids=(original.artifact_id,),
            content=content,
        )
        self.put(revision)
        with self._lock:
            self._relations = [
                ArtifactRelation(item.parent_artifact_id, item.child_artifact_id, "revision_of")
                if item.parent_artifact_id == original.artifact_id and item.child_artifact_id == revision.artifact_id
                else item for item in self._relations
            ]
        return revision
