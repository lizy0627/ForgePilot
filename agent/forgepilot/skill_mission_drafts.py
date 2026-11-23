"""Freeze private evidence from an admitted Mission for a local manual draft."""
from __future__ import annotations

from itertools import groupby, islice, zip_longest
from operator import attrgetter
from uuid import uuid4

from .artifacts.canonical import content_sha256
from .artifacts.models import ChangeSet, EvidenceBundle, TaskSpec, ValidationReport
from .artifacts.store import ArtifactNotFoundError
from .errors import ForgePilotError
from .mission_events import TERMINAL_MISSION_STATUSES
from .skill_models import SkillDraft, SkillDraftData, SkillEvidenceRef

# Model jobs verify entire immutable envelopes before projecting allowed fields.
# Skip oversize records before loading a diff or an external payload.
MAX_GENERATION_ARTIFACT_BYTES = 1_048_576


def _evidence_items(content, *, change_summary=True):
    """Only public validation values can become recorded results.

    Other material stays in its private artifact. A pointer identifies the
    exact item without copying paths, patches, prompts or diagnostic output.
    """
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
        if change_summary:
            yield "/summary", "source_context", None
        for i in range(len(content.files)):
            yield f"/files/{i}/change_type", "source_context", None


def _owned_artifact(missions, mission_id, artifact_id):
    try:
        return missions.owned_artifact(mission_id, artifact_id)
    except (ArtifactNotFoundError, ValueError, ForgePilotError):
        raise ForgePilotError("invalid_skill_evidence", "Source artifact could not be verified") from None


def validate_mission_artifact_refs(missions, mission_id: str, refs) -> None:
    """Read each distinct artifact once to verify ownership, digest and entries."""
    record_id = attrgetter("record_id")
    for artifact_id, group in groupby(sorted(refs, key=record_id), key=record_id):
        artifact = _owned_artifact(missions, mission_id, artifact_id)
        for ref in group:
            if artifact.content_sha256 != ref.content_sha256:
                raise ForgePilotError("invalid_skill_evidence", "Source artifact digest changed")
            if ref.locator is not None and (
                    ref.locator, ref.classification, ref.recorded_value) not in _evidence_items(artifact.content):
                raise ForgePilotError("invalid_skill_evidence", "Referenced artifact entry does not match its record")


def _freeze_refs(db, missions, mission_id, *, bounded=False):
    groups = []
    # Take the latest stored artifact of each relevant type. All reads share
    # the write transaction which pins both the Mission revision and the set
    # of existing artifacts (including those stored before admission).
    for kind in ("ValidationReport", "EvidenceBundle", "TaskSpec", "ChangeSet"):
        row = db.execute("""SELECT artifact_id,size_bytes FROM artifacts WHERE task_id=? AND artifact_type=?
                            ORDER BY rowid DESC LIMIT 1""", (mission_id, kind)).fetchone()
        if row is None or (bounded and row["size_bytes"] > MAX_GENERATION_ARTIFACT_BYTES):
            continue
        try:
            artifact = _owned_artifact(missions, mission_id, row["artifact_id"])
        except ForgePilotError:
            if bounded:
                # Preserve the new local note. The job can use the remaining
                # verified kinds or finish with insufficient evidence.
                continue
            raise
        groups.append([
            SkillEvidenceRef(kind="artifact", record_id=artifact.artifact_id,
                             content_sha256=artifact.content_sha256, locator=locator,
                             classification=classification, recorded_value=value)
            for locator, classification, value in islice(_evidence_items(artifact.content, change_summary=bounded), 8)
        ])
    # Reserve room for each available type before taking secondary entries.
    return tuple(islice((ref for layer in zip_longest(*groups) for ref in layer if ref is not None), 8))


def create_mission_draft(store, task_id: str, *, developer_note: str = "",
                         request_id: str | None = None) -> SkillDraft:
    request_id = str(uuid4()) if request_id is None else request_id
    with store.transaction() as db:
        return create_mission_draft_locked(store, db, task_id, developer_note, request_id, "manual_mission_draft")


def create_mission_draft_locked(store, db, task_id, developer_note, request_id, operation):
    note = SkillDraftData(developer_note=developer_note)
    # Replaying the original intent must succeed even after edits, a new
    # artifact, or a later change of the Mission's state/revision.
    fingerprint = content_sha256({"operation": operation, "mission_id": task_id,
                                  "developer_note": note.developer_note})
    bounded = operation == "model_mission_draft"
    existing = store.skills._replayed_draft(db, request_id, fingerprint, verify_artifacts=not bounded)
    if existing is not None:
        return existing
    if not store.missions.is_admitted(task_id):
        raise ForgePilotError("unknown_mission", "Mission does not exist")
    run = store.missions.get_graph_run(task_id)
    if run["status"] not in TERMINAL_MISSION_STATUSES:
        raise ForgePilotError("mission_not_terminal", "A draft requires a terminal Mission")
    refs = () if run["status"] == "skipped" else _freeze_refs(
        db, store.missions, task_id, bounded=bounded)
    data = note.model_copy(update={"evidence_revision": run["current_revision"], "evidence_refs": refs})
    return store.skills._insert_draft(db, request_id, data, source_mission_id=task_id, create_hash=fingerprint)
