"""Frozen, local-only review fixture shared by skill evaluation and protocol tests."""
from pathlib import Path

from forgepilot.skill_models import SkillDraftData, SkillVersion
from forgepilot.skill_store import SkillRepository


QT_SKILL_FILE = Path(__file__).resolve().parents[1] / "reviewed-qt-skill-v1.json"
QT_SKILL_SHA256 = "d408c9d2375c0e929e54f54aba97c85222b538bdbff363cfd64fc9bb5241da4d"


def load_qt_skill() -> SkillDraftData:
    return SkillDraftData.model_validate_json(QT_SKILL_FILE.read_bytes())


def publish_qt_skill(repository: SkillRepository) -> SkillVersion:
    draft = repository.create_draft(request_id="eval-qt-v1", data=load_qt_skill())
    version = repository.publish_local(
        draft.draft_id, expected_revision=draft.revision, request_id="eval-qt-review-v1",
    )
    if (version.version, version.content_sha256) != (1, QT_SKILL_SHA256):
        raise ValueError("Reviewed Qt version no longer matches the locked skill")
    return version
