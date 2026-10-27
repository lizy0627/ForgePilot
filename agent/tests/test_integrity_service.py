"""End-to-end approval and final-integrity boundaries through the task service."""

import hashlib
import json

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.integrity import IntegrityTaskSpec
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from test_planning import proposal


TEST_PATH = "tests/existing.cpp"
OLD = "void check(){ EXPECT_EQ(answer(),42); }\n"
STRONGER = "void check(){ EXPECT_EQ(answer(),42); EXPECT_NE(answer(),0); }\n"


def patch_test():
    return reply(calls=[call("repository_read_range", json.dumps({"path": TEST_PATH}), "read-test"),
        call("repository_apply_patch", json.dumps({"path": TEST_PATH,
            "expected_hash": hashlib.sha256(OLD.encode()).hexdigest(),
            "edits": [{"old_text": OLD, "new_text": STRONGER}]}), "patch-test")])


def make_service(root, rg, git, messages, *, task_type="bug_fix", exact_files=(TEST_PATH,), paths=(TEST_PATH,)):
    (root / "tests").mkdir(exist_ok=True)
    (root / TEST_PATH).write_bytes(OLD.encode())
    trace = MemoryTrace()
    llm = ScriptedLLM([reply("Inspect existing tests"), reply(json.dumps(proposal(*paths))), *messages])
    service = TaskService(llm, rg_executable=rg, git_executable=git, trace=trace,
        task_spec=IntegrityTaskSpec(task_type, exact_files))
    return service, trace


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["bug_fix", "feature"])
@pytest.mark.parametrize("separate_approval", [False, True])
async def test_existing_test_edits_require_exact_spec_and_separate_approval(
        cpp_repo, rg_exe, git_exe, task_type, separate_approval):
    service, trace = make_service(cpp_repo, rg_exe, git_exe, [patch_test(), reply("Added assertion")],
        task_type=task_type)
    try:
        pending = await service.start(str(cpp_repo), "Strengthen the existing regression test")
        approved = service.approve(pending.task_id, 1,
            approved_test_files=(TEST_PATH,) if separate_approval else ())
        assert approved.test_integrity["test_files"]
        assert approved.test_integrity["plan_version"] == 1
        assert approved.test_integrity["approved_test_files"] == ([TEST_PATH] if separate_approval else [])
        result = await service.execute(pending.task_id)
        if separate_approval:
            assert result.state == TaskState.COMPLETED, result.error
            assert (cpp_repo / TEST_PATH).read_bytes() == STRONGER.encode()
            assert result.test_integrity["status"] == "passed"
            assert any(event.kind == "test_integrity_verified" for event in trace.events)
        else:
            assert result.state == TaskState.FAILED
            assert result.error.startswith("validation_integrity:")
            assert (cpp_repo / TEST_PATH).read_bytes() == OLD.encode()
            assert not service._changes.snapshots
    finally:
        service.close()


@pytest.mark.asyncio
async def test_test_approval_cannot_expand_task_spec_or_plan(cpp_repo, rg_exe, git_exe):
    service, _ = make_service(cpp_repo, rg_exe, git_exe, [], exact_files=())
    try:
        pending = await service.start(str(cpp_repo), "Edit test")
        with pytest.raises(ForgePilotError, match="exact TaskSpec"):
            service.approve(pending.task_id, 1, approved_test_files=(TEST_PATH,))
        assert service.state == TaskState.WAITING_APPROVAL
        assert service._runtime._grant is None
        assert service._extra_test_approval == ()
    finally:
        service.close()
    service, _ = make_service(cpp_repo, rg_exe, git_exe, [], paths=("main.cpp",))
    try:
        pending = await service.start(str(cpp_repo), "Edit production source")
        with pytest.raises(ForgePilotError, match="files in this plan"):
            service.approve(pending.task_id, 1, approved_test_files=(TEST_PATH,))
        assert service._runtime._grant is None
    finally:
        service.close()


@pytest.mark.asyncio
async def test_new_plan_requires_fresh_separate_test_approval(cpp_repo, rg_exe, git_exe):
    service, _ = make_service(cpp_repo, rg_exe, git_exe, [
        reply(calls=[call("repository_create_file", json.dumps({"path": "extra.cpp", "content": "int helper;"}))]),
        reply(json.dumps(proposal(TEST_PATH, "extra.cpp"))), patch_test(), reply("Done")])
    try:
        first = await service.start(str(cpp_repo), "Strengthen test and add helper")
        service.approve(first.task_id, 1, approved_test_files=(TEST_PATH,))
        second = await service.execute(first.task_id)
        assert second.state == TaskState.WAITING_APPROVAL and second.plan_version == 2
        assert not (cpp_repo / "extra.cpp").exists()
        approved = service.approve(first.task_id, 2)
        assert approved.test_integrity["approved_test_files"] == []
        final = await service.execute(first.task_id)
        assert final.state == TaskState.FAILED
        assert final.error.startswith("validation_integrity:")
        assert (cpp_repo / TEST_PATH).read_bytes() == OLD.encode()
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete", "rename", "assertion", "skip"])
async def test_final_verification_detects_test_tampering_without_build_config(
        cpp_repo, rg_exe, git_exe, tamper):
    service, _ = make_service(cpp_repo, rg_exe, git_exe, [reply("Done")], paths=("main.cpp",))
    try:
        pending = await service.start(str(cpp_repo), "Change production")
        service.approve(pending.task_id, 1)
        target = cpp_repo / TEST_PATH
        if tamper == "delete":
            target.unlink()
        elif tamper == "rename":
            target.rename(cpp_repo / "tests/renamed.cpp")
        elif tamper == "assertion":
            target.write_bytes(b"void check(){ EXPECT_TRUE(true); }\n")
        else:
            target.write_bytes(b"void check(){ GTEST_SKIP(); }\n")
        final = await service.execute(pending.task_id)
        assert final.state == TaskState.FAILED
        assert final.error.startswith("validation_integrity:")
        assert final.final_result is None
    finally:
        service.close()


@pytest.mark.asyncio
async def test_approval_metadata_is_a_detached_copy(cpp_repo, rg_exe, git_exe):
    service, _ = make_service(cpp_repo, rg_exe, git_exe, [patch_test(), reply("Done")])
    try:
        pending = await service.start(str(cpp_repo), "Edit test")
        approved = service.approve(pending.task_id, 1)
        approved.test_integrity["approved_test_files"].append(TEST_PATH)
        approved.test_integrity["test_files"].clear()
        final = await service.execute(pending.task_id)
        assert final.state == TaskState.FAILED
        assert final.error.startswith("validation_integrity:")
        assert (cpp_repo / TEST_PATH).read_bytes() == OLD.encode()
    finally:
        service.close()
