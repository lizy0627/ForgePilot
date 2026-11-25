"""Package a reviewed version without exposing its private working draft."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys

import pytest
from forgepilot_eval.reviewed_qt_skill import QT_SKILL_SHA256

from forgepilot.errors import ForgePilotError
from forgepilot.skill_models import SkillDraftData
from forgepilot.skill_package import export_skill_package, read_skill_package
from forgepilot.sqlite_store import SqliteEventStore


@pytest.fixture
def reviewed(tmp_path, qt_draft):
    with closing(SqliteEventStore(tmp_path / "work.sqlite3")) as store:
        private_data = qt_draft.model_copy(update={
            "developer_note": "private note from the original task",
            "model_suggestion": r"D:\private\repo.cpp",
            "pending_inference": "[DEBUG] raw private log",
        })
        draft = store.skills.create_draft(request_id="create", data=private_data)
        version = store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="review")
        yield store.skills, version, draft


def export(reviewed, directory):
    skills, version, _ = reviewed
    return export_skill_package(skills, version.skill_id, version.version, directory,
                                slug="qt-save", release_version=7)


def test_package_round_trip_and_readonly_cli(reviewed, tmp_path):
    _, version, draft = reviewed
    package = export(reviewed, tmp_path)
    bundle = tmp_path / "qt-save"
    assert read_skill_package(bundle) == package
    assert (package.version, package.name, package.content, package.content_sha256) == (
        7, version.name, version.content, QT_SKILL_SHA256)
    files = {p.name: p.read_text(encoding="utf-8") for p in bundle.iterdir()}
    assert files.keys() == {"SKILL.md", "manifest.json"}
    assert all(part in files["SKILL.md"] for part in (version.name, version.content.summary, version.content.body))
    assert version.content.body not in files["manifest.json"]
    for private in (draft.draft_id, version.skill_id, draft.data.developer_note, "repo.cpp", "private log"):
        assert all(private not in text for text in files.values())
    with pytest.raises(FileExistsError):
        export(reviewed, tmp_path)

    # Export the committed version while the writer owns the database lease.
    output = subprocess.run([sys.executable, "-m", "forgepilot.skill_package", "--database",
                             str(tmp_path / "work.sqlite3"), "--skill-id", version.skill_id, "--version", "1",
                             "--slug", "qt-save", "--release-version", "7", "--output-directory", str(tmp_path / "cli")],
                            cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True)
    assert QT_SKILL_SHA256.encode() in output.stdout
    assert read_skill_package(tmp_path / "cli" / "qt-save") == package


def test_draft_cannot_be_exported(reviewed, tmp_path):
    skills, _, draft = reviewed
    with pytest.raises(ForgePilotError, match="skill_not_found"):
        export_skill_package(skills, draft.draft_id, 1, tmp_path, slug="qt-save", release_version=1)
    assert not (tmp_path / "qt-save").exists()


@pytest.mark.parametrize("changed_field,sensitive", [
    ("body", r"See C:\private\main.cpp"), ("name", "source /home/alice/private.txt"),
    ("project_tags", ["e28c989e-c565-4b1b-95eb-48ed6342f8f6"]), ("summary", "def private_code():"),
    ("boundaries", ["[ERROR] private test failed"]), ("keyword_groups", [["API_KEY: hidden", "save"]]),
])
def test_rejects_private_material_pasted_into_public_fields(reviewed, tmp_path, changed_field, sensitive):
    skills, version, _ = reviewed
    data = {"name": version.name, "content": version.content.model_dump(mode="json")}
    if changed_field == "name":
        data["name"] = sensitive
    else:
        data["content"][changed_field] = sensitive
    draft = skills.create_draft(request_id="private", data=SkillDraftData.model_validate(data))
    private = skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="private")
    with pytest.raises(ValueError, match="public prose"):
        export_skill_package(skills, private.skill_id, private.version, tmp_path, slug="example", release_version=1)
    assert not (tmp_path / "example").exists()


@pytest.mark.parametrize("change", ["markdown", "metadata", "unknown", "extra_file", "directory", "duplicate"])
def test_rejects_changed_or_unreviewed_package_data(reviewed, tmp_path, change):
    export(reviewed, tmp_path)
    bundle = tmp_path / "qt-save"
    manifest = bundle / "manifest.json"
    if change == "markdown":
        markdown = bundle / "SKILL.md"
        markdown.write_bytes(markdown.read_bytes() + b"Added prose")
    elif change in {"metadata", "unknown"}:
        data = json.loads(manifest.read_bytes())
        if change == "metadata":
            data["metadata"]["usage_scope"] = "allowed_by_policy"
        else:
            data["metadata"]["raw_log"] = "hidden"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif change == "extra_file":
        (bundle / "draft.txt").write_text("excluded", encoding="utf-8")
    elif change == "directory":
        bundle = bundle.rename(tmp_path / "another")
    else:
        text = manifest.read_text(encoding="utf-8").replace('"version": 7', '"version": 1, "version": 7')
        manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        read_skill_package(bundle)
