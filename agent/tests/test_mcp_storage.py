"""Temporary SQLite migrations/rollback and real current-user Windows DPAPI."""
import sqlite3
import subprocess
import sys

import pytest

from test_mcp_http import peers
from test_mcp_service import database, candidate, save_new, SECRET, REPLACEMENT
from forgepilot.errors import ForgePilotError
from forgepilot.mcp_service import MCPService
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.windows_secrets import protect_secrets, unprotect_secrets
import forgepilot.sqlite_store as sqlite_store


def test_real_dpapi_roundtrip_current_user_another_process_and_tamper():
    values = {"api_key": SECRET, "env:UNICODE": "唯一测试凭证🌿", "env:EMPTY": ""}
    ciphertext = protect_secrets(values)
    assert ciphertext != protect_secrets(values)  # DPAPI randomized protection.
    assert unprotect_secrets(ciphertext) == values
    assert SECRET.encode() not in ciphertext
    # Send only ciphertext through stdin; no plaintext keys in argv/files/output.
    program = """import sys
from forgepilot.windows_secrets import unprotect_secrets
values = unprotect_secrets(sys.stdin.buffer.read())
assert set(values) == {'api_key', 'env:UNICODE', 'env:EMPTY'}
assert values['env:EMPTY'] == '' and len(values['env:UNICODE']) == 7
print('current-user decrypt ok')
"""
    completed = subprocess.run([sys.executable, "-c", program], input=ciphertext, capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout.strip() == b"current-user decrypt ok"
    for damaged in (b"", b"not dpapi", ciphertext[:-8], ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])):
        with pytest.raises(ForgePilotError, match="mcp_secret_unavailable") as error:
            unprotect_secrets(damaged)
        assert SECRET not in str(error.value)


def version_three(path):
    store = SqliteEventStore(path)
    cid = store.create_conversation(title="历史不应丢失")["conversation_id"]
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE rag_index_runs")
        db.execute("DROP TABLE rag_files")
        db.execute("DROP TABLE mcp_servers")
        db.execute("DROP TABLE mcp_migrations")
        db.execute("ALTER TABLE conversation_requests DROP COLUMN mcp_calls_json")
        db.execute("PRAGMA user_version=3")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        old = {name: db.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
    return cid, old


def assert_old_data(db, old):
    for table, records in old.items():
        assert [tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')] == records
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v3_migration_backup_preserves_all_old_tables_and_only_runs_once(tmp_path):
    path = tmp_path / "v3.sqlite3"
    cid, old = version_three(path)
    store = SqliteEventStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert_old_data(store._db, old)
        assert store.conversation_detail(cid)["title"] == "历史不应丢失"
        assert MCPService(store).list_servers() == {"items": []}
    finally:
        store.close()
    backups = list(tmp_path.glob("*.v3-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert_old_data(db, old)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_servers'").fetchone()
    SqliteEventStore(path).close()
    assert len(list(tmp_path.glob("*.v3-backup-*.sqlite3"))) == 1


def test_v3_migration_failure_rolls_back_partial_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite3"
    _, old = version_three(path)
    real_create = sqlite_store.create_mcp_schema
    def fail_after_ddl(db):
        real_create(db)
        db.execute("UPDATE conversations SET title='must rollback'")
        raise sqlite3.OperationalError("injected MCP migration failure")
    monkeypatch.setattr(sqlite_store, "create_mcp_schema", fail_after_ddl)
    with pytest.raises(ForgePilotError, match="storage_unavailable"):
        SqliteEventStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_servers'").fetchone()
        assert_old_data(db, old)
    backups = list(tmp_path.glob("*.v3-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert_old_data(db, old)
    monkeypatch.setattr(sqlite_store, "create_mcp_schema", real_create)
    store = SqliteEventStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert_old_data(store._db, old)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_encryption_failure_cannot_change_existing_config_or_key(peers, database):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    before = service.store.get(sid)
    def fail_encryption(values):
        raise ForgePilotError("mcp_secret_unavailable", "injected DPAPI failure")
    failing = MCPService(database, protect=fail_encryption)
    peer.expected_secret = REPLACEMENT
    with pytest.raises(ForgePilotError, match="mcp_secret_unavailable"):
        await failing.save(sid, candidate(peer, name="failure"), base_revision=1,
                           catalog_hash=saved["catalog_hash"], secret_updates={"api_key": REPLACEMENT})
    assert service.store.get(sid) == before
    assert unprotect_secrets(before["secrets_dpapi"]) == {"api_key": SECRET}


@pytest.mark.asyncio
async def test_one_corrupt_server_does_not_block_other_snapshot_test_disable_or_delete(peers, database):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    good, bad = await save_new(service, peer), await save_new(service, peer)
    database._db.execute("UPDATE mcp_servers SET secrets_dpapi=? WHERE server_id=?", (b"bad", bad["server_id"]))
    snapshots = {snapshot.server_id: snapshot for snapshot in service.snapshots("chat")}
    assert snapshots[bad["server_id"]].secret_error == "mcp_secret_unavailable"
    assert snapshots[good["server_id"]].connection_config().secret == SECRET
    assert (await service.test(good["server_id"], revision=1))["last_check_status"] == "success"
    records = len(peer.records)
    disabled = await service.set_enabled(bad["server_id"], revision=1, enabled=False)
    service.delete(bad["server_id"], revision=disabled["revision"])
    assert len(peer.records) == records and len(service.list_servers()["items"]) == 1
