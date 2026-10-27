import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.file_access import RepositoryLock
import forgepilot.file_access as file_access
import forgepilot.changes as changes_module
from forgepilot.git import GitRepository
from forgepilot.repository import MAX_FILE_BYTES
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from test_planning import proposal, contents


def write(path, content, id="write"):
    return call("write_file", json.dumps({"path": path, "content": content}), id)


def service_for(rg_exe, git_exe, responses, *, paths=("main.cpp",), trace=None, redactor=None):
    llm = ScriptedLLM([reply("Analyze existing user changes"), reply(json.dumps(proposal(*paths))), *responses])
    trace = trace or MemoryTrace(redactor)
    return TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace, redactor=redactor), trace


@pytest.fixture
def captured_baselines(monkeypatch):
    baselines = []
    capture = GitRepository.capture
    async def record_capture(repository):
        baseline = await capture(repository)
        baselines.append(baseline)
        return baseline
    monkeypatch.setattr(GitRepository, "capture", record_capture)
    return baselines


def git(root, exe, *args):
    return subprocess.run([exe, "--no-optional-locks", "-C", str(root), *args], capture_output=True, check=True).stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_write", [False, True])
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_partial_staged_write_preserves_destination_and_prior_snapshot(cpp_repo, rg_exe, git_exe, monkeypatch, previous_write, tool_name):
    # Exercise I/O with supported uniform newlines, independent of checkout EOLs.
    (cpp_repo / "main.cpp").write_bytes(b"original user content\r\n")
    service, trace = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    original = (cpp_repo / "main.cpp").read_bytes()
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        if previous_write:
            await service._changes.write_file("main.cpp", "previous Agent content")
        before = (cpp_repo / "main.cpp").read_bytes()
        events = len([e for e in trace.events if e.kind == "file_changed"])

        def fail_partway(stream, data):
            stream.seek(0)
            stream.write(data[:3])
            stream.flush()
            raise OSError("simulated partial write")

        with monkeypatch.context() as patch:
            patch.setattr(file_access, "write_staged_bytes", fail_partway)
            with pytest.raises(ForgePilotError, match="file_access"):
                if tool_name == "write_file":
                    await service._changes.write_file("main.cpp", "new replacement")
                else:
                    await service._changes.edit_file("main.cpp", [{"old_text": before.decode(), "new_text": "new replacement"}])
        assert (cpp_repo / "main.cpp").read_bytes() == before
        assert not list(cpp_repo.glob(".forgepilot-patch-*.tmp"))
        if previous_write:
            snapshot = service._changes.snapshots["main.cpp"]
            assert snapshot.original == original and snapshot.last_written == before
        else:
            assert not service._changes.snapshots
        assert len([e for e in trace.events if e.kind == "file_changed"]) == events
        service.cancel()
        assert (await service.restore(pending.task_id))["conflicts"] == 0
        assert (cpp_repo / "main.cpp").read_bytes() == original
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_cancel_at_write_checkpoint_leaves_no_snapshot(cpp_repo, rg_exe, git_exe, tool_name):
    service, trace = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    original = (cpp_repo / "main.cpp").read_bytes()
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        mutation = (service._changes.write_file("main.cpp", "cancelled") if tool_name == "write_file" else
            service._changes.edit_file("main.cpp", [{"old_text": original.decode(), "new_text": "cancelled"}]))
        task = asyncio.create_task(mutation)
        await asyncio.sleep(0)  # The write is now suspended at its pre-handle checkpoint.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (cpp_repo / "main.cpp").read_bytes() == original
        assert not service._changes.snapshots
        assert not any(e.kind == "file_changed" for e in trace.events)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_after_approval", [False, True])
