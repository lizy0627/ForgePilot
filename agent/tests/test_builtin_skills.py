"""Install release resources through the normal database open/reopen path."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from forgepilot import builtin_skills
from forgepilot.errors import ForgePilotError
from forgepilot.skill_package import export_skill_package
from forgepilot.skill_recommendations import recommend_skills
from forgepilot.sqlite_store import SqliteEventStore


def publish(skills, data):
    draft = skills.create_draft(request_id=str(uuid4()), data=data)
    return skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id=str(uuid4()))


def recommendations(skills):
    return {r["skill_id"] for r in recommend_skills(skills, description="列表仍显示旧值",
                                                     task_type="bug_fix", project_tags=[])}


def release(directory, repository, local, version, slug="qt-example"):
    return export_skill_package(repository, local.skill_id, local.version, directory,
                                slug=slug, release_version=version)


def catalog(directory, *packages):
    directory.mkdir(parents=True, exist_ok=True)
    entries = [package.model_dump(include={"slug", "version", "content_sha256"}) for package in packages]
    (directory / "catalog.json").write_text(json.dumps({"schema_version": 1, "packages": entries}), encoding="utf-8")


def test_import_reopen_promote_and_fork(tmp_path, monkeypatch, qt_draft):
    first, second = tmp_path / "first", tmp_path / "second"
    with closing(SqliteEventStore()) as author:
        reviewed = publish(author.skills, qt_draft)
        package = release(first, author.skills, reviewed, 1)
        updated = qt_draft.model_copy(update={"content": qt_draft.content.model_copy(update={"body": "新发行指导。"})})
        new_release = release(second, author.skills, publish(author.skills, updated), 2)
    catalog(first, package)
    catalog(second, new_release)
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: first)
    db_path = tmp_path / "reader.sqlite3"
    builtin_id = "builtin:qt-example"
    with closing(SqliteEventStore(db_path)) as store:
        assert store.skills.get_version(builtin_id, 1).content == reviewed.content
        assert store.skills.list_drafts() == []
        assert recommendations(store.skills) == {builtin_id}
        source = publish(store.skills, qt_draft)
        assert recommendations(store.skills) == {source.skill_id}
        assert tuple(store._db.execute("SELECT * FROM skill_promotion_sources").fetchone()) == (
            builtin_id, 1, source.content_sha256, source.skill_id, 1)
        draft = store.skills.draft_from_version(builtin_id, 1, request_id="adapt")
        altered = qt_draft.model_copy(update={"content": qt_draft.content.model_copy(update={"body": "本机补充检查。"})})
        draft = store.skills.edit_draft(draft.draft_id, expected_revision=draft.revision, data=altered)
        fork = store.skills.publish_local(draft.draft_id, expected_revision=draft.revision, request_id="approve")
        assert store.skills.get_skill(fork.skill_id).source == "local"
        assert recommendations(store.skills) == {source.skill_id, fork.skill_id}
        # Independently reviewed copies continue to share a single recommendation
        # after a new built-in version changes the current pointer.
        duplicate = publish(store.skills, qt_draft)
        local_id = min(source.skill_id, duplicate.skill_id)
        assert recommendations(store.skills) == {local_id, fork.skill_id}
    with closing(SqliteEventStore(db_path)) as store:
        assert store.skills.get_skill(builtin_id).revision == 1
        assert store._db.execute("SELECT count(*) FROM skill_versions").fetchone()[0] == 4
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: second)
    with closing(SqliteEventStore(db_path)) as store:
        assert store.skills.get_skill(builtin_id).current_version == 2
        assert store.skills.get_version(builtin_id, 2).content_sha256 == new_release.content_sha256
        assert store.skills.get_version(builtin_id, 1).content_sha256 == package.content_sha256
        assert store.skills.get_version(fork.skill_id, 1) == fork
        assert recommendations(store.skills) == {local_id, fork.skill_id, builtin_id}


def test_withdraw_reinstate_and_preserve_user_disable(tmp_path, monkeypatch, qt_draft):
    directory = tmp_path / "skills"
    with closing(SqliteEventStore()) as author:
        package = release(directory, author.skills, publish(author.skills, qt_draft), 1)
    catalog(directory, package)
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: directory)
    path = tmp_path / "reader.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        (skill_id,) = recommendations(store.skills)
    catalog(directory)
    with closing(SqliteEventStore(path)) as store:
        assert recommendations(store.skills) == set()
        assert store.skills.get_version(skill_id, 1).content_sha256 == package.content_sha256
    catalog(directory, package)
    with closing(SqliteEventStore(path)) as store:
        assert recommendations(store.skills) == {skill_id}
        store.skills.disable(skill_id, expected_revision=store.skills.get_skill(skill_id).revision)
    with closing(SqliteEventStore(path)) as reopened:
        assert recommendations(reopened.skills) == set()


def test_failed_import_rolls_back_whole_catalog_and_retries(tmp_path, monkeypatch, qt_draft):
    first, second = tmp_path / "first", tmp_path / "second"
    with closing(SqliteEventStore()) as author:
        local = publish(author.skills, qt_draft)
        original = release(first, author.skills, local, 1)
        addition = release(second, author.skills, local, 1, slug="first-in-batch")
        renamed = publish(author.skills, qt_draft.model_copy(update={"name": "不同的名称"}))
        conflicting = release(second, author.skills, renamed, 1)
    catalog(first, original)
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: first)
    path = tmp_path / "reader.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        draft = store.skills.create_draft(request_id="private", data=qt_draft)
    # Only the second item conflicts during the write transaction.
    catalog(second, addition, conflicting)
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: second)
    with pytest.raises(ForgePilotError, match="skill_package_conflict"):
        SqliteEventStore(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT skill_id,revision FROM skills").fetchall() == [("builtin:qt-example", 1)]
    (second / "catalog.json").write_text("invalid", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="invalid_skill_catalog"):
        SqliteEventStore(path)
    with monkeypatch.context() as patch:
        patch.setenv("FORGEPILOT_DISABLE_SKILL_ENTRIES", "1")
        with closing(SqliteEventStore(path)) as store:
            assert store.skills.get_draft(draft.draft_id) == draft
    catalog(second, addition)
    with closing(SqliteEventStore(path)) as reopened:
        assert recommendations(reopened.skills) == {"builtin:first-in-batch"}
        assert reopened.skills.get_draft(draft.draft_id) == draft


@pytest.mark.parametrize("entry", [
    {"slug": "../escape", "version": 1, "content_sha256": "a" * 64},
    {"slug": "qt-example", "version": True, "content_sha256": "a" * 64},
    {"slug": "missing", "version": 1, "content_sha256": "a" * 64},
    {"slug": "qt-example", "version": 1, "content_sha256": "a" * 64, "private": "extra"},
])
def test_only_pinned_catalog_entries_are_accepted(tmp_path, entry):
    (tmp_path / "catalog.json").write_text(json.dumps({"schema_version": 1, "packages": [entry]}), encoding="utf-8")
    with pytest.raises(ForgePilotError, match="invalid_skill_catalog"):
        builtin_skills.read_catalog(tmp_path)


def test_catalog_rejects_changed_pins_and_duplicate_releases(tmp_path, qt_draft):
    with closing(SqliteEventStore()) as author:
        package = release(tmp_path, author.skills, publish(author.skills, qt_draft), 1)
    for entries in ([package.model_copy(update={"version": 2})],
                    [package.model_copy(update={"content_sha256": "1" * 64})], [package, package]):
        catalog(tmp_path, *entries)
        with pytest.raises(ForgePilotError, match="invalid_skill_catalog"):
            builtin_skills.read_catalog(tmp_path)


def test_process_exit_after_first_builtin_write_recovers(tmp_path, monkeypatch, qt_draft):
    directory = tmp_path / "skills"
    path = tmp_path / "reader.sqlite3"
    with closing(SqliteEventStore(path)) as store:
        private = store.skills.create_draft(request_id="private", data=qt_draft)
    with closing(SqliteEventStore()) as author:
        local = publish(author.skills, qt_draft)
        first = release(directory, author.skills, local, 1, slug="first")
        second = release(directory, author.skills, local, 1, slug="second")
    catalog(directory, first, second)

    # Crash after SQLite has applied the first complete version, bypassing all
    # Python exception handlers, connection cleanup and the transaction commit.
    child = """
from functools import partial
import os
from pathlib import Path
import sqlite3
import sys
from forgepilot import builtin_skills
from forgepilot.sqlite_store import SqliteEventStore

class CrashAfterVersion(sqlite3.Connection):
    def execute(self, statement, parameters=()):
        cursor = super().execute(statement, parameters)
        if statement.startswith("INSERT INTO skill_versions") and parameters[0] == "builtin:first":
            assert self.in_transaction and cursor.rowcount == 1
            os._exit(73)
        return cursor

sqlite3.connect = partial(sqlite3.connect, factory=CrashAfterVersion)
builtin_skills.builtin_resource_directory = lambda: Path(sys.argv[2])
SqliteEventStore(Path(sys.argv[1]))
"""
    result = subprocess.run([sys.executable, "-c", child, str(path), str(directory)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=45)
    assert result.returncode == 73, result.stderr
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT count(*) FROM skills").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM skill_versions").fetchone() == (0,)
    monkeypatch.setattr(builtin_skills, "builtin_resource_directory", lambda: directory)
    with closing(SqliteEventStore(path)) as reopened:
        assert reopened.skills.get_draft(private.draft_id) == private
        assert [s.skill_id for s in reopened.skills.list_available()] == ["builtin:first", "builtin:second"]
