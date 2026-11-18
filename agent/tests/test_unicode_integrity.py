"""Distinct NTFS names must never share a test-file approval through casefold."""

import hashlib
import json
import os

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.changes import ChangeSet
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.integrity import IntegrityMonitor, IntegrityTaskSpec
from forgepilot.path_identity import path_key
from forgepilot.repository import Repository
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from test_planning import proposal


PAIRS = (("tests/ß.cpp", "tests/ss.cpp"), ("tests/İ.cpp", "tests/i\u0307.cpp"))
CONTENTS = (b"void check(){ EXPECT_EQ(answer(),1); }\n", b"void check(){ EXPECT_EQ(answer(),2); }\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def create_distinct_tests(root, pair):
    (root / "tests").mkdir(exist_ok=True)
    for path, content in zip(pair, CONTENTS):
        (root / path).write_bytes(content)
    assert not os.path.samefile(root / pair[0], root / pair[1])
    assert pair[0].casefold() == pair[1].casefold()
    assert path_key(pair[0]) != path_key(pair[1])


def stronger(old):
    return old.decode().replace("; }", "; EXPECT_NE(answer(),0); }")


def guarded_changes(root, monitor, pair):
    def authorize(path):
        if path_key(path) not in {path_key(item) for item in pair}:
            raise ForgePilotError("approval_required", "Outside exact source scope")
    changes = ChangeSet(monitor.repository, authorize, MemoryTrace(), "unicode-integrity")
    changes.content_guard = monitor.guard
    changes.capture_scope()
    return changes


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("approved_index", [0, 1])
@pytest.mark.asyncio
async def test_approval_for_one_unicode_test_cannot_edit_its_distinct_casefold_alias(tmp_path, pair, approved_index):
    create_distinct_tests(tmp_path, pair)
    approved, denied = pair[approved_index], pair[1 - approved_index]
    repo = Repository(tmp_path.resolve(), "unused")
    monitor = IntegrityMonitor(repo, IntegrityTaskSpec("bug_fix", (approved,)))
    report = monitor.capture(1, approved_test_files=(approved,))
    assert {item["path"] for item in report["test_files"]} == set(pair)
    assert report["approved_test_files"] == [path_key(approved)]
    assert len(repo.audit_manifest()) == 2
    changes = guarded_changes(tmp_path, monitor, pair)
    old = CONTENTS[1 - approved_index]
    with pytest.raises(ForgePilotError, match="Existing tests are read-only"):
        await changes.apply_patch(denied, sha(old), [{"old_text": old.decode(), "new_text": stronger(old)}])
    assert not changes.snapshots
    assert (tmp_path / denied).read_bytes() == old
    allowed_old = CONTENTS[approved_index]
    await changes.apply_patch(approved, sha(allowed_old),
        [{"old_text": allowed_old.decode(), "new_text": stronger(allowed_old)}])
    assert set(changes.snapshots) == {path_key(approved)}
    assert (tmp_path / denied).read_bytes() == old
    assert monitor.verify()["status"] == "passed"
    changes.verify_scope()


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("requested_index", [0, 1])
def test_extra_test_approval_cannot_expand_unicode_task_spec(tmp_path, pair, requested_index):
    create_distinct_tests(tmp_path, pair)
    requested, other = pair[requested_index], pair[1 - requested_index]
    monitor = IntegrityMonitor(Repository(tmp_path.resolve(), "unused"),
        IntegrityTaskSpec("feature", (requested,)))
    with pytest.raises(ForgePilotError, match="exact TaskSpec"):
        monitor.capture(1, approved_test_files=(other,))
    assert monitor.snapshot is None


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.asyncio
async def test_individually_approved_unicode_tests_keep_independent_hashes_and_snapshots(tmp_path, pair):
    create_distinct_tests(tmp_path, pair)
    monitor = IntegrityMonitor(Repository(tmp_path.resolve(), "unused"), IntegrityTaskSpec("feature", pair))
    report = monitor.capture(1, approved_test_files=pair)
    assert {item["path"]: item["sha256"] for item in report["test_files"]} == dict(zip(pair, map(sha, CONTENTS)))
    changes = guarded_changes(tmp_path, monitor, pair)
    for path, old in zip(pair, CONTENTS):
        await changes.apply_patch(path, sha(old), [{"old_text": old.decode(), "new_text": stronger(old)}])
    assert len(changes.snapshots) == 2
    assert set(changes.snapshots) == {path_key(path) for path in pair}
    assert {item["path"] for item in changes.diff()["files"]} == set(pair)
    assert monitor.verify()["status"] == "passed"
    changes.verify_scope()
    restored = await changes.restore()
    assert restored["conflicts"] == 0
    assert len(restored["files"]) == 2
    assert all(item["status"] == "restored" for item in restored["files"])
    for path, old in zip(pair, CONTENTS):
        assert (tmp_path / path).read_bytes() == old


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.asyncio
async def test_final_inventory_cannot_authorize_tampered_unicode_alias(tmp_path, pair):
    create_distinct_tests(tmp_path, pair)
    monitor = IntegrityMonitor(Repository(tmp_path.resolve(), "unused"), IntegrityTaskSpec("feature", (pair[0],)))
    monitor.capture(1, approved_test_files=(pair[0],))
    (tmp_path / pair[1]).write_text(stronger(CONTENTS[1]), encoding="utf-8")
    with pytest.raises(ForgePilotError, match="hash changed without separate exact approval"):
        monitor.verify()


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.asyncio
async def test_runtime_source_scope_for_both_does_not_expand_exact_test_permission(cpp_repo, rg_exe, git_exe, pair):
    create_distinct_tests(cpp_repo, pair)
    old = CONTENTS[1]
    llm = ScriptedLLM([reply("Inspect both test files"), reply(json.dumps(proposal(*pair))),
        reply(calls=[call("repository_read_range", json.dumps({"path": pair[1]}), "read-alias"),
            call("repository_apply_patch", json.dumps({"path": pair[1], "expected_hash": sha(old),
                "edits": [{"old_text": old.decode(), "new_text": stronger(old)}]}), "patch-alias")]),
        reply("Done")])
    service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=MemoryTrace(),
        task_spec=IntegrityTaskSpec("feature", (pair[0],)))
    try:
        pending = await service.start(str(cpp_repo), "Modify the specifically approved test")
        assert pending.state == TaskState.WAITING_APPROVAL
        service.approve(pending.task_id, 1, approved_test_files=(pair[0],))
        result = await service.execute(pending.task_id)
        assert result.state == TaskState.FAILED
        assert result.error.startswith("validation_integrity:")
        assert not service._changes.snapshots
        for path, content in zip(pair, CONTENTS):
            assert (cpp_repo / path).read_bytes() == content
    finally:
        service.close()
