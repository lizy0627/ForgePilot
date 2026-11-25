"""Pause new skill admissions and extraction for this installation or store."""
import os

from .errors import ForgePilotError


def skill_entries_enabled(store) -> bool:
    return store.builtin_skill_error is None and os.environ.get("FORGEPILOT_DISABLE_SKILL_ENTRIES") != "1"


def require_skill_entries(store) -> None:
    if not skill_entries_enabled(store):
        raise ForgePilotError("skill_entries_disabled", "New skill entries are disabled on this installation")
