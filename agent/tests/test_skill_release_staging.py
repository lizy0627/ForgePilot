"""Only approved catalog assets should cross into a frozen distribution."""
import importlib.util
from pathlib import Path

import pytest


def test_skill_release_staging_excludes_unlisted_private_files_and_verifies_bytes(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/skill_release.py"
    spec = importlib.util.spec_from_file_location("skill_release", script)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    source = tmp_path / "resources"
    source.mkdir()
    (source / "catalog.json").write_text('{"schema_version":1,"packages":[]}', encoding="utf-8")
    release.add_fixture(source)
    private = source / "unlisted"
    private.mkdir()
    (private / "SKILL.md").write_text("private draft", encoding="utf-8")
    (source / "history.sqlite3").touch()
    monkeypatch.setattr(release, "builtin_resource_directory", lambda: source)

    staged = tmp_path / "staged"
    release.stage(staged)
    release.check_resources(staged)
    assert {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()} == {
        "catalog.json", "release-test-qt/SKILL.md", "release-test-qt/manifest.json"}
    (staged / "release-test-qt/SKILL.md").write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        release.check_resources(staged)
