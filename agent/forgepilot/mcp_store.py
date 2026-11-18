"""MCP records on SqliteEventStore's existing connection, lock and transaction.

Only ciphertext enters this module. Network/DPAPI work happens before these short
transactions. Capacity is rechecked under the write lock to cover competing IDs.
"""
import sqlite3

from .errors import ForgePilotError


def create_mcp_schema(db):
    db.execute("""CREATE TABLE mcp_servers(
        server_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision > 0),
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 40),
        transport TEXT NOT NULL CHECK(transport IN ('streamable_http','stdio')),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        connection_json TEXT NOT NULL CHECK(length(CAST(connection_json AS BLOB)) <= 262144),
        policy_json TEXT NOT NULL CHECK(length(CAST(policy_json AS BLOB)) <= 16384),
        catalog_json TEXT NOT NULL CHECK(length(CAST(catalog_json AS BLOB)) <= 262144),
        secrets_dpapi BLOB NOT NULL CHECK(typeof(secrets_dpapi)='blob' AND length(secrets_dpapi) <= 524288),
        last_checked_at TEXT,
        last_check_status TEXT CHECK(last_check_status IN ('success','failed')),
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")


class MCPConfigStore:
    def __init__(self, event_store):
        self.events = event_store

    def get(self, server_id):
        with self.events._lock:
            row = self.events._db.execute("SELECT * FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone()
            return dict(row) if row is not None else None

    def all(self):
        with self.events._lock:
            return [dict(row) for row in self.events._db.execute(
                "SELECT * FROM mcp_servers ORDER BY created_at,server_id").fetchall()]

    @staticmethod
    def check(current, expected):
        # created_at also prevents an in-flight operation targeting a deleted and
        # recreated UUID whose revision happens to equal the old revision.
        if ((current is None) != (expected is None) or (current is not None and
                (current["revision"], current["created_at"]) != (expected["revision"], expected["created_at"]))):
            raise ForgePilotError("mcp_config_conflict", "MCP configuration changed; refresh before retrying")

    def recheck(self, server_id, expected):
        self.check(self.get(server_id), expected)

    def save(self, server_id, expected, values, *, checked_at, capacity, present, migration=None):
        fields = ("name", "transport", "enabled", "connection_json", "policy_json", "catalog_json", "secrets_dpapi")
        try:
            with self.events._transaction():
                current = self.get(server_id)
                self.check(current, expected)
                proposed = {**values, "server_id": server_id}
                capacity([row for row in self.all() if row["server_id"] != server_id] + [proposed])
                changed = current is None or any(values[field] != current[field] for field in fields)
                revision = 1 if current is None else current["revision"] + int(changed)
                created = current["created_at"] if current else checked_at
                updated = checked_at if changed else current["updated_at"]
                self.events._db.execute("""INSERT INTO mcp_servers(
                    server_id,revision,name,transport,enabled,connection_json,policy_json,catalog_json,secrets_dpapi,
                    last_checked_at,last_check_status,last_error_code,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'success',NULL,?,?)
                    ON CONFLICT(server_id) DO UPDATE SET revision=excluded.revision,name=excluded.name,
                    transport=excluded.transport,enabled=excluded.enabled,connection_json=excluded.connection_json,
                    policy_json=excluded.policy_json,catalog_json=excluded.catalog_json,secrets_dpapi=excluded.secrets_dpapi,
                    last_checked_at=excluded.last_checked_at,last_check_status='success',last_error_code=NULL,
                    updated_at=excluded.updated_at""",
                    (server_id, revision, *(values[field] for field in fields), checked_at, created, updated))
                # Validate the bounded public response before COMMIT. A response
                # limit failure must not leave a successfully changed record.
                result = present(self.get(server_id))
                if migration:
                    self.events._db.execute('INSERT INTO mcp_migrations(name,completed_at) VALUES(?,?)', (migration,checked_at))
            return result
        except sqlite3.Error:
            raise ForgePilotError("mcp_storage", "MCP configuration was not saved; previous values were retained") from None

    def set_enabled(self, server_id, expected, enabled, *, now, capacity, present, checked=False):
        try:
            with self.events._transaction():
                current = self.get(server_id)
                self.check(current, expected)
                proposed = {**current, "enabled": int(enabled)}
                capacity([row for row in self.all() if row["server_id"] != server_id] + [proposed])
                changed = current["enabled"] != int(enabled)
                self.events._db.execute("""UPDATE mcp_servers SET enabled=?,revision=?,updated_at=? WHERE server_id=?""",
                    (int(enabled), current["revision"] + int(changed), now if changed else current["updated_at"], server_id))
                if checked:
                    self.events._db.execute("""UPDATE mcp_servers SET last_checked_at=?,last_check_status='success',
                        last_error_code=NULL WHERE server_id=?""", (now, server_id))
                result = present(self.get(server_id))
            return result
        except sqlite3.Error:
            raise ForgePilotError("mcp_storage", "MCP enable state was not saved") from None

    def checked(self, server_id, expected, *, at, present, error_code=None):
        try:
            with self.events._transaction():
                self.check(self.get(server_id), expected)
                self.events._db.execute("""UPDATE mcp_servers SET last_checked_at=?,last_check_status=?,
                    last_error_code=? WHERE server_id=?""", (at, "failed" if error_code else "success", error_code, server_id))
                result = present(self.get(server_id))
            return result
        except sqlite3.Error:
            raise ForgePilotError("mcp_storage", "MCP check status was not saved") from None

    def delete(self, server_id, expected):
        try:
            with self.events._transaction():
                self.check(self.get(server_id), expected)
                self.events._db.execute("DELETE FROM mcp_servers WHERE server_id=?", (server_id,))
        except sqlite3.Error:
            raise ForgePilotError("mcp_storage", "MCP configuration was not deleted") from None
