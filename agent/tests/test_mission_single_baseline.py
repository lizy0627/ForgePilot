"""Offline single-Agent Mission graph and exact-test approval boundaries."""
import asyncio
from hashlib import sha256
import json
import subprocess

import pytest

from conftest import reply
from forgepilot.errors import ForgePilotError
from forgepilot.mission_host import MissionHost
from forgepilot.validation import ValidationService

from test_mission_factory import build_dependencies, build_host
from test_mission_locked_spec import fields, frozen_payload
from test_workspace_manager import committed_repo


@pytest.mark.asyncio
async def test_single_baseline_reaches_review_and_requires_separate_exact_test_grant(
        tmp_path, git_exe, rg_exe, committed_repo, monkeypatch):
    test_file = committed_repo / "test" / "existing.cpp"
    test_file.parent.mkdir()
    test_file.write_text("int main() { return 0; }\n", encoding="utf-8")
    subprocess.run([git_exe, "-C", str(committed_repo), "add", "--", "test/existing.cpp"],
                   check=True, capture_output=True)
    subprocess.run([git_exe, "-C", str(committed_repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "test file"],
                   check=True, capture_output=True)
    payload = frozen_payload()
    payload["test_policy"] = {"existing_tests_read_only": False, "allow_new_tests": True,
                              "modifiable_existing_test_patterns": ["test/existing.cpp"]}
    payload["allowed_scope"] = {"path_patterns": ["source.txt", "test/existing.cpp"],
                                "max_changed_files": 2}
    from forgepilot.agents.contracts import validate_task_spec
    spec = validate_task_spec(payload)
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe)
    missions = MissionHost(host, dependencies=build_dependencies(host, gateway, git_exe, rg_exe),
        locked_approval_authority=lambda frozen, source, ref, actor:
        frozen == spec and source == "locked_benchmark_manifest" and ref == "a" * 64
        and actor == "benchmark-evaluator/" + "a" * 64)

    async def fake_capture(self, task_id, task_spec, **kwargs):
        return {"status": "captured"}

    async def fake_chat(messages, tools=None):
        provider.calls += 1
        prompt = messages[0]["content"]
        data = json.loads(next(message["content"] for message in messages
                               if message["role"] == "user"))
        if "Current stage: generalist_analysis" in prompt:
            content = {"summary": "Pinned source evidence", "items": [{
                "evidence_id": "EV-1", "claim": "The pinned source is the edit location.",
                "path": "source.txt", "start_line": 1, "end_line": 1,
                "file_hash": sha256((committed_repo / "source.txt").read_bytes()).hexdigest(),
                "symbol": "source", "source": "local_repository", "confidence": 0.8,
                "reason": "The current source line is present in the pinned checkout."}]}
        elif "Current stage: generalist_plan" in prompt:
            content = {"task_spec_artifact_id": data["task_spec"]["artifact_id"],
                       "summary": "Change the source and exact named test, then validate.",
                       "steps": [{"step_id": "STEP-1", "owner": "implementer.v1",
                                  "depends_on": [],
                                  "expected_files": ["source.txt", "test/existing.cpp"],
                                  "action": "Implement and update the exact approved test.",
                                  "verification": ["protected_test", "build", "ctest"],
                                  "risk": "Preserve existing tests.", "adds_tests": False,
                                  "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3"]}]}
        elif "Current stage: generalist_implementer" in prompt:
            assert data["approved_existing_tests"] == ["test/existing.cpp"]
            content = {"status": "awaiting_human", "summary": "No write was requested in this fake run"}
        else:
            raise AssertionError("Unexpected model stage")
        return reply(json.dumps(content), usage={
            "prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})

    monkeypatch.setattr(ValidationService, "capture", fake_capture)
    provider.chat = fake_chat
    await host.start()
    try:
        made = await missions.create_locked(spec, allow_locked_test_scope=True,
            locked_authorization_ref="a" * 64,
            **(fields(committed_repo, spec) | {"orchestration_mode": "single_agent"}))
        task_id = made["task_id"]
        for _ in range(300):
            current = missions.get(task_id)
            if current["status"] in {"awaiting_human", "failed", "retryable"}:
                break
            await asyncio.sleep(.02)
        assert current["status"] == "awaiting_human"
        assert provider.calls == 2
        for _ in range(300):
            active = missions._running.get(task_id)
            if active is None or active.done():
                break
            await asyncio.sleep(.02)
        assert active is None or active.done()
        review = current["review"]
        assert host.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        args = dict(expected_revision=review["revision"],
                    task_spec_hash=review["task_spec_hash"], plan_hash=review["plan_hash"],
                    scope_hash=review["scope_hash"],
                    authorization_source="locked_benchmark_manifest",
                    authorization_ref="a" * 64,
                    approved_by="benchmark-evaluator/" + "a" * 64)
        with pytest.raises(ForgePilotError, match="test_approval_required"):
            await missions.approve_existing_tests(task_id, exact_paths=["test/other.cpp"], **args)
        with pytest.raises(ForgePilotError, match="test_approval_required"):
            await missions.approve_existing_tests(task_id, exact_paths=["test/existing.cpp"],
                **(args | {"authorization_ref": "b" * 64}))
        assert host.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        from forgepilot import mission_events
        original_append = mission_events.append_mission_event
        def fail_test_grant_event(store, task_id, event_type, data, **kwargs):
            if event_type == "approval.existing_tests":
                raise RuntimeError("audit event write failed")
            return original_append(store, task_id, event_type, data, **kwargs)
        monkeypatch.setattr(mission_events, "append_mission_event", fail_test_grant_event)
        with pytest.raises(RuntimeError, match="audit event write failed"):
            await missions.approve_existing_tests(task_id, exact_paths=["test/existing.cpp"], **args)
        assert host.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        monkeypatch.setattr(mission_events, "append_mission_event", original_append)
        grant = await missions.approve_existing_tests(task_id,
            exact_paths=["test/existing.cpp"], **args)
        assert grant["approval_type"] == "existing_tests" and grant["valid"] is True
        assert grant["approved_by"] == "benchmark-evaluator/" + "a" * 64
        assert missions.get(task_id)["status"] == "awaiting_human"
        assert provider.calls == 2
        active = missions.get(task_id)["review"]
        await missions.approve(task_id, expected_revision=active["revision"],
            task_spec_hash=active["task_spec_hash"], plan_hash=active["plan_hash"],
            scope_hash=active["scope_hash"], decision="approved")
        for _ in range(300):
            if provider.calls == 3:
                break
            await asyncio.sleep(.02)
        assert provider.calls == 3
        assert (committed_repo / "source.txt").read_bytes() == b"original source\n"
    finally:
        await missions.close()
        await host.close()
        await gateway.aclose()
