"""Distinct NTFS Unicode names must never share grants, CAS, or snapshots."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import ScriptedLLM
from forgepilot.changes import ChangeSet
from forgepilot.context import TaskContext, TaskState
from forgepilot.errors import ForgePilotError
import forgepilot.file_access as access
from forgepilot.path_identity import path_key, same_path, path_within
from forgepilot.planning import validate_plan
from forgepilot.repository import Repository, RepositoryCache
import forgepilot.repository as repository_module
from forgepilot.runtime import AgentRuntime
from forgepilot.tools import ToolRegistry
from forgepilot.trace import MemoryTrace
from test_planning import proposal


PAIRS = [("straße.cpp", "strasse.cpp"), ("İ.cpp", "i\u0307.cpp")]


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture(params=PAIRS, ids=["sharp-s", "dotted-i"])
def distinct_files(tmp_path, request):
    left, right = request.param
    (tmp_path / left).write_bytes(b"int value = 1;\n")
    (tmp_path / right).write_bytes(b"int value = 2;\n")
    assert (tmp_path / left).stat().st_ino != (tmp_path / right).stat().st_ino
    assert path_key(left) != path_key(right)
    return tmp_path.resolve(), left, right


def runtime_for(root, *approved):
    repository = Repository(root, "unused")
    trace = MemoryTrace()
    runtime = AgentRuntime(ScriptedLLM([]), ToolRegistry(), trace, repository=repository)
    context = TaskContext(str(root), "controlled Unicode patch")
    context.state = TaskState.PLANNING
    runtime._context, runtime._task_id = context, context.task_id
    runtime.changes = ChangeSet(repository, runtime.ensure_write, trace, context.task_id)
    runtime._install_plan(context, proposal(*approved))
    runtime.changes.capture_scope()
    runtime.approve(context.task_id, 1)
    return runtime, context


@pytest.mark.asyncio
async def test_unicode_twin_cannot_borrow_approved_file_capability(distinct_files):
    root, left, right = distinct_files
    runtime, context = runtime_for(root, left)
    assert runtime.is_file_approved(context.task_id, 1, left)
    assert not runtime.is_file_approved(context.task_id, 1, right)
    with pytest.raises(ForgePilotError, match="approval_required"):
        await runtime.changes.apply_patch(right, sha(b"int value = 2;\n"),
            [{"old_text": "= 2", "new_text": "= 9"}])
    assert (root / right).read_bytes() == b"int value = 2;\n" and not runtime.changes.snapshots
    await runtime.changes.apply_patch(left, sha(b"int value = 1;\n"),
        [{"old_text": "= 1", "new_text": "= 3"}])
    runtime.changes.verify_scope()
    assert set(runtime.changes.snapshots) == {path_key(left)}


@pytest.mark.asyncio
async def test_unicode_files_keep_independent_hashes_cas_snapshots_and_restore(distinct_files):
    root, left, right = distinct_files
    runtime, _ = runtime_for(root, left, right)
    repository = runtime.repository
    left_read = json.loads(await repository.read_file(left))
    right_read = json.loads(await repository.read_file(right))
    assert repository._read_versions == {path_key(left): left_read["sha256"], path_key(right): right_read["sha256"]}
    assert len(repository.audit_manifest()) == 2
    with pytest.raises(ForgePilotError, match="write_conflict"):
        await runtime.changes.apply_patch(right, left_read["sha256"], [{"old_text": "= 2", "new_text": "= 9"}])
    for path, old, new, digest in ((left, "= 1", "= 3", left_read["sha256"]),
                                   (right, "= 2", "= 4", right_read["sha256"])):
        await runtime.changes.apply_patch(path, digest, [{"old_text": old, "new_text": new}])
    assert len(runtime.changes.snapshots) == len(repository.audit_manifest()) == 2
    assert runtime.changes.snapshots[path_key(left)].original == b"int value = 1;\n"
    assert runtime.changes.snapshots[path_key(right)].original == b"int value = 2;\n"
    assert (await runtime.changes.restore())["conflicts"] == 0
    assert (root / left).read_bytes() == b"int value = 1;\n"
    assert (root / right).read_bytes() == b"int value = 2;\n"


def test_scope_inventory_cannot_hide_tampered_unicode_twin(distinct_files):
    root, left, right = distinct_files
    changes = ChangeSet(Repository(root, "unused"), lambda path: None, MemoryTrace(), "unicode")
    changes.capture_scope()
    assert len(changes._scope_baseline) == 2
    (root / left).write_bytes(b"unapproved external mutation")
    with pytest.raises(ForgePilotError, match="scope_violation"):
        changes.verify_scope()
    assert (root / right).read_bytes() == b"int value = 2;\n"


def test_distinct_unicode_files_are_not_duplicate_plan_entries(distinct_files):
    root, left, right = distinct_files
    plan = validate_plan(proposal(left, right), Repository(root, "unused").normalize_plan_path)
    assert len(plan.files_to_modify) == 2


@pytest.mark.parametrize("boundary", ["read", "pinned"])
def test_opened_handle_cannot_be_relabelled_as_unicode_twin(distinct_files, monkeypatch, boundary):
    root, left, right = distinct_files
    repository = Repository(root, "unused")
    if boundary == "read":
        monkeypatch.setattr(repository_module, "opened_path", lambda fd: root / right)
        with pytest.raises(ForgePilotError, match="repository_boundary"):
            repository._read(left)
    else:
        monkeypatch.setattr(access, "opened_path", lambda fd: root / right)
        with pytest.raises(ForgePilotError, match="repository_boundary"):
            with access.pinned_file(repository, left):
                pytest.fail("Unicode path identity alias accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("names", PAIRS)
async def test_repository_cache_never_reuses_a_unicode_twin_root(tmp_path, names, git_exe, rg_exe):
    roots = [tmp_path / name.removesuffix(".cpp") for name in names]
    for index, root in enumerate(roots):
        root.mkdir()
        (root / "main.cpp").write_text(str(index), encoding="utf-8")
        subprocess.run([git_exe, "init", "-q", str(root)], check=True, capture_output=True)
    cache = RepositoryCache()
    try:
        first, first_hit = await cache.open(str(roots[0]), rg_executable=rg_exe, git_executable=git_exe)
        second, second_hit = await cache.open(str(roots[1]), rg_executable=rg_exe, git_executable=git_exe)
        assert not first_hit and not second_hit
        assert first.root.stat().st_ino != second.root.stat().st_ino
        assert second._read("main.cpp")[0] == "1"
    finally:
        await cache.aclose()


@pytest.mark.asyncio
async def test_existing_unicode_case_alias_resolves_to_physical_file_casing(tmp_path):
    target = tmp_path / "Ü.cpp"
    target.write_bytes(b"int value = 1;\n")
    repository = Repository(tmp_path.resolve(), "unused")
    assert repository.normalize_plan_path("ü.cpp") == "Ü.cpp"
    assert json.loads(await repository.read_file("ü.cpp"))["sha256"] == sha(target.read_bytes())
    runtime, context = runtime_for(tmp_path.resolve(), "Ü.cpp")
    assert runtime.is_file_approved(context.task_id, 1, "ü.cpp")
    await runtime.changes.apply_patch("ü.cpp", sha(target.read_bytes()), [{"old_text": "= 1", "new_text": "= 2"}])
    assert target.read_bytes() == b"int value = 2;\n"


def test_conservative_path_helpers_preserve_unicode_and_posix_case(monkeypatch):
    assert path_key("Dir\\ASCII.CPP") == "dir/ascii.cpp"
    for left, right in PAIRS:
        assert not same_path(Path(left), Path(right))
        assert not path_within(Path(left) / "main.cpp", Path(right))
    assert same_path(None, None) is False
    assert same_path(Path("DIR"), Path("dir"))
    assert path_within("C:/work/main.cpp", "c:/WORK")
    assert path_within("C:/work", "c:/WORK")
    with monkeypatch.context() as scope:
        scope.setattr(os, "name", "posix")
        assert path_key("A.cpp") != path_key("a.cpp")
@pytest.mark.skipif(os.name != "nt", reason="Windows mutex namespace")
def test_ascii_repository_mutex_identity_keeps_legacy_namespace(tmp_path):
    from forgepilot.file_access import RepositoryLock
    root = Path(tmp_path.anchor) / "ForgePilot-ASCII-Namespace"
    assert RepositoryLock(root).key == str(root.resolve()).casefold()

