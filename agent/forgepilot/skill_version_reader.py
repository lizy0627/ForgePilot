"""Read immutable reviewed skill versions from an existing SQLite connection."""
import sqlite3

from .errors import ForgePilotError
from .skill_models import SkillContent, SkillVersion


def skill_version_from_row(row: sqlite3.Row) -> SkillVersion:
    return SkillVersion(
        skill_id=row["skill_id"], version=row["version"], name=row["name"],
        content=SkillContent.model_validate_json(row["content_json"]),
        content_sha256=row["content_sha256"], created_at=row["created_at"],
    )


def read_skill_version(db: sqlite3.Connection, skill_id: str, version: int) -> SkillVersion:
    row = db.execute("SELECT * FROM skill_versions WHERE skill_id=? AND version=?",
                     (skill_id, version)).fetchone()
    if row is None:
        raise ForgePilotError("skill_not_found", "Skill version does not exist")
    return skill_version_from_row(row)
