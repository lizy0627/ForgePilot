"""Deterministic matching against the reviewed phrases of current local skills."""
from collections.abc import Sequence
import re

from .skill_models import normalize_match_text
from .skill_store import SkillRepository


def _contains(text: str, phrase: str) -> bool:
    """Use substrings for Chinese and identifier boundaries for English terms."""
    if not phrase or phrase not in text:
        return False
    left = r"(?<![a-z0-9_])" if re.match(r"[a-z0-9_]", phrase[0]) else ""
    right = r"(?![a-z0-9_])" if re.match(r"[a-z0-9_]", phrase[-1]) else ""
    return re.search(left + re.escape(phrase) + right, text) is not None


def recommend_skills(repository: SkillRepository, *, description: str, task_type: str,
                     project_tags: Sequence[str]) -> list[dict]:
    text = normalize_match_text(description)
    available_tags = {normalize_match_text(tag) for tag in project_tags}
    ranked = []
    for skill in repository.list_match_metadata():
        if skill.task_types and task_type != "auto" and task_type not in skill.task_types:
            continue
        if any(_contains(text, normalize_match_text(term)) for term in skill.exclusions):
            continue

        # Count distinct normalized evidence, retaining the reviewed spelling to display.
        symptoms = {}
        for phrase in skill.triggers:
            key = normalize_match_text(phrase)
            if _contains(text, key):
                symptoms.setdefault(key, phrase)
        groups = {}
        for group in skill.keyword_groups:
            keys = tuple(sorted(normalize_match_text(term) for term in group))
            if all(_contains(text, key) for key in keys):
                groups.setdefault(keys, group)
        if not (symptoms or groups):
            continue

        tags = {}
        for tag in skill.project_tags:
            key = normalize_match_text(tag)
            if key in available_tags:
                tags.setdefault(key, tag)
        reasons = [f"命中症状「{phrase}」" for phrase in symptoms.values()]
        for group in groups.values():
            reasons.append("命中关键词组合" + "".join(f"「{term}」" for term in group))
        reasons.extend(f"命中项目标签「{tag}」" for tag in tags.values())
        rank = (-len(symptoms), -len(groups), -len(tags), skill.skill_id)
        ranked.append((rank, {
            "skill_id": skill.skill_id, "version": skill.version, "content_sha256": skill.content_sha256,
            "name": skill.name, "summary": skill.summary, "reasons": reasons,
        }))
    ranked.sort(key=lambda item: item[0])
    return [result for _, result in ranked[:3]]