async def test_first_write_rejects_external_change_and_reread_recovers(cpp_repo, rg_exe, git_exe, change_after_approval):
    service, trace = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    try:
        registry = service._runtime.registry
        assert (await registry.execute("read_file", {"path": ".\\MAIN.CPP"})).success
        if change_after_approval:
            service.approve(pending.task_id, 1)
        external = b"// external version\r\n"
        (cpp_repo / "main.cpp").write_bytes(external)
        if not change_after_approval:
            service.approve(pending.task_id, 1)
        result = await registry.execute("write_file", {"path": "main.cpp", "content": "stale replacement"})
        assert not result.success and result.error.startswith("write_conflict")
        assert (cpp_repo / "main.cpp").read_bytes() == external
        assert not service._changes.snapshots
        assert not any(event.kind == "file_changed" for event in trace.events)
        assert (await registry.execute("read_file", {"path": "main.cpp"})).success
        assert (await registry.execute("write_file", {"path": ".\\MAIN.CPP", "content": "fresh replacement"})).success
        assert service._changes.snapshots["main.cpp"].original == external
        service.cancel()
        assert (await service.restore(pending.task_id))["conflicts"] == 0
        assert (cpp_repo / "main.cpp").read_bytes() == external
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, error", [
    ("unread", "write_requires_read"),
    ("new_file_race", "write_requires_read"),
    ("deleted", "write_conflict"),
    ("search_only", "write_requires_read"),
    ("search_after_edit", "write_conflict"),
    ("truncated_reread", "write_requires_read"),
    ("bom_removed", "write_conflict"),
    ("crlf_changed", "write_conflict"),
])
async def test_first_write_preserves_unread_or_changed_files(cpp_repo, rg_exe, git_exe, scenario, error):
    path = "new.cpp" if scenario == "new_file_race" else "main.cpp"
    target = cpp_repo / path
    original = b"\xef\xbb\xbf// original\r\n"
    if scenario != "new_file_race":
        target.write_bytes(original)
    service, trace = service_for(rg_exe, git_exe, [], paths=(path,))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        repo = service._changes.repository
        if scenario not in {"unread", "new_file_race", "search_only"}:
            await repo.read_file(path)
        service.approve(pending.task_id, 1)
        if scenario in {"new_file_race", "search_after_edit"}:
            target.write_bytes(b"// user version\n")
        elif scenario == "deleted":
            target.unlink()
        elif scenario == "truncated_reread":
            target.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
            result = json.loads(await repo.read_file(path))
            assert "file_byte_limit" in result["limits"]
            target.write_bytes(original)  # An old baseline must not become valid again.
        elif scenario == "bom_removed":
            target.write_bytes(original[3:])
        elif scenario == "crlf_changed":
            target.write_bytes(original.replace(b"\r\n", b"\n"))
        if scenario in {"search_only", "search_after_edit"}:
            assert json.loads(await repo.search_code("//", path))["matches"]
        before = target.read_bytes() if target.exists() else None
        result = await service._runtime.registry.execute("write_file", {"path": path, "content": "replacement"})
        assert not result.success and result.error.startswith(error)
        assert (target.read_bytes() if target.exists() else None) == before
        assert not service._changes.snapshots
        assert not any(event.kind == "file_changed" for event in trace.events)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_new_file_race_at_create_new_never_overwrites(cpp_repo, rg_exe, git_exe, monkeypatch):
    service, _ = service_for(rg_exe, git_exe, [], paths=("raced.cpp",))
    pending = await service.start(str(cpp_repo), "modify")
    original_open = file_access.FileTransaction.open_handle
    def race(transaction, path, access, share, disposition, flags):
        if path.name == "raced.cpp" and disposition == 1:
            path.write_bytes(b"user won creation")
        return original_open(transaction, path, access, share, disposition, flags)
    monkeypatch.setattr(file_access.FileTransaction, "open_handle", race)
    try:
        service.approve(pending.task_id, 1)
        with pytest.raises(ForgePilotError, match="file_access"):
            await service._changes.write_file("raced.cpp", "agent")
        assert (cpp_repo / "raced.cpp").read_bytes() == b"user won creation"
        assert not service._changes.snapshots
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_baseline_snapshot_task_diff_and_restore_preserve_user_changes(cpp_repo, rg_exe, git_exe, committed, captured_baselines):
    if committed:
        git(cpp_repo, git_exe, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture baseline")
        (cpp_repo / "staged.txt").write_bytes(b"staged user content\r\n")
        git(cpp_repo, git_exe, "add", "staged.txt")
    before = contents(cpp_repo)
    original = (cpp_repo / "main.cpp").read_bytes()
    notes = (cpp_repo / "notes.txt").read_text()
    updated = original.decode() + "// Agent addition\n"
    service, trace = service_for(rg_exe, git_exe, [
        reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-main"),
                     call("read_file", '{"path":"notes.txt"}', "read-notes"),
                     write(".\\MAIN.CPP", updated), write("notes.txt", notes + "\nAgent note\n", "notes"),
                     write("src/New.cpp", "// new Agent file\n", "new")]),
        reply(calls=[call("git_diff", "{}", "diff"), call("git_status", "{}", "status")]), reply("Changes ready")],
        paths=("main.cpp", "notes.txt", "src/New.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        assert pending.state == TaskState.WAITING_APPROVAL and contents(cpp_repo) == before
        assert len(captured_baselines) == 1
        baseline = captured_baselines[0]
        assert baseline.head_state == ("present" if committed else "unborn")
        assert (baseline.head is not None) == committed
        assert b"main.cpp" in baseline.status and b"notes.txt" in baseline.status
        assert b"existing unstaged user change" in baseline.diff
        assert baseline.cached_diff
        approved = service.approve(pending.task_id, 1)
        assert approved.state == TaskState.MODIFYING and contents(cpp_repo) == before
        completed = await service.execute(pending.task_id)
        assert completed.state == TaskState.COMPLETED, completed.error
        assert completed.outcome == "modification_completed_unvalidated"
        assert len(completed.task_diff["files"]) == 3
        snap = service._changes.snapshots["main.cpp"]
        assert snap.original == original and snap.last_written == updated.encode()
        main_diff = completed.task_diff["files"][0]["diff"]
        assert "+// Agent addition" in main_diff
        assert "+// existing unstaged user change" not in main_diff
        assert completed.task_diff["files"][2]["created"]
        assert "--- /dev/null" in completed.task_diff["files"][2]["diff"]
        assert (cpp_repo / ".git/index").read_bytes() == before[".git/index"]
        assert all(r["success"] for r in completed.tool_results)
        assert not any(f["conflict"] for f in completed.task_diff["files"])
        restored = await service.restore(pending.task_id)
        assert restored["conflicts"] == 0
        assert contents(cpp_repo) == before
        assert all(f["restored"] for f in service.task_diff(pending.task_id)["files"])
        assert all(f["status"] == "already_restored" for f in (await service.restore(pending.task_id))["files"])
        assert len([e for e in trace.events if e.kind == "task_finished"]) == 1
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_no_writes_before_approval_or_after_terminal(cpp_repo, rg_exe, git_exe, tool_name):
    before = contents(cpp_repo)
    service, _ = service_for(rg_exe, git_exe, [reply("nothing")])
    pending = await service.start(str(cpp_repo), "modify")
    try:
        registry = service._runtime.registry
        assert {s.name for s in registry.get_specs()} == {
            "repository_list_files", "repository_read_range", "repository_search_text",
            "repository_get_symbol", "repository_find_references", "repository_read_cmake_target",
            "repository_git_diff"}
        assert (await registry.execute("read_file", {"path": "main.cpp"})).success
        arguments = {"path": "main.cpp", **({"content": "bad"} if tool_name == "write_file" else
            {"edits": [{"old_text": "int", "new_text": "long"}]})}
        denied = await registry.execute(tool_name, arguments)
        assert not denied.success and denied.error.startswith("policy_denied")
        service.approve(pending.task_id, 1)
        assert {"repository_apply_patch", "repository_create_file", "repository_delete_new_file"} <= {
            spec.name for spec in registry.get_specs()}
        assert service.cancel()
        denied = await registry.execute(tool_name, arguments)
        assert not denied.success
        assert contents(cpp_repo) == before
        with pytest.raises(ForgePilotError, match="invalid_state"):
            await service.execute(pending.task_id)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_new_scope_pauses_replans_and_requires_new_version(cpp_repo, rg_exe, git_exe):
    original_bytes = (cpp_repo / "main.cpp").read_bytes()
    old = (cpp_repo / "main.cpp").read_text()
    service, trace = service_for(rg_exe, git_exe, [
        reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-main"),
                     write("main.cpp", old + "// first\n"), write("Extra.cpp", "extra\n", "extra"),
                     write("main.cpp", "must not execute", "deferred")]),
        reply(json.dumps(proposal("main.cpp", "Extra.cpp"))),
        reply(calls=[write("Extra.cpp", "extra\n")]), reply("done")])
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        revised = await service.execute(pending.task_id)
        assert revised.state == TaskState.WAITING_APPROVAL, revised.error
        assert revised.plan_version == 2 and not revised.approved_files
        assert not (cpp_repo / "Extra.cpp").exists()
        assert (cpp_repo / "main.cpp").read_text() == old + "// first\n"
        assert not service.is_file_approved(pending.task_id, 1, "main.cpp")
        with pytest.raises(ForgePilotError, match="stale_plan"):
            service.approve(pending.task_id, 1)
        service.approve(pending.task_id, 2)
        done = await service.execute(pending.task_id)
        assert done.state == TaskState.COMPLETED and (cpp_repo / "Extra.cpp").read_bytes() == b"extra\n"
        assert service._changes.snapshots["main.cpp"].original == original_bytes
        assert done.steps == 6
        assert [e.data["state"] for e in trace.events if e.kind == "task_state_changed"] == [
            "analyzing", "planning", "waiting_approval", "modifying", "planning", "waiting_approval", "modifying", "completed"]
        assert (await service.restore(pending.task_id))["conflicts"] == 0
    finally:
        service.close()


