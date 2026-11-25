"""Rehearse both rollback paths against disposable, file-backed databases."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION
from forgepilot.skill_selection import skill_manifest_sha256
from forgepilot.sqlite_store import SqliteEventStore
from test_api_v2 import request_body
from test_artifact_protocol import TASK_ID
from test_mission_events import finish_mission, make_mission
from test_skill_drafts_http import V2, error, local_api, success
from test_skill_recommendations_http import SAVE_TASK, recommend
from test_skill_selection_store import admit, publish


def test_stop_new_entries_keeps_private_draft_and_frozen_selection(tmp_path, monkeypatch, qt_draft):
    with local_api(tmp_path) as (client, store):
        make_mission(store)
        finish_mission(store)
        path = f"{V2}/missions/{TASK_ID}/skill-drafts"
        draft = success(client.post(path, json={"developer_note": "pending review"}), 201)
        ref = publish(store, qt_draft)
        assert len(recommend(client, SAVE_TASK)) == 1
        selected = store.skills.resolve_selection([ref])
        admit(store, selected)

    monkeypatch.setenv("FORGEPILOT_DISABLE_SKILL_ENTRIES", "1")
    with local_api(tmp_path) as (client, store):
        assert recommend(client, SAVE_TASK) == []
        detail = f"{V2}/skill-drafts/{draft['draft_id']}"
        assert success(client.get(detail)) == draft
        saved = success(client.patch(detail, json={
            "expected_revision": draft["revision"], "developer_note": "reviewed privately"}))
        assert saved["data"]["developer_note"] == "reviewed privately"
        for mode in ("manual", "model"):
            error(client.post(path, json={"generation_mode": mode}), 503, "skill_entries_disabled")
        error(client.post(detail + "/regenerate", json={
            "request_id": "again", "expected_revision": saved["revision"]}), 503, "skill_entries_disabled")
        error(client.post(f"{V2}/skills/{ref['skill_id']}/drafts", json={
            "request_id": "edit", "version": 1}), 503, "skill_entries_disabled")
        error(client.post(V2 + "/missions", json=request_body(selected_skills=[ref])),
              409, "skill_selection_changed")
        assert store.skills.load_frozen("m", skill_manifest_sha256(selected)) == selected


def test_restore_pre_upgrade_copy_and_preserve_post_upgrade_data(tmp_path):
    database = tmp_path / "original.sqlite3"
    with closing(SqliteEventStore(database)) as store:
        earlier = store.create_conversation(title="before")["conversation_id"]
    # Recreate the immediate predecessor's schema with the existing conversation.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP VIEW skill_promotion_sources")
        db.execute("DROP INDEX skill_versions_by_hash")
        db.execute("ALTER TABLE skills DROP COLUMN builtin_withdrawn")
        db.execute("PRAGMA user_version=13")
    with closing(SqliteEventStore(database)) as store:
        before_upgrade = Path(store.migration_backup_path)
        newer = store.create_conversation(title="after")["conversation_id"]

    launcher = ([os.environ["FORGEPILOT_VERIFY_FROZEN_AGENT"]] if os.getenv("FORGEPILOT_VERIFY_FROZEN_AGENT")
                else [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/desktop_launcher.py")])

    def copy(source, folder, *, check=True):
        return subprocess.run([*launcher, "database-copy", "--source", str(source),
                               "--destination", str(folder)], cwd=tmp_path, check=check, timeout=60)

    retained = tmp_path / "retained"
    restored = tmp_path / "restored"
    copy(database, retained)
    copy(before_upgrade, restored)
    assert copy(database, restored, check=False).returncode != 0
    for file, version, ids in (
            (restored / "history.sqlite3", 13, {earlier}),
            (retained / "history.sqlite3", CURRENT_SCHEMA_VERSION, {earlier, newer}),
            (database, CURRENT_SCHEMA_VERSION, {earlier, newer})):
        with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == version
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert {row[0] for row in db.execute("SELECT conversation_id FROM conversations")} == ids
