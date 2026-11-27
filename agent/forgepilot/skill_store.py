"""Reviewed local skills and their private working drafts in one event store."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from uuid import uuid4

from .artifacts.canonical import canonical_json_bytes, content_sha256
from .errors import ForgePilotError
from .mission_events import append_mission_event
from .recording import now
from .skill_evidence_projection import validate_mission_artifact_refs, validate_mission_evidence_records
from .skill_mission_drafts import freeze_mission_draft_data
from .skill_models import SkillDraft, SkillDraftData, SkillGenerationAttempt, SkillIdentity, SkillVersion
from .skill_selection import FrozenSkill, load_frozen, resolve_selection
from .skill_version_reader import read_skill_version, skill_version_from_row


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


def _attempt(row: sqlite3.Row) -> SkillGenerationAttempt:
    return SkillGenerationAttempt(
        attempt_id=row["attempt_id"], draft_id=row["draft_id"], start_revision=row["start_revision"],
        status=row["status"], template_version=row["template_version"], route=row["route"],
        elapsed_ms=row["elapsed_ms"], error_code=row["error_code"],
        usage=json.loads(row["usage_json"]) if row["usage_json"] else None,
        suggestion=json.loads(row["suggestion_json"]) if row["suggestion_json"] else None,
    )


def _with_latest_attempt(db: sqlite3.Connection, draft: SkillDraft) -> SkillDraft:
    row = db.execute("""SELECT * FROM skill_generation_attempts WHERE draft_id=?
                        ORDER BY sequence DESC LIMIT 1""", (draft.draft_id,)).fetchone()
    if row is None:
        return draft
    latest = _attempt(row)
    return draft.model_copy(update={"latest_attempt": latest,
                                    "data": draft.data.model_copy(update={"generation_status": latest.status})})


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


def _check_source(db: sqlite3.Connection, missions, source_mission_id: str | None, data: SkillDraftData,
                  draft_id: str | None = None, *, verify_artifacts: bool = True) -> None:
    if data.edited_lesson is not None:
        found = db.execute('''SELECT 1 FROM skill_generation_attempts
                              WHERE draft_id=? AND attempt_id=? AND suggestion_json IS NOT NULL''',
                           (draft_id, data.edited_lesson.attempt_id)).fetchone()
        if found is None:
            raise ForgePilotError("invalid_skill_evidence", "The edited lesson requires a suggestion from this draft")
    if source_mission_id is None:
        if data.evidence_revision is not None or data.evidence_refs:
            raise ForgePilotError("invalid_skill_evidence", "Evidence requires a source Mission")
        return
    validate_mission_evidence_records(missions, source_mission_id, data.evidence_revision, data.evidence_refs)
    if verify_artifacts:
        validate_mission_artifact_refs(missions, source_mission_id,
                                       (ref for ref in data.evidence_refs if ref.kind == "artifact"))


class SkillRepository:
    def __init__(self, store) -> None:
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

    def get_draft(self, draft_id: str, *, verify_artifacts: bool = True) -> SkillDraft:
        with self._store.locked_connection() as db:
            return self._checked_draft(db, _draft_row(db, draft_id), verify_artifacts=verify_artifacts)

    def _checked_draft(self, db: sqlite3.Connection, row: sqlite3.Row, *, verify_artifacts: bool = True) -> SkillDraft:
        draft = _draft(row)
        _check_source(db, self._store.missions, draft.source_mission_id, draft.data, draft.draft_id,
                      verify_artifacts=verify_artifacts)
        return _with_latest_attempt(db, draft)

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
            return self._create_draft(db, request_id, data, source_mission_id=source_mission_id)

    def _create_draft(self, db: sqlite3.Connection, request_id: str, data: SkillDraftData, *,
                      source_mission_id: str | None = None, base_skill_id: str | None = None,
                      base_version: int | None = None) -> SkillDraft:
        fingerprint = content_sha256({
            "source_mission_id": source_mission_id, "base_skill_id": base_skill_id,
            "base_version": base_version, "data": data.model_dump(mode="json"),
        })
        existing = self._replayed_draft(db, request_id, fingerprint)
        if existing is not None:
            return existing
        return self._insert_draft(db, request_id, fingerprint, data, source_mission_id=source_mission_id,
                                  base_skill_id=base_skill_id, base_version=base_version)

    def _insert_draft(self, db: sqlite3.Connection, request_id: str, fingerprint: str, data: SkillDraftData, *,
                      source_mission_id: str | None = None, base_skill_id: str | None = None,
                      base_version: int | None = None) -> SkillDraft:
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

    def _replayed_draft(self, db: sqlite3.Connection, request_id: str, fingerprint: str, *,
                        verify_artifacts: bool = True) -> SkillDraft | None:
        _check_request_id(request_id)
        existing = db.execute("SELECT * FROM skill_drafts WHERE request_id=?", (request_id,)).fetchone()
        if existing is None:
            return None
        if existing["create_hash"] != fingerprint:
            raise ForgePilotError("skill_request_conflict", "Request ID already identifies another draft request")
        return self._checked_draft(db, existing, verify_artifacts=verify_artifacts)

    def create_mission_draft(self, task_id: str, *, developer_note: str = "",
                             request_id: str | None = None) -> SkillDraft:
        request_id = str(uuid4()) if request_id is None else request_id
        with self._store.transaction() as db:
            return self._create_mission_draft(db, task_id, developer_note, request_id, for_generation=False)

    def _create_mission_draft(self, db: sqlite3.Connection, task_id: str, developer_note: str,
                              request_id: str, *, for_generation: bool) -> SkillDraft:
        note = SkillDraftData(developer_note=developer_note)
        operation = "model_mission_draft" if for_generation else "manual_mission_draft"
        fingerprint = content_sha256({"operation": operation, "mission_id": task_id,
                                      "developer_note": note.developer_note})
        # Replay the original intent before inspecting the current Mission or selecting newer artifacts.
        existing = self._replayed_draft(db, request_id, fingerprint, verify_artifacts=not for_generation)
        if existing is not None:
            return existing
        data = freeze_mission_draft_data(self._store.missions, self._store.artifacts, task_id, note,
                                         for_generation=for_generation)
        return self._insert_draft(db, request_id, fingerprint, data, source_mission_id=task_id)

    def create_generation(self, task_id: str, *, developer_note: str = "", request_id: str | None = None,
                          template_version: str) -> tuple[SkillDraft, str, bool]:
        request_id = str(uuid4()) if request_id is None else request_id
        with self._store.transaction() as db:
            draft = self._create_mission_draft(db, task_id, developer_note, request_id, for_generation=True)
            return self._begin_generation(db, draft, request_id, template_version, expected_revision=None)

    def regenerate(self, draft_id: str, *, expected_revision: int, request_id: str,
                   template_version: str) -> tuple[SkillDraft, str, bool]:
        with self._store.transaction() as db:
            # Admission checks ownership, cutoff and stored hashes. The worker
            # verifies the bounded payloads it will use before calling the model.
            draft = self._checked_draft(db, _draft_row(db, draft_id), verify_artifacts=False)
            return self._begin_generation(db, draft, request_id, template_version, expected_revision=expected_revision)

    def _begin_generation(self, db, draft, request_id, template_version, *, expected_revision):
        _check_request_id(request_id)
        operation = "create" if expected_revision is None else "regenerate"
        # Replay before checking editor revision or closed state. The client may
        # be recovering an acknowledgement well after the developer moved on.
        prior = db.execute("SELECT * FROM skill_generation_attempts WHERE request_id=?", (request_id,)).fetchone()
        if prior is not None:
            if (prior["draft_id"] != draft.draft_id or prior["operation"] != operation
                    or (expected_revision is not None and prior["start_revision"] != expected_revision)):
                raise ForgePilotError("skill_request_conflict", "Request ID already identifies a different attempt")
            return draft, prior["attempt_id"], False
        if expected_revision is not None:
            _check_revision(draft.revision, expected_revision)
        if draft.result_version is not None or draft.source_mission_id is None:
            raise ForgePilotError("skill_revision_conflict", "Generation requires an open Mission draft")
        if draft.latest_attempt and draft.latest_attempt.status == "generating":
            raise ForgePilotError("skill_revision_conflict", "A generation attempt is already active")
        attempt = SkillGenerationAttempt(attempt_id=str(uuid4()), draft_id=draft.draft_id, start_revision=draft.revision,
                                         status="generating", template_version=template_version)
        db.execute("""INSERT INTO skill_generation_attempts(
                      attempt_id,draft_id,request_id,operation,start_revision,status,template_version)
                      VALUES(?,?,?,?,?,'generating',?)""",
                   (attempt.attempt_id, draft.draft_id, request_id, operation, draft.revision, template_version))
        return _with_latest_attempt(db, draft), attempt.attempt_id, True

    def get_attempt(self, draft_id: str, attempt_id: str) -> SkillGenerationAttempt:
        with self._store.locked_connection() as db:
            row = db.execute("SELECT * FROM skill_generation_attempts WHERE draft_id=? AND attempt_id=?",
                             (draft_id, attempt_id)).fetchone()
            if row is None:
                raise ForgePilotError("skill_attempt_not_found", "Generation attempt does not belong to this draft")
            return _attempt(row)

    def record_generation_route(self, attempt_id: str, start_revision: int, route: str) -> bool:
        with self._store.transaction() as db:
            return db.execute("""UPDATE skill_generation_attempts SET route=? WHERE attempt_id=?
                                 AND start_revision=? AND status='generating'""",
                              (route, attempt_id, start_revision)).rowcount == 1

    def finish_generation(self, attempt_id: str, *, start_revision: int, status: str, suggestion=None,
                          error_code=None, route=None, elapsed_ms=None, usage=None) -> bool:
        """Compare against this attempt's captured revision, independent of later edits."""
        if status == "generating" or (status == "ready") != (suggestion is not None):
            raise ValueError("Expected a terminal outcome and a suggestion only for successful generation")
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM skill_generation_attempts WHERE attempt_id=? AND start_revision=?",
                             (attempt_id, start_revision)).fetchone()
            if row is None:
                return False
            # Some providers include free-form diagnostics alongside token counts.
            counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}
            outcome = SkillGenerationAttempt(
                attempt_id=attempt_id, draft_id=row["draft_id"], start_revision=start_revision, status=status,
                template_version=row["template_version"], route=route or row["route"], suggestion=suggestion,
                error_code=error_code, elapsed_ms=elapsed_ms, usage=counts or None,
            )
            encoded = outcome.suggestion.model_dump_json() if outcome.suggestion is not None else None
            if row["status"] != "generating":
                # A provider may complete after cancellation or timeout. Retain
                # the first late suggestion on its receipt without reviving it.
                if encoded and row["status"] in {"interrupted", "timed_out", "cancelled"}:
                    db.execute("""UPDATE skill_generation_attempts SET suggestion_json=?, usage_json=COALESCE(usage_json,?)
                                  WHERE attempt_id=? AND suggestion_json IS NULL""",
                               (encoded, json.dumps(counts) if counts else None, attempt_id))
                return False
            db.execute("""UPDATE skill_generation_attempts SET status=?,route=?,elapsed_ms=?,usage_json=?,
                          error_code=?,suggestion_json=? WHERE attempt_id=?""",
                       (status, outcome.route, elapsed_ms, json.dumps(counts) if counts else None,
                        error_code, encoded, attempt_id))
            return True

    def interrupt_generations(self) -> None:
        """Opening the database under its process lease gives us exclusive recovery ownership."""
        with self._store.transaction() as db:
            db.execute("""UPDATE skill_generation_attempts SET status='interrupted',error_code='process_restarted'
                          WHERE status='generating'""")

    def edit_draft(self, draft_id: str, *, expected_revision: int, data: SkillDraftData) -> SkillDraft:
        """Replace the editable document at an exact revision."""
        data = SkillDraftData.model_validate(data)
        with self._store.transaction() as db:
            row = _draft_row(db, draft_id)
            return self._save_draft(db, row, expected_revision, data)

    def patch_draft(self, draft_id: str, *, expected_revision: int, changes: dict) -> SkillDraft:
        """Only the editor fields may change. Keep the original private evidence and cutoff."""
        if not changes or changes.keys() - {"name", "content", "developer_note", "pending_inference", "edited_lesson"}:
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
        _check_source(db, self._store.missions, row["source_mission_id"], data, row["draft_id"])
        db.execute("""UPDATE skill_drafts SET data_json=?,revision=revision+1,updated_at=?
                      WHERE draft_id=?""", (data.model_dump_json(), now(), row["draft_id"]))
        return _with_latest_attempt(db, _draft(_draft_row(db, row["draft_id"])))

    def draft_from_version(self, skill_id: str, version: int, *, request_id: str) -> SkillDraft:
        """Copy the reusable fields of an exact version into a new private working draft."""
        with self._store.transaction() as db:
            original = read_skill_version(db, skill_id, version)
            data = SkillDraftData(name=original.name, content=original.content.model_dump())
            return self._create_draft(db, request_id, data, base_skill_id=skill_id, base_version=version)

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
                return read_skill_version(db, receipt["result_skill_id"], receipt["result_version"])

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
            return read_skill_version(db, skill_id, version)

    def get_version(self, skill_id: str, version: int) -> SkillVersion:
        with self._store.locked_connection() as db:
            return read_skill_version(db, skill_id, version)

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
            return [skill_version_from_row(row) for row in rows]

    def list_match_metadata(self) -> list[SkillMatchMetadata]:
        """Project the enabled, current versions in a single SQLite snapshot."""
        with self._store.locked_connection() as db:
            rows = db.execute("""
                SELECT v.skill_id, v.version, v.content_sha256, v.name,
                       v.content_sha256 IN (
                           SELECT bv.content_sha256 FROM skills bs JOIN skill_versions bv ON bs.skill_id=bv.skill_id
                           WHERE bs.source='builtin'
                       ) AS promoted,
                       json_extract(v.content_json,'$.summary') AS summary,
                       json_extract(v.content_json,'$.triggers','$.exclusions','$.keyword_groups',
                                    '$.project_tags','$.task_types') AS rules
                FROM skills s JOIN skill_versions v
                    ON (s.skill_id,s.current_version)=(v.skill_id,v.version)
                WHERE s.enabled=1 ORDER BY s.source='builtin',v.skill_id""").fetchall()
        # Prefer the developer's local identity for promoted content. Independent
        # local-only skills retain their historical ranking and identity.
        emitted = set()
        result = []
        for row in rows:
            digest = row["content_sha256"]
            if row["promoted"]:
                if digest in emitted:
                    continue
                emitted.add(digest)
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
            db.execute("""UPDATE skills SET enabled=0,builtin_withdrawn=0,revision=revision+1
                          WHERE skill_id=? AND (enabled=1 OR builtin_withdrawn=1)""", (skill_id,))
            return _identity(db, skill_id)