@pytest.mark.asyncio
async def test_trusted_revision_and_reject_preserves_existing_diff(cpp_repo, rg_exe, git_exe):
    service, _ = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        await service._changes.write_file("main.cpp", "changed\n")
        with pytest.raises(ForgePilotError, match="files_to_modify"):
            service.replace_plan(pending.task_id, 1, proposal("Extra.cpp"))
        revised = service.replace_plan(pending.task_id, 1, proposal("main.cpp", "Extra.cpp"))
        assert revised.state == TaskState.WAITING_APPROVAL and revised.plan_version == 2
        result = service.reject(pending.task_id, 2)
        assert result.state == TaskState.CANCELLED and result.task_diff["files"]
        assert (cpp_repo / "main.cpp").read_bytes() == b"changed\n"
        assert (await service.restore(pending.task_id))["conflicts"] == 0
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.cpp", "/tmp/file.cpp", "D:\\escape.cpp", "\\\\host\\share\\x",
    "src/../../x", "main.cpp:stream", ".git/config", ".env", "build/new.cpp", "NUL", "src/x. "])
async def test_invalid_write_paths_cannot_escape(cpp_repo, rg_exe, git_exe, path):
    service, _ = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    before = contents(cpp_repo)
    try:
        service.approve(pending.task_id, 1)
        result = await service._runtime.registry.execute("write_file", {"path": path, "content": "bad"})
        assert not result.success and result.error.startswith("repository_")
        assert contents(cpp_repo) == before
    finally:
        service.close()


