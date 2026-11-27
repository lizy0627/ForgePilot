"""Freeze Mission evidence at draft creation, inside the caller's write transaction."""
from __future__ import annotations

from itertools import islice, zip_longest

from .errors import ForgePilotError
from .mission_events import TERMINAL_MISSION_STATUSES
from .skill_evidence_projection import MAX_GENERATION_ARTIFACT_BYTES, project_artifact_refs, read_evidence_artifact
from .skill_models import SkillDraft, SkillDraftData


def _freeze_refs(missions, artifacts, mission_id, *, for_generation):
    groups = []
    for kind in ("ValidationReport", "EvidenceBundle", "TaskSpec", "ChangeSet"):
        metadata = artifacts.latest_metadata_for_task(mission_id, kind)
        if metadata is None or (for_generation and metadata.size_bytes > MAX_GENERATION_ARTIFACT_BYTES):
            continue
        try:
            artifact = read_evidence_artifact(missions, mission_id, metadata.artifact_id)
        except ForgePilotError:
            if for_generation:
                # A model attempt can proceed with the remaining verified kinds.
                continue
            raise
        refs = project_artifact_refs(artifact, include_change_summary=for_generation)
        groups.append(list(islice(refs, 8)))
    # Reserve room for each available type before taking secondary entries.
    return tuple(islice((ref for layer in zip_longest(*groups) for ref in layer if ref is not None), 8))


def freeze_mission_draft_data(missions, artifacts, task_id: str, note: SkillDraftData, *,
                             for_generation: bool) -> SkillDraftData:
    if not missions.is_admitted(task_id):
        raise ForgePilotError("unknown_mission", "Mission does not exist")
    run = missions.get_graph_run(task_id)
    if run["status"] not in TERMINAL_MISSION_STATUSES:
        raise ForgePilotError("mission_not_terminal", "A draft requires a terminal Mission")
    refs = () if run["status"] == "skipped" else _freeze_refs(missions, artifacts, task_id, for_generation=for_generation)
    return note.model_copy(update={"evidence_revision": run["current_revision"], "evidence_refs": refs})


def create_mission_draft(store, task_id: str, *, developer_note: str = "",
                         request_id: str | None = None) -> SkillDraft:
    return store.skills.create_mission_draft(task_id, developer_note=developer_note, request_id=request_id)
