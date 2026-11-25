"""Process-level rollback of new skill admissions and extraction."""
import os

from .errors import ForgePilotError


def skill_entries_enabled() -> bool:
    return os.environ.get("FORGEPILOT_DISABLE_SKILL_ENTRIES") != "1"


def require_skill_entries() -> None:
    if not skill_entries_enabled():
        raise ForgePilotError("skill_entries_disabled", "New skill entries are disabled on this installation")
