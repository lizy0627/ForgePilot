"""SQLite schema for the local skill library and per-Mission selections."""
import sqlite3


def create_skill_schema(db: sqlite3.Connection) -> None:
    """Create the library inside the event store's existing migration transaction."""
    statements = (
        # Deferring the pointer lets a new identity and its first version appear
        # together at commit, without allowing an identity with a dangling pointer.
        """CREATE TABLE skills(
            skill_id TEXT PRIMARY KEY,
            source TEXT NOT NULL CHECK(source IN ('local','builtin','team')),
            current_version INTEGER NOT NULL CHECK(current_version >= 1),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
            use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count >= 0),
            last_used_at TEXT,
            FOREIGN KEY(skill_id,current_version) REFERENCES skill_versions(skill_id,version)
                DEFERRABLE INITIALLY DEFERRED) STRICT""",
        """CREATE TABLE skill_versions(
            skill_id TEXT NOT NULL REFERENCES skills(skill_id),
            version INTEGER NOT NULL CHECK(version >= 1),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0),
            content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object'),
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            PRIMARY KEY(skill_id,version)) STRICT""",
        """CREATE TRIGGER skill_versions_no_update BEFORE UPDATE ON skill_versions
            BEGIN SELECT RAISE(ABORT,'skill versions are immutable'); END""",
        """CREATE TRIGGER skill_versions_no_delete BEFORE DELETE ON skill_versions
            BEGIN SELECT RAISE(ABORT,'skill versions are immutable'); END""",
        # INSERT OR REPLACE can bypass DELETE triggers when recursive_triggers is off.
        """CREATE TRIGGER skill_versions_no_replace BEFORE INSERT ON skill_versions
            WHEN EXISTS(SELECT 1 FROM skill_versions WHERE skill_id=NEW.skill_id AND version=NEW.version)
            BEGIN SELECT RAISE(ABORT,'skill versions are immutable'); END""",
        """CREATE TABLE skill_drafts(
            draft_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            create_hash TEXT NOT NULL CHECK(length(create_hash)=64 AND create_hash NOT GLOB '*[^0-9a-f]*'),
            source_mission_id TEXT REFERENCES task_graph_runs(task_id) ON DELETE SET NULL,
            data_json TEXT NOT NULL CHECK(json_valid(data_json) AND json_type(data_json)='object'),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            base_skill_id TEXT,
            base_version INTEGER,
            review_request_id TEXT UNIQUE,
            result_skill_id TEXT,
            result_version INTEGER,
            CHECK((base_skill_id IS NULL) = (base_version IS NULL)),
            CHECK((review_request_id IS NULL) = (result_skill_id IS NULL)),
            CHECK((result_skill_id IS NULL) = (result_version IS NULL)),
            FOREIGN KEY(base_skill_id,base_version) REFERENCES skill_versions(skill_id,version),
            FOREIGN KEY(result_skill_id,result_version) REFERENCES skill_versions(skill_id,version)) STRICT""",
        "CREATE INDEX skill_drafts_recent ON skill_drafts(updated_at DESC,draft_id DESC)",
        "CREATE INDEX skill_drafts_by_mission ON skill_drafts(source_mission_id,updated_at DESC,draft_id DESC)",
        """CREATE TABLE mission_skill_snapshots(
            mission_id TEXT NOT NULL REFERENCES task_graph_runs(task_id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK(position >= 0),
            skill_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object'),
            content_sha256 TEXT NOT NULL,
            usage_scope TEXT NOT NULL CHECK(usage_scope IN ('local_only','allowed_by_policy')),
            CHECK(usage_scope='local_only' OR json_extract(content_json,'$.usage_scope') IS 'allowed_by_policy'),
            PRIMARY KEY(mission_id,skill_id),
            UNIQUE(mission_id,position),
            FOREIGN KEY(skill_id,version) REFERENCES skill_versions(skill_id,version)) STRICT""",
        """CREATE TRIGGER mission_skill_snapshot_insert BEFORE INSERT ON mission_skill_snapshots BEGIN
            SELECT RAISE(ABORT,'snapshot must copy the referenced version')
                WHERE NOT EXISTS(SELECT 1 FROM skill_versions
                    WHERE skill_id=NEW.skill_id AND version=NEW.version AND content_sha256=NEW.content_sha256
                        AND name=NEW.name AND content_json=NEW.content_json);
            SELECT RAISE(ABORT,'skill snapshot already exists')
                WHERE EXISTS(SELECT 1 FROM mission_skill_snapshots WHERE mission_id=NEW.mission_id
                    AND (position=NEW.position OR skill_id=NEW.skill_id));
            END""",
        """CREATE TRIGGER mission_skill_snapshot_no_update BEFORE UPDATE ON mission_skill_snapshots
            BEGIN SELECT RAISE(ABORT,'skill snapshots are immutable'); END""",
    )
    for statement in statements:
        db.execute(statement)
