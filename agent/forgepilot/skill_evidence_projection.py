"""Allowlisted evidence references for local Mission drafts and model suggestions."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import groupby
from operator import attrgetter

from .artifacts.models import ArtifactContent, ArtifactEnvelope, ChangeSet, EvidenceBundle, TaskSpec, ValidationReport
from .artifacts.store import ArtifactNotFoundError
from .errors import ForgePilotError
from .skill_evidence_ref import SkillEvidenceRef

# Model jobs must be able to verify the entire envelope before projecting any field.
MAX_GENERATION_ARTIFACT_BYTES = 1_048_576


def _evidence_items(content: ArtifactContent, *, include_change_summary: bool):
    """Keep only public validation values in the draft; other fields are pointers."""
    if isinstance(content, ValidationReport):
        yield "/status", "verified_result", content.status
        for i, command in enumerate(content.commands):
            if command.kind in {"build", "configure", "test"}:
                yield f"/commands/{i}/exit_code", "verified_result", str(command.exit_code)
    elif isinstance(content, EvidenceBundle):
        for i, item in enumerate(content.items):
            classification = "developer_observation" if item.source == "user" else "pending_inference"
            yield f"/items/{i}/claim", classification, None
    elif isinstance(content, TaskSpec):
        yield "/objective", "source_context", None
    elif isinstance(content, ChangeSet):
        if include_change_summary:
            yield "/summary", "source_context", None
        for i in range(len(content.files)):
            yield f"/files/{i}/change_type", "source_context", None


def project_artifact_refs(artifact: ArtifactEnvelope, *, include_change_summary: bool) -> Iterator[SkillEvidenceRef]:
    items = _evidence_items(artifact.content, include_change_summary=include_change_summary)
    for locator, classification, value in items:
        yield SkillEvidenceRef(kind="artifact", record_id=artifact.artifact_id, content_sha256=artifact.content_sha256,
                               locator=locator, classification=classification, recorded_value=value)


def read_evidence_artifact(missions, mission_id: str, artifact_id: str) -> ArtifactEnvelope:
    try:
        return missions.owned_artifact(mission_id, artifact_id)
    except (ArtifactNotFoundError, ValueError, ForgePilotError):
        raise ForgePilotError("invalid_skill_evidence", "Source artifact could not be verified") from None


def validate_artifact_ref(artifact: ArtifactEnvelope, ref: SkillEvidenceRef) -> None:
    if artifact.artifact_id != ref.record_id or artifact.content_sha256 != ref.content_sha256:
        raise ForgePilotError("invalid_skill_evidence", "Source artifact does not match the frozen reference")
    # Older drafts can identify a whole artifact without specifying an entry.
    if ref.locator is not None and (ref.locator, ref.classification, ref.recorded_value) not in _evidence_items(
            artifact.content, include_change_summary=True):
        raise ForgePilotError("invalid_skill_evidence", "Referenced artifact entry does not match its record")


def validate_mission_evidence_records(missions, mission_id: str, cutoff_revision: int | None,
                                      refs: tuple[SkillEvidenceRef, ...]) -> None:
    """Check the source and its frozen records without loading artifact bodies."""
    if not missions.allows_evidence_revision(mission_id, cutoff_revision):
        raise ForgePilotError("invalid_skill_evidence", "Evidence revision exceeds the source Mission revision")
    if cutoff_revision is None:
        if refs:
            raise ForgePilotError("invalid_skill_evidence", "Evidence references require a cutoff revision")
        return
    records = {(ref.kind, ref.record_id, ref.content_sha256) for ref in refs}
    for kind, record_id, digest in records:
        if not missions.has_evidence_record(mission_id, kind, record_id, cutoff_revision, content_sha256=digest):
            raise ForgePilotError("invalid_skill_evidence", "Evidence reference does not match a source Mission record")


def validate_mission_artifact_refs(missions, mission_id: str, refs: Iterable[SkillEvidenceRef]) -> None:
    """Read each distinct artifact once to verify its payload and entries."""
    record_id = attrgetter("record_id")
    for artifact_id, group in groupby(sorted(refs, key=record_id), key=record_id):
        artifact = read_evidence_artifact(missions, mission_id, artifact_id)
        for ref in group:
            validate_artifact_ref(artifact, ref)