@pytest.mark.asyncio
async def test_link_junction_and_hardlink_escape_denied(cpp_repo, rg_exe, git_exe, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value.cpp").write_bytes(b"outside")
    (cpp_repo / "folder").mkdir()
    (cpp_repo / "folder/value.cpp").write_bytes(b"inside")
    service, _ = service_for(rg_exe, git_exe, [], paths=("folder/value.cpp", "hard.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        (cpp_repo / "folder/value.cpp").unlink()
        (cpp_repo / "folder").rmdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(cpp_repo / "folder"), str(outside)], check=True, capture_output=True)
        os.link(outside / "value.cpp", cpp_repo / "hard.cpp")
        for path in ("folder/value.cpp", "hard.cpp"):
            result = await service._runtime.registry.execute("write_file", {"path": path, "content": "bad"})
            assert not result.success and result.error.startswith("repository_boundary"), result
        assert (outside / "value.cpp").read_bytes() == b"outside"
        assert not service._changes.snapshots
    finally:
        service.close()
        (cpp_repo / "folder").rmdir()  # Only unlink this known test junction, never recurse.


@pytest.mark.asyncio
async def test_restore_conflicts_and_subsequent_writes_preserve_user_edits(cpp_repo, rg_exe, git_exe):
    service, _ = service_for(rg_exe, git_exe, [], paths=("main.cpp", "New.cpp", "Remove.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        for p in ("main.cpp", "New.cpp", "Remove.cpp"):
            await service._changes.write_file(p, "Agent\r\n")
        await service._changes.write_file("main.cpp", "Second Agent version\n")
        (cpp_repo / "main.cpp").write_bytes(b"later user edit\r\n")
        (cpp_repo / "New.cpp").write_bytes(b"user new edit")
        await service._changes.repository.read_file("main.cpp")
        with pytest.raises(ForgePilotError, match="scope_violation"):
            await service._changes.write_file("main.cpp", "override")
        service.cancel()
        assert len(service.task_diff(pending.task_id)["files"]) == 3
        restored = await service.restore(pending.task_id)
        assert restored["conflicts"] == 2
        assert (cpp_repo / "main.cpp").read_bytes() == b"later user edit\r\n"
        assert (cpp_repo / "New.cpp").read_bytes() == b"user new edit"
        assert not (cpp_repo / "Remove.cpp").exists()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_cancellation_after_first_write_stops_remaining_batch(cpp_repo, rg_exe, git_exe):
    holder = {}
    def on_event(event):
        if event.kind == "file_changed":
            assert holder["service"].cancel()
    service, _ = service_for(rg_exe, git_exe, [reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-main"),
        write("main.cpp", "first"), write("Extra.cpp", "second", "second")])],
                              paths=("main.cpp", "Extra.cpp"), trace=MemoryTrace(on_event=on_event))
    holder["service"] = service
    before = contents(cpp_repo)
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        cancelled = await service.execute(pending.task_id)
        assert cancelled.state == TaskState.CANCELLED
        assert not (cpp_repo / "Extra.cpp").exists()
        assert (cpp_repo / "main.cpp").read_bytes() == b"first"
        assert len(cancelled.task_diff["files"]) == 1
        assert (await service.restore(pending.task_id))["conflicts"] == 0
        assert contents(cpp_repo) == before
    finally:
        service.close()


@pytest.mark.asyncio
async def test_same_repo_lock_spans_review_execution_and_restore(cpp_repo, rg_exe, git_exe):
    first, _ = service_for(rg_exe, git_exe, [])
    second, _ = service_for(rg_exe, git_exe, [])
    pending = await first.start(str(cpp_repo), "modify")
    try:
        denied = await second.start(str(cpp_repo).upper(), "modify")
        assert denied.state == TaskState.FAILED and denied.error.startswith("repository_busy")
        # A separate Python process must also fail; thread-only locking is insufficient.
        code = "from pathlib import Path; from forgepilot.file_access import RepositoryLock; from forgepilot.errors import ForgePilotError; import sys\ntry: RepositoryLock(Path(sys.argv[1])).acquire()\nexcept ForgePilotError as e: print(e.code); sys.exit(7)"
        proc = subprocess.run([sys.executable, "-c", code, str(cpp_repo)], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 7 and proc.stdout.strip() == "repository_busy", proc.stderr
        first.cancel()
        next_task = await second.start(str(cpp_repo), "modify")
        assert next_task.state == TaskState.WAITING_APPROVAL
        with pytest.raises(ForgePilotError, match="repository_busy"):
            await first.restore(pending.task_id)
        second.cancel()
        assert (await first.restore(pending.task_id))["conflicts"] == 0
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_baseline_never_executes_git_filters_or_leaks_key(cpp_repo, rg_exe, git_exe, captured_baselines):
    marker = cpp_repo / "executed.txt"
    git(cpp_repo, git_exe, "config", "filter.evil.clean", "echo UNSAFE > executed.txt")
    git(cpp_repo, git_exe, "config", "filter.evil.process", "echo UNSAFE > executed.txt")
    git(cpp_repo, git_exe, "config", "filter.evil.required", "true")
    git(cpp_repo, git_exe, "config", "diff.evil.command", "echo UNSAFE > executed.txt")
    git(cpp_repo, git_exe, "config", "diff.evil.textconv", "echo UNSAFE > executed.txt")
    (cpp_repo / ".gitattributes").write_text("*.cpp filter=evil diff=evil\n")
    key = "phase3-test-secret-value"
    with (cpp_repo / "main.cpp").open("a") as stream:
        stream.write(key)
    service, trace = service_for(rg_exe, git_exe, [], redactor=Redactor((key,)))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        assert pending.state == TaskState.WAITING_APPROVAL, pending.error
        assert not marker.exists()
        assert len(captured_baselines) == 1
        assert key.encode() in captured_baselines[0].diff
        assert key not in json.dumps(asdict(pending), default=str)
        assert key not in json.dumps([asdict(e) for e in trace.events], default=str)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_byte_exact_empty_bom_files_and_missing_file_conflict(cpp_repo, rg_exe, git_exe):
    (cpp_repo / "empty.cpp").write_bytes(b"")
    original = b"\xef\xbb\xbf// BOM\r\n// line 2\r\n"
    (cpp_repo / "bom.cpp").write_bytes(original)
    service, _ = service_for(rg_exe, git_exe, [], paths=("empty.cpp", "bom.cpp", "new-empty.cpp", "deleted.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        for path in ("empty.cpp", "bom.cpp"):
            await service._changes.repository.read_file(path)
        for path in ("empty.cpp", "bom.cpp", "deleted.cpp"):
            await service._changes.write_file(path, "new\n")
        await service._changes.write_file("new-empty.cpp", "")
        (cpp_repo / "deleted.cpp").unlink()
        with pytest.raises(ForgePilotError, match="write_conflict"):
            await service._changes.write_file("deleted.cpp", "do not recreate")
        assert not (cpp_repo / "deleted.cpp").exists()
        service.cancel()
        restored = await service.restore(pending.task_id)
        assert restored["conflicts"] == 1
        assert (cpp_repo / "empty.cpp").is_file() and (cpp_repo / "empty.cpp").read_bytes() == b""
        assert (cpp_repo / "bom.cpp").read_bytes() == original
        assert not (cpp_repo / "new-empty.cpp").exists()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_restore_denies_replaced_parent_link(cpp_repo, rg_exe, git_exe, tmp_path):
    service, _ = service_for(rg_exe, git_exe, [], paths=("dir/file.cpp",))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        await service._changes.write_file("dir/file.cpp", "agent")
        service.cancel()
        (cpp_repo / "dir/file.cpp").unlink()
        (cpp_repo / "dir").rmdir()
        outside = tmp_path / "restore-outside"
        outside.mkdir()
        (outside / "file.cpp").write_bytes(b"agent")  # Equal bytes must not bypass the path boundary.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(cpp_repo / "dir"), str(outside)], check=True, capture_output=True)
        restored = await service.restore(pending.task_id)
        assert restored["conflicts"] == 1
        assert (outside / "file.cpp").read_bytes() == b"agent"
    finally:
        service.close()
        (cpp_repo / "dir").rmdir()


@pytest.mark.asyncio
async def test_mutation_pins_ancestors_and_refuses_first_snapshot_truncation(cpp_repo, rg_exe, git_exe, monkeypatch):
    huge = b"x" * 262_145
    (cpp_repo / "huge.cpp").write_bytes(huge)
    service, _ = service_for(rg_exe, git_exe, [], paths=("main.cpp", "huge.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    original_open = file_access.FileTransaction.open_handle
    rename_denied = []
    def guarded(transaction, path, access, share, disposition, flags):
        if path.name == "main.cpp" and access & 0x40000000:
            with pytest.raises(OSError):
                cpp_repo.rename(cpp_repo.with_name("cannot-rename-pinned-root"))
            rename_denied.append(True)
        return original_open(transaction, path, access, share, disposition, flags)
    monkeypatch.setattr(file_access.FileTransaction, "open_handle", guarded)
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        await service._changes.write_file("main.cpp", "safe")
        assert rename_denied == [True]
        with pytest.raises(ForgePilotError, match="snapshot_limit"):
            await service._changes.write_file("huge.cpp", "small")
        assert (cpp_repo / "huge.cpp").read_bytes() == huge
        assert "huge.cpp" not in service._changes.snapshots
    finally:
        service.close()


@pytest.mark.asyncio
async def test_modification_shares_30_step_budget_and_keeps_diff_on_failure(cpp_repo, rg_exe, git_exe):
    service, _ = service_for(rg_exe, git_exe,
        [reply(calls=[call("git_diff", "{}", f"diff_{i}")]) for i in range(27)] +
        [reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-main"), write("main.cpp", "last allowed request")])])
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        failed = await service.execute(pending.task_id)
        assert failed.state == TaskState.FAILED and failed.error.startswith("step_limit")
        assert failed.steps == 30 and len(service.llm.requests) == 30
        assert (cpp_repo / "main.cpp").read_bytes() == b"last allowed request"
        assert failed.task_diff["files"] and service._lock.handle is None
        assert (await service.restore(pending.task_id))["conflicts"] == 0
    finally:
        service.close()


@pytest.mark.asyncio
async def test_baseline_failure_releases_lock_before_llm(cpp_repo, rg_exe, git_exe, monkeypatch):
    async def fail(self):
        raise ForgePilotError("git_output_limit", "baseline too large")
    monkeypatch.setattr(GitRepository, "capture", fail)
    service, _ = service_for(rg_exe, git_exe, [])
    result = await service.start(str(cpp_repo), "modify")
    assert result.state == TaskState.FAILED and result.error.startswith("git_output_limit")
    assert not service.llm.requests and service._lock.handle is None
    lock = RepositoryLock(cpp_repo)
    lock.acquire()
    lock.release()


@pytest.mark.asyncio
async def test_baseline_failure_during_cancel_releases_lock(cpp_repo, rg_exe, git_exe, monkeypatch):
    started = asyncio.Event()
    async def pending(self):
        started.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(GitRepository, "capture", pending)
    service, _ = service_for(rg_exe, git_exe, [])
    task = asyncio.create_task(service.start(str(cpp_repo), "modify"))
    await started.wait()
    assert service.cancel()
    result = await task
    assert result.state == TaskState.CANCELLED and service._lock.handle is None


@pytest.mark.asyncio
async def test_old_tool_handle_cannot_use_a_later_tasks_authority(cpp_repo, rg_exe, git_exe):
    service, _ = service_for(rg_exe, git_exe, [reply("analysis"), reply(json.dumps(proposal("main.cpp")))])
    first = await service.start(str(cpp_repo), "first")
    old_changes = service._changes
    service.cancel()
    next_task = await service.start(str(cpp_repo), "second")
    try:
        service.approve(next_task.task_id, 1)
        with pytest.raises(ForgePilotError, match="repository_lock"):
            await old_changes.write_file("main.cpp", "old task write")
    finally:
        service.close()


@pytest.mark.asyncio
async def test_root_replacement_invalidates_repository_identity(cpp_repo, rg_exe, git_exe):
    service, _ = service_for(rg_exe, git_exe, [])
    pending = await service.start(str(cpp_repo), "modify")
    saved = cpp_repo.with_name("original-fixture-root")
    assert saved.parent == cpp_repo.parent and saved.is_relative_to(cpp_repo.parent)
    try:
        service.approve(pending.task_id, 1)
        cpp_repo.rename(saved)
        cpp_repo.mkdir()
        (cpp_repo / "main.cpp").write_bytes(b"replacement root user file")
        with pytest.raises(ForgePilotError, match="repository_boundary"):
            await service._changes.write_file("main.cpp", "bad")
        assert (cpp_repo / "main.cpp").read_bytes() == b"replacement root user file"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_diff_limits_empty_new_file_and_git_status_exclusions(cpp_repo, rg_exe, git_exe):
    (cpp_repo / ".env").write_bytes(b"unknown sensitive configuration")
    service, _ = service_for(rg_exe, git_exe, [], paths=("main.cpp", "empty-new.cpp"))
    pending = await service.start(str(cpp_repo), "modify")
    try:
        service.approve(pending.task_id, 1)
        await service._changes.repository.read_file("main.cpp")
        await service._changes.write_file("main.cpp", "x" * 12_000)
        await service._changes.write_file("empty-new.cpp", "")
        diff = service.task_diff(pending.task_id)
        assert diff["truncated"] and sum(len(f["diff"]) for f in diff["files"]) <= 12_000
        empty = diff["files"][1]
        assert empty["created"] and empty["original_sha256"] is None and empty["agent_sha256"] is not None
        status = json.loads((await service._runtime.registry.execute("git_status", "{}")).output)
        assert status["omitted"] >= 1 and ".env" not in {e["path"] for e in status["entries"]}
        assert any(e["path"] == "main.cpp" and e["status"] == "AM" for e in status["entries"])
        service.cancel()
        assert (await service.restore(pending.task_id))["conflicts"] == 0
        assert not (cpp_repo / "empty-new.cpp").exists()
    finally:
        service.close()
