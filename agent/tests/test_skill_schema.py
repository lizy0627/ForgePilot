"""File-backed migrations, immutable versions and Mission selection constraints."""
from contextlib import closing
import sqlite3

import pytest

from conftest import drop_skill_schema
from forgepilot.artifacts.canonical import canonical_json_bytes, content_sha256
from forgepilot.errors import ForgePilotError
from forgepilot.schema_versions import SchemaVersion
from forgepilot.skill_models import SkillContent
from forgepilot.sqlite_store import SqliteEventStore
import forgepilot.sqlite_store as sqlite_store_module


def schema(db):
    return db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()


def v11_database(path):
    with closing(SqliteEventStore(path)) as store:
        conversation = store.create_conversation(title="升级前的历史")["conversation_id"]
    with closing(sqlite3.connect(path)) as db, db:
        drop_skill_schema(db)
        db.execute("PRAGMA user_version=11")
        return conversation, schema(db)


def assert_v11(path, conversation, old_schema):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (11,)
        assert schema(db) == old_schema
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT title FROM conversations WHERE conversation_id=?", (conversation,)).fetchone() == (
            "升级前的历史",
        )


def test_v11_upgrade_keeps_history_and_backs_up_once(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    conversation, old_schema = v11_database(path)
    with closing(SqliteEventStore(path)) as store:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == SchemaVersion.LOCAL_SKILL_LIBRARY
        assert store.conversation_detail(conversation)["title"] == "升级前的历史"
        assert_v11(store.migration_backup_path, conversation, old_schema)
    with closing(SqliteEventStore(path)) as reopened:
        assert reopened.migration_backup_path is None
        assert reopened.conversation_detail(conversation)["title"] == "升级前的历史"
    assert len(list(tmp_path.glob("*.v11-backup-*.sqlite3"))) == 1


def test_failed_v12_migration_restores_schema_and_records(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite3"
    conversation, old_schema = v11_database(path)
    original_migration = sqlite_store_module.create_skill_schema

    def fail_after_ddl(db):
        original_migration(db)
        db.execute("UPDATE conversations SET title='interrupted'")
        raise sqlite3.OperationalError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_store_module, "create_skill_schema", fail_after_ddl)
        with pytest.raises(ForgePilotError, match="storage_unavailable"):
            SqliteEventStore(path)
    assert_v11(path, conversation, old_schema)
    backup, = tmp_path.glob("*.v11-backup-*.sqlite3")
    assert_v11(backup, conversation, old_schema)
    with closing(SqliteEventStore(path)) as reopened:
        assert reopened.conversation_detail(conversation)["title"] == "升级前的历史"
        assert reopened._db.execute("SELECT count(*) FROM skills").fetchone()[0] == 0


def test_versions_and_pointers_enforced_by_sqlite(tmp_path):
    with closing(SqliteEventStore(tmp_path / "library.sqlite3")) as store:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
            db.execute("INSERT INTO skills(skill_id,source,current_version) VALUES('s','local',1)")
        with store.transaction() as db:
            db.execute("INSERT INTO skills(skill_id,source,current_version) VALUES('s','local',1)")
            db.execute("INSERT INTO skill_versions VALUES('s',1,'Qt','{}',?,'now')", (content_sha256({}),))
        for statement in (
            "UPDATE skill_versions SET name='overwritten'",
            "DELETE FROM skill_versions",
            "INSERT OR REPLACE INTO skill_versions SELECT * FROM skill_versions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"), store.transaction() as db:
                db.execute(statement)


def test_mission_snapshots_bind_order_exact_version_and_hash(tmp_path):
    with closing(SqliteEventStore(tmp_path / "snapshot.sqlite3")) as store:
        store.missions.create_graph_run(task_id="mission", graph_id="g", graph_version=1)
        content = SkillContent(body="Inspect the model row.", stages=("root_cause_analysis",),
                               triggers=("Stale list",), boundaries=("After a successful save",))
        encoded = canonical_json_bytes(content).decode("utf-8")
        digest = content_sha256(content)
        insert = """INSERT INTO mission_skill_snapshots
            (mission_id,position,skill_id,version,name,content_json,content_sha256,usage_scope)
            VALUES(?,?,?,?,?,?,?,?)"""
        copy = ("Qt", encoded, digest, "local_only")
        with store.transaction() as db:
            for skill_id in ("first", "second"):
                db.execute("INSERT INTO skills(skill_id,source,current_version) VALUES(?,'local',1)", (skill_id,))
                db.execute("INSERT INTO skill_versions VALUES(?,1,'Qt',?,?,'now')", (skill_id, encoded, digest))
            db.execute(insert, ("mission", 0, "first", 1, *copy))
        for invalid in (
            ("missing", 1, "second", 1, *copy),
            ("mission", 0, "second", 1, *copy),
            ("mission", 1, "first", 1, *copy),
            ("mission", -1, "second", 1, *copy),
            ("mission", 1, "second", 2, *copy),
            ("mission", 1, "second", 1, "Qt", encoded, "a" * 64, "local_only"),
            ("mission", 1, "second", 1, "renamed", encoded, digest, "local_only"),
            ("mission", 1, "second", 1, "Qt", '{"changed":1}', digest, "local_only"),
            ("mission", 1, "second", 1, "Qt", encoded, digest, "unknown"),
            ("mission", 1, "second", 1, "Qt", encoded, digest, "allowed_by_policy"),
        ):
            with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
                db.execute(insert, invalid)
        with store.transaction() as db:
            db.execute(insert, ("mission", 1, "second", 1, *copy))
            assert [row[0] for row in db.execute(
                "SELECT skill_id FROM mission_skill_snapshots WHERE mission_id='mission' ORDER BY position"
            )] == ["first", "second"]
        for statement in (
            "UPDATE mission_skill_snapshots SET position=2 WHERE position=0",
            "INSERT OR REPLACE INTO mission_skill_snapshots SELECT * FROM mission_skill_snapshots",
        ):
            with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
                db.execute(statement)
        with store.transaction() as db:
            db.execute("DELETE FROM task_graph_runs WHERE task_id='mission'")
            assert db.execute("SELECT COUNT(*) FROM mission_skill_snapshots").fetchone()[0] == 0
