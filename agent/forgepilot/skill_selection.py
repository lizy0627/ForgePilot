"""Admission and replay of a Mission's reviewed skill content."""
from dataclasses import dataclass
import json
import sqlite3
from typing import Sequence

from .artifacts.canonical import content_sha256
from .errors import ForgePilotError
from .skill_models import SelectedSkill, SkillContent


@dataclass(frozen=True)
class FrozenSkill:
    skill_id: str
    version: int
    content_sha256: str
    content: SkillContent

    def identity(self) -> dict:
        return {"skill_id": self.skill_id, "version": self.version, "content_sha256": self.content_sha256}


def skill_manifest_sha256(skills: Sequence[FrozenSkill]) -> str:
    return content_sha256([skill.identity() for skill in skills])


def skill_reference_payload(skills: Sequence[FrozenSkill], stage: str) -> dict:
    references = [
        {**skill.identity(), "reference_type": "historical_guidance",
         "body": skill.content.body, "boundaries": list(skill.content.boundaries)}
        for skill in skills if stage in skill.content.stages
    ]
    return {
        "skill_references": references,
        "skill_reference_usage": "Historical guidance for investigation only. "
                                "Support every finding with evidence from the current repository.",
    }


def _read_content(row: sqlite3.Row, error_code: str) -> FrozenSkill:
    try:
        content = SkillContent.model_validate_json(row["content_json"])
        if (content_sha256(content) != row["content_sha256"]
                or content_sha256(json.loads(row["content_json"])) != row["content_sha256"]):
            raise ValueError
    except (ValueError, RecursionError):
        raise ForgePilotError(error_code, "Skill content failed schema or SHA-256 verification") from None
    return FrozenSkill(row["skill_id"], row["version"], row["content_sha256"], content)


def resolve_selection(db: sqlite3.Connection, selections) -> tuple[FrozenSkill, ...]:
    if not isinstance(selections, (list, tuple)) or len(selections) > 2:
        raise ForgePilotError("invalid_skill_selection", "Select at most two distinct skills")
    try:
        items = sorted((SelectedSkill.model_validate(s) for s in selections), key=lambda s: s.skill_id)
    except ValueError:
        raise ForgePilotError("invalid_skill_selection", "Expected a skill ID, version and content SHA-256") from None
    if len({s.skill_id for s in items}) != len(items):
        raise ForgePilotError("invalid_skill_selection", "Selected skill IDs must be distinct")

    skills = []
    for item in items:
        row = db.execute("""SELECT v.*,s.enabled,s.current_version FROM skill_versions v
                            JOIN skills s ON s.skill_id=v.skill_id WHERE v.skill_id=? AND v.version=?""",
                         (item.skill_id, item.version)).fetchone()
        if row is None or row["content_sha256"] != item.content_sha256:
            raise ForgePilotError("invalid_skill_selection", "Selected version or content SHA-256 is unknown")
        if not row["enabled"] or row["current_version"] != item.version:
            raise ForgePilotError("skill_selection_changed", "Selected skill was disabled or superseded")
        skills.append(_read_content(row, "invalid_skill_selection"))

    # Reserve a separate UTF-8 input allowance per receiving node. Include
    # boundaries and JSON escaping just as the analysis request serializer does.
    for stage in {stage for skill in skills for stage in skill.content.stages}:
        field = skill_reference_payload(skills, stage)
        if len(json.dumps(field, ensure_ascii=False).encode("utf-8")) > 16_384:
            raise ForgePilotError("skill_budget_exceeded", f"Skill references exceed 16 KiB at {stage}")
    return tuple(skills)


def admit_selection(db: sqlite3.Connection, task_id: str, skills: Sequence[FrozenSkill], stamp: str) -> dict:
    """Called after BEGIN IMMEDIATE. An upgrade/disable cannot interleave with the copy."""
    for position, skill in enumerate(skills):
        inserted = db.execute("""
            INSERT INTO mission_skill_snapshots(mission_id,position,skill_id,version,name,
                                               content_json,content_sha256,usage_scope)
            SELECT ?,?,v.skill_id,v.version,v.name,v.content_json,v.content_sha256,
                   json_extract(v.content_json,'$.usage_scope')
            FROM skills s JOIN skill_versions v ON (s.skill_id,s.current_version)=(v.skill_id,v.version)
            WHERE s.enabled=1 AND v.skill_id=? AND v.version=? AND v.content_sha256=?""",
            (task_id, position, skill.skill_id, skill.version, skill.content_sha256))
        if inserted.rowcount != 1:
            raise ForgePilotError("skill_selection_changed", "Selected skill changed before Mission admission")
        db.execute("UPDATE skills SET use_count=use_count+1,last_used_at=? WHERE skill_id=?", (stamp, skill.skill_id))
    return {"selected_skills": [s.identity() for s in skills], "skill_manifest_sha256": skill_manifest_sha256(skills)}


def load_frozen(db: sqlite3.Connection, task_id: str, metadata: dict, expected_digest: str) -> tuple[FrozenSkill, ...]:
    rows = db.execute("SELECT * FROM mission_skill_snapshots WHERE mission_id=? ORDER BY position",
                      (task_id,)).fetchall()
    skills = tuple(_read_content(row, "skill_snapshot_invalid") for row in rows)
    if (not 1 <= len(skills) <= 2 or expected_digest != skill_manifest_sha256(skills)
            or metadata.get("skill_manifest_sha256") != expected_digest
            or metadata.get("selected_skills") != [skill.identity() for skill in skills]
            or any(row["position"] != index or row["usage_scope"] != skill.content.usage_scope
                   for index, (row, skill) in enumerate(zip(rows, skills)))):
        raise ForgePilotError("skill_snapshot_invalid", "Frozen Mission skill selection is missing or inconsistent")
    return skills
