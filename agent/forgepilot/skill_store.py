"""Reviewed local skills and their private working drafts in one event store."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from .artifacts.canonical import canonical_json_bytes, content_sha256
from .errors import ForgePilotError
from .mission_events import append_mission_event
from .recording import now
from .skill_mission_drafts import validate_mission_artifact_refs
from .skill_models import SkillContent, SkillDraft, SkillDraftData, SkillIdentity, SkillVersion
from .skill_selection import FrozenSkill, load_frozen, resolve_selection

if TYPE_CHECKING:
    from .sqlite_store import SqliteEventStore


@dataclass(frozen=True)
class SkillMatchMetadata:
    skill_id: str
    version: int
    content_sha256: str
    name: str
    summary: str
    triggers: list[str]
    exclusions: list[str]
    keyword_groups: list[list[str]]
    project_tags: list[str]
    task_types: list[str]


def _draft(row: sqlite3.Row) -> SkillDraft:
    return SkillDraft(
        draft_id=row["draft_id"], revision=row["revision"], source_mission_id=row["source_mission_id"],
        data=SkillDraftData.model_validate_json(row["data_json"]),
        base_skill_id=row["base_skill_id"], base_version=row["base_version"],
        result_skill_id=row["result_skill_id"], result_version=row["result_version"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _draft_row(db: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM skill_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        raise ForgePilotError("skill_draft_not_found", "Skill draft does not exist")
    return row


def _version(row: sqlite3.Row) -> SkillVersion:
    return SkillVersion(
        skill_id=row["skill_id"], version=row["version"], name=row["name"],
        content=SkillContent.model_validate_json(row["content_json"]),
        content_sha256=row["content_sha256"], created_at=row["created_at"],
    )


def _read_version(db: sqlite3.Connection, skill_id: str, version: int) -> SkillVersion:
    row = db.execute("SELECT * FROM skill_versions WHERE skill_id=? AND version=?",
                     (skill_id, version)).fetchone()
    if row is None:
        raise ForgePilotError("skill_not_found", "Skill version does not exist")
    return _version(row)


def _identity(db: sqlite3.Connection, skill_id: str) -> SkillIdentity:
    row = db.execute("SELECT * FROM skills WHERE skill_id=?", (skill_id,)).fetchone()
    if row is None:
        raise ForgePilotError("skill_not_found", "Skill does not exist")
    return SkillIdentity(skill_id=skill_id, source=row["source"], current_version=row["current_version"],
                        enabled=bool(row["enabled"]), revision=row["revision"])


def _check_revision(current: int, expected: int) -> None:
    if type(expected) is not int or current != expected:
        raise ForgePilotError("skill_revision_conflict", "Revision changed; refresh before saving")


def _check_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        raise ForgePilotError("invalid_skill_request", "Expected a nonempty request ID of at most 256 characters")


def _check_source(db: sqlite3.Connection, missions, source_mission_id: str | None, data: SkillDraftData) -> None:
    if source_mission_id is None:
        if data.evidence_revision is not None or data.evidence_refs:
            raise ForgePilotError("invalid_skill_evidence", "Evidence requires a source Mission")
        return
    mission = db.execute("SELECT current_revision FROM task_graph_runs WHERE task_id=?",
                         (source_mission_id,)).fetchone()
    if mission is None:
        raise ForgePilotError("unknown_mission", "Source Mission does not exist")
    if data.evidence_revision is not None and data.evidence_revision > mission["current_revision"]:
        raise ForgePilotError("invalid_skill_evidence", "Evidence revision exceeds the source Mission revision")
    if data.evidence_refs and data.evidence_revision is None:
        raise ForgePilotError("invalid_skill_evidence", "Evidence references require a cutoff revision")
    for ref in data.evidence_refs:
        if ref.kind == "artifact":
            # Pre-admission artifacts have no creation event. When a Mission did
            # log the artifact, respect the same cutoff as a direct event reference.
            found = db.execute("""SELECT 1 FROM artifacts a WHERE a.task_id=? AND a.artifact_id=?
                                  AND a.content_sha256=? AND NOT EXISTS(
                                      SELECT 1 FROM graph_events e WHERE e.task_id=a.task_id
                                      AND e.revision>? AND e.event_type='artifact.created'
                                      AND json_extract(e.payload_json,'$.artifact_id')=a.artifact_id)""",
                               (source_mission_id, ref.record_id, ref.content_sha256, data.evidence_revision)).fetchone()
        elif ref.kind == "event":
            # MissionEvent.event_id is the source task ID followed by its sequence.
            sequence = ref.record_id.rsplit(":", 1)[-1]
            found = db.execute("""SELECT 1 FROM graph_events WHERE task_id=? AND sequence=?
                                  AND revision<=? AND task_id || ':' || sequence = ?""",
                               (source_mission_id, sequence, data.evidence_revision, ref.record_id)).fetchone()
        else:
            found = db.execute("SELECT 1 FROM graph_node_states WHERE task_id=? AND node_id=?",
                               (source_mission_id, ref.record_id)).fetchone()
        if found is None:
            raise ForgePilotError("invalid_skill_evidence", "Evidence reference does not match a source Mission record")
    validate_mission_artifact_refs(missions, source_mission_id,
                                  (ref for ref in data.evidence_refs if ref.kind == "artifact"))


class SkillRepository:
    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store

    def resolve_selection(self, selected_skills) -> tuple[FrozenSkill, ...]:
        with self._store.locked_connection() as db:
            return resolve_selection(db, selected_skills)

    def load_frozen(self, task_id: str, expected_manifest_sha256: str) -> tuple[FrozenSkill, ...]:
        with self._store.locked_connection() as db:
            metadata = self._store.missions.metadata(task_id)
            return load_frozen(db, task_id, metadata, expected_manifest_sha256)

    def record_injection(self, task_id: str, node_id: str, attempt: int, skills: tuple[FrozenSkill, ...]) -> None:
        if not skills:
            return
        with self._store.transaction() as db:
            previous = db.execute("""SELECT 1 FROM graph_events WHERE task_id=? AND node_id=?
                AND event_type='node.skills_injected' AND json_extract(payload_json,'$.attempt')=? LIMIT 1""",
                (task_id, node_id, attempt)).fetchone()
            if previous is None:
                append_mission_event(self._store, task_id, "node.skills_injected",
                                     {"attempt": attempt, "skills": [s.identity() for s in skills]}, node_id=node_id)

    def mission_usage(self, task_id: str) -> dict:
        # History needs only admission and injection receipts, even when execution
        # rejects a missing or damaged snapshot body.
        with self._store.locked_connection() as db:
            metadata = self._store.missions.metadata(task_id)
            injections = []
            for row in db.execute("""SELECT node_id,payload_json FROM graph_events WHERE task_id=?
                    AND event_type='node.skills_injected' ORDER BY sequence""", (task_id,)):
                event = json.loads(row["payload_json"])
                injections.extend({**identity, "node_id": row["node_id"], "attempt": event["attempt"]}
                                  for identity in event["skills"])
            return {"selected_skills": metadata.get("selected_skills", []), "injections": injections}

    def get_draft(self, draft_id: str) -> SkillDraft:
        with self._store.locked_connection() as db:
            return self._checked_draft(db, _draft_row(db, draft_id))

    def _checked_draft(self, db: sqlite3.Connection, row: sqlite3.Row) -> SkillDraft:
        draft = _draft(row)
        _check_source(db, self._store.missions, draft.source_mission_id, draft.data)
        return draft

    def _draft_rows(self, db: sqlite3.Connection, source_mission_id: str | None, *,
                    limit: int = -1, offset: int = 0) -> list[sqlite3.Row]:
        """Find drafts and their review results, most recently edited first."""
        query = "SELECT * FROM skill_drafts"
        params = ()
        if source_mission_id is not None:
            query += " WHERE source_mission_id=?"
            params = (source_mission_id,)
        return db.execute(query + " ORDER BY updated_at DESC, draft_id DESC LIMIT ? OFFSET ?",
                          (*params, limit, offset)).fetchall()

    def list_drafts(self, *, source_mission_id: str | None = None) -> list[SkillDraft]:
        with self._store.locked_connection() as db:
            return [self._checked_draft(db, row) for row in self._draft_rows(db, source_mission_id)]

    def list_draft_page(self, *, source_mission_id: str | None = None, limit: int, offset: int) -> dict:
        with self._store.locked_connection() as db:
            rows = self._draft_rows(db, source_mission_id, limit=limit + 1, offset=offset)
            return {"drafts": [self._checked_draft(db, row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

    def create_draft(self, *, request_id: str, data: SkillDraftData,
                     source_mission_id: str | None = None) -> SkillDraft:
        data = SkillDraftData.model_validate(data)
        with self._store.transaction() as db:
            return self._insert_draft(db, request_id, data, source_mission_id=source_mission_id)

    def _insert_draft(self, db: sqlite3.Connection, request_id: str, data: SkillDraftData, *,
                      source_mission_id: str | None = None, base_skill_id: str | None = None,
                      base_version: int | None = None, create_hash: str | None = None) -> SkillDraft:
        _check_request_id(request_id)
        fingerprint = create_hash or content_sha256({
            "source_mission_id": source_mission_id, "base_skill_id": base_skill_id,
            "base_version": base_version, "data": data.model_dump(mode="json"),
        })
        existing = self._replayed_draft(db, request_id, fingerprint)
        if existing is not None:
            return existing
        _check_source(db, self._store.missions, source_mission_id, data)
        draft_id = str(uuid4())
        timestamp = now()
        db.execute("""INSERT INTO skill_drafts(
                        draft_id,request_id,create_hash,source_mission_id,data_json,revision,
                        created_at,updated_at,base_skill_id,base_version)
                      VALUES(?,?,?,?,?,1,?,?,?,?)""",
                   (draft_id, request_id, fingerprint, source_mission_id, data.model_dump_json(),
                    timestamp, timestamp, base_skill_id, base_version))
        return _draft(_draft_row(db, draft_id))

    def _replayed_draft(self, db: sqlite3.Connection, request_id: str, fingerprint: str) -> SkillDraft | None:
        existing = db.execute("SELECT * FROM skill_drafts WHERE request_id=?", (request_id,)).fetchone()
        if existing is None:
            return None
        if existing["create_hash"] != fingerprint:
            raise ForgePilotError("skill_request_conflict", "Request ID already identifies another draft request")
        return self._checked_draft(db, existing)

    def edit_draft(self, draft_id: str, *, expected_revision: int, data: SkillDraftData) -> SkillDraft:
        """Replace the editable document at an exact revision."""
        data = SkillDraftData.model_validate(data)
        with self._store.transaction() as db:
            row = _draft_row(db, draft_id)
            return self._save_draft(db, row, expected_revision, data)

    def patch_draft(self, draft_id: str, *, expected_revision: int, changes: dict) -> SkillDraft:
        """Only the editor fields may change. Keep the original private evidence and cutoff."""
        if not changes or changes.keys() - {"name", "content", "developer_note", "pending_inference"}:
            raise ForgePilotError("invalid_skill_request", "Expected editable draft fields")
        with self._store.transaction() as db:
            row = _draft_row(db, draft_id)
            draft = _draft(row)
            data = SkillDraftData.model_validate({**draft.data.model_dump(), **changes})
            if (draft.result_version is None and type(expected_revision) is int and draft.revision == expected_revision + 1
                    and draft.data == data):
                return self._checked_draft(db, row)
            return self._save_draft(db, row, expected_revision, data)

    def _save_draft(self, db: sqlite3.Connection, row: sqlite3.Row,
                    expected_revision: int, data: SkillDraftData) -> SkillDraft:
        _check_revision(row["revision"], expected_revision)
        if row["result_version"] is not None:
            raise ForgePilotError("skill_revision_conflict", "Reviewed drafts are closed; start a version revision")
        _check_source(db, self._store.missions, row["source_mission_id"], data)
        db.execute("""UPDATE skill_drafts SET data_json=?,revision=revision+1,updated_at=?
                      WHERE draft_id=?""", (data.model_dump_json(), now(), row["draft_id"]))
        return _draft(_draft_row(db, row["draft_id"]))

    def draft_from_version(self, skill_id: str, version: int, *, request_id: str) -> SkillDraft:
        """Copy the reusable fields of an exact version into a new private working draft."""
        with self._store.transaction() as db:
            original = _read_version(db, skill_id, version)
            data = SkillDraftData(name=original.name, content=original.content.model_dump())
            return self._insert_draft(db, request_id, data, base_skill_id=skill_id, base_version=version)

    def publish_local(self, draft_id: str, *, expected_revision: int, request_id: str) -> SkillVersion:
        """Review the stored revision and commit its version, current pointer and receipt together."""
        _check_request_id(request_id)
        with self._store.transaction() as db:
            row = _draft_row(db, draft_id)
            receipt = db.execute("SELECT * FROM skill_drafts WHERE review_request_id=?", (request_id,)).fetchone()
            if receipt is not None:
                # Review increments the revision once and closes the draft to further edits.
                if receipt["draft_id"] != draft_id:
                    raise ForgePilotError("skill_request_conflict", "Request ID already identifies another review")
                _check_revision(receipt["revision"] - 1, expected_revision)
                return _read_version(db, receipt["result_skill_id"], receipt["result_version"])

            _check_revision(row["revision"], expected_revision)
            if row["result_version"] is not None:
                raise ForgePilotError("skill_revision_conflict", "This draft has already been reviewed")
            draft = self._checked_draft(db, row)
            try:
                content = draft.data.reviewed_content()
            except ValueError:
                raise ForgePilotError("skill_draft_incomplete",
                                      "Complete the name, guidance, stages, match rules and boundaries before review") from None

            base = _identity(db, draft.base_skill_id) if draft.base_skill_id is not None else None
            if base is not None and (base.current_version != draft.base_version or not base.enabled):
                raise ForgePilotError("skill_revision_conflict", "The base skill was updated or disabled")
            if base is None or base.source != "local":
                skill_id, version = str(uuid4()), 1
                db.execute("""INSERT INTO skills(skill_id,source,current_version,enabled,revision)
                              VALUES(?,'local',1,1,1)""", (skill_id,))
            else:
                skill_id, version = base.skill_id, base.current_version + 1
                db.execute("UPDATE skills SET current_version=?,revision=revision+1 WHERE skill_id=?",
                           (version, skill_id))

            timestamp = now()
            db.execute("""INSERT INTO skill_versions(skill_id,version,name,content_json,content_sha256,created_at)
                          VALUES(?,?,?,?,?,?)""",
                       (skill_id, version, draft.data.name, canonical_json_bytes(content).decode("utf-8"),
                        content_sha256(content), timestamp))
            db.execute("""UPDATE skill_drafts SET result_skill_id=?,result_version=?,review_request_id=?,
                          revision=revision+1,updated_at=? WHERE draft_id=?""",
                       (skill_id, version, request_id, timestamp, draft_id))
            return _read_version(db, skill_id, version)

    def get_version(self, skill_id: str, version: int) -> SkillVersion:
        with self._store.locked_connection() as db:
            return _read_version(db, skill_id, version)

    def get_skill(self, skill_id: str) -> SkillIdentity:
        with self._store.locked_connection() as db:
            return _identity(db, skill_id)

    def list_skill_page(self, *, limit: int, offset: int) -> dict:
        with self._store.locked_connection() as db:
            rows = db.execute("""SELECT s.skill_id,s.source,s.current_version,s.enabled,s.revision,v.name
                                 FROM skills s JOIN skill_versions v
                                 ON (s.skill_id,s.current_version)=(v.skill_id,v.version)
                                 ORDER BY s.skill_id LIMIT ? OFFSET ?""", (limit + 1, offset)).fetchall()
        return {"skills": [{**row, "enabled": bool(row["enabled"])} for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def list_available(self) -> list[SkillVersion]:
        with self._store.locked_connection() as db:
            rows = db.execute("""SELECT v.* FROM skills s JOIN skill_versions v
                                 ON (s.skill_id,s.current_version)=(v.skill_id,v.version)
                                 WHERE s.enabled=1 ORDER BY s.skill_id""")
            return [_version(row) for row in rows]

    def list_match_metadata(self) -> list[SkillMatchMetadata]:
        """Project the enabled, current versions in a single SQLite snapshot."""
        with self._store.locked_connection() as db:
            rows = db.execute("""
                SELECT v.skill_id, v.version, v.content_sha256, v.name,
                       json_extract(v.content_json,'$.summary') AS summary,
                       json_extract(v.content_json,'$.triggers','$.exclusions','$.keyword_groups',
                                    '$.project_tags','$.task_types') AS rules
                FROM skills s JOIN skill_versions v
                    ON (s.skill_id,s.current_version)=(v.skill_id,v.version)
                WHERE s.enabled=1""").fetchall()
        result = []
        for row in rows:
            triggers, exclusions, groups, tags, types = json.loads(row["rules"])
            summary = row["summary"] or (("适用现象：" + triggers[0])[:256] if triggers else row["name"])
            result.append(SkillMatchMetadata(
                skill_id=row["skill_id"], version=row["version"], content_sha256=row["content_sha256"],
                name=row["name"], summary=summary, triggers=triggers, exclusions=exclusions,
                keyword_groups=groups or [], project_tags=tags or [], task_types=types or [],
            ))
        return result

    def disable(self, skill_id: str, *, expected_revision: int) -> SkillIdentity:
        with self._store.transaction() as db:
            current = _identity(db, skill_id)
            if not current.enabled and type(expected_revision) is int and current.revision == expected_revision + 1:
                return current
            _check_revision(current.revision, expected_revision)
            if current.enabled:
                db.execute("UPDATE skills SET enabled=0,revision=revision+1 WHERE skill_id=?", (skill_id,))
            return _identity(db, skill_id)
