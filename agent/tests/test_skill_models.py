from hashlib import sha256

import pytest
from pydantic import ValidationError

from forgepilot.artifacts.canonical import canonical_json_bytes, content_sha256
from forgepilot.skill_models import SkillContent, SkillDraftData, SkillEvidenceRef


@pytest.fixture
def content_fields():
    return {
        "body": "Compare the saved value with the model row.",
        "stages": ["root_cause_analysis", "impact_test_analysis"],
        "triggers": ["列表未更新", "stale list"],
        "boundaries": ["Inspect only after a successful save."],
        "exclusions": ["duplicate signal"],
    }


def test_skill_hash_uses_canonical_json_and_normalized_match_sets(content_fields):
    content = SkillContent(**content_fields)
    reordered = dict(reversed(list(content_fields.items())))
    reordered["stages"] = list(reversed(content.stages))
    reordered["triggers"] = list(reversed(content.triggers))
    assert SkillContent(**reordered) == content
    assert content_sha256(SkillContent(**reordered)) == sha256(canonical_json_bytes(content)).hexdigest()
    assert SkillContent.model_validate_json(canonical_json_bytes(content)) == content


def test_pre_recommendation_content_keeps_exact_hash(content_fields):
    legacy = {**content_fields, "stages": sorted(content_fields["stages"]),
              "triggers": sorted(content_fields["triggers"]), "schema_version": 1, "usage_scope": "local_only"}
    loaded = SkillContent.model_validate_json(canonical_json_bytes(legacy))
    assert canonical_json_bytes(loaded) == canonical_json_bytes(legacy)


def test_keyword_only_skill_round_trips_and_normalizes_combinations(content_fields):
    fields = {**content_fields, "triggers": [], "keyword_groups": [["view", "Signal"], ["model", "row"]]}
    content = SkillContent(**fields)
    assert content == SkillContent(**{**fields, "keyword_groups": [["row", "model"], ["Signal", "view"]]})
    assert SkillContent.model_validate_json(canonical_json_bytes(content)) == content


@pytest.mark.parametrize("field,value", [
    ("body", "Inspect the active filter."),
    ("stages", ["root_cause_analysis"]),
    ("triggers", ["different symptom"]),
    ("boundaries", ["Only for filtered views."]),
    ("exclusions", []),
    ("usage_scope", "allowed_by_policy"),
    ("keyword_groups", [["model", "role"]]),
    ("project_tags", ["Qt"]),
    ("task_types", ["feature"]),
    ("summary", "Check the active model row."),
])
def test_skill_hash_covers_each_execution_field(content_fields, field, value):
    original = SkillContent(**content_fields)
    changed = SkillContent(**{**content_fields, field: value})
    assert content_sha256(changed) != content_sha256(original)


def test_private_fields_stay_in_draft(content_fields):
    with_evidence = SkillDraftData(
        name="本机备注名称", content=content_fields, developer_note="private text",
        evidence_revision=1,
        evidence_refs=[{"kind": "artifact", "record_id": "private-id", "content_sha256": "f" * 64}],
        model_suggestion="private suggestion",
    )
    anonymous = SkillDraftData(name="Published name", content=content_fields)
    assert canonical_json_bytes(with_evidence.reviewed_content()) == canonical_json_bytes(anonymous.reviewed_content())
    assert b"private" not in canonical_json_bytes(with_evidence.reviewed_content())
    assert with_evidence.evidence_refs[0].record_id == "private-id"


def test_incomplete_draft_can_be_saved_but_requires_review_fields():
    only_note = SkillDraftData(developer_note="Inspect the model first.")
    partial = SkillDraftData(name="WIP", content={"body": "Partly written."})
    with pytest.raises(ValueError):
        only_note.reviewed_content()
    with pytest.raises(ValidationError):
        partial.reviewed_content()
    assert partial.content.body == "Partly written."


@pytest.mark.parametrize("extra_fields", [
    {"schema_version": 2}, {"schema_version": True}, {"schema_version": 1.0},
    {"body": " "}, {"body": "a" * 2_001},
    {"stages": ["implementation"]}, {"triggers": []}, {"boundaries": []},
    {"keyword_groups": [["Qt"]]}, {"keyword_groups": [["Qt", "qt"]]}, {"task_types": ["auto"]},
    {"evidence_refs": []}, {"use_count": 1}, {"created_at": "private"},
])
def test_review_rejects_invalid_or_private_fields(content_fields, extra_fields):
    with pytest.raises(ValidationError):
        SkillContent(**{**content_fields, **extra_fields})


def test_evidence_requires_content_digest_for_artifacts_only():
    with pytest.raises(ValidationError):
        SkillEvidenceRef(kind="artifact", record_id="artifact-id")
    for kind in ("event", "node"):
        with pytest.raises(ValidationError):
            SkillEvidenceRef(kind=kind, record_id="mission:17", content_sha256="a" * 64)
    event_id = "m" * 256 + ":17"
    assert SkillEvidenceRef(kind="event", record_id=event_id).record_id == event_id
    with pytest.raises(ValidationError):
        SkillEvidenceRef(kind="event", record_id="a" * 301)
    with pytest.raises(ValidationError):
        SkillDraftData(evidence_revision=0)


def test_version_content_is_deeply_immutable(content_fields):
    content = SkillContent(**content_fields)
    before = content_sha256(content)
    content_fields["triggers"].clear()
    with pytest.raises(ValidationError):
        content.body = "Rewrite"
    assert isinstance(content.triggers, tuple)
    assert content_sha256(content) == before
