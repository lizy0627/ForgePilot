import json
import os
from types import SimpleNamespace
import subprocess

import pytest

from conftest import call, reply
from forgepilot.changes import ChangeSet, FileSnapshot, MAX_SNAPSHOT_BYTES, MAX_TOTAL_SNAPSHOT_BYTES
import forgepilot.changes as changes_module
import forgepilot.file_access as file_access
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.repair import ensure_validation_integrity
from forgepilot.repository import Repository
from forgepilot.tools import ToolRegistry
from forgepilot.tools.change_tools import register_change_tools
from forgepilot.trace import MemoryTrace
from test_changes import service_for, write
from test_planning import proposal


def edit(old, new):
    return {"old_text": old, "new_text": new}


@pytest.fixture
def local(tmp_path):
    repo = Repository(tmp_path.resolve(), "unused-rg")
    trace = MemoryTrace()
    changes = ChangeSet(repo, lambda path: None, trace, "local-edit")
    changes.content_guard = ensure_validation_integrity
    registry = ToolRegistry()

    async def status():
        return "{}"

    register_change_tools(registry, SimpleNamespace(status=status), changes)
    return SimpleNamespace(root=tmp_path, repo=repo, trace=trace, changes=changes, registry=registry)


async def prepare(local, data, path="main.cpp", *, read=True):
    target = local.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    if read:
        await local.repo.read_file(path, 1, 1)
    return target


async def apply(local, edits, path="main.cpp"):
    return await local.registry.execute("edit_file", {"path": path, "edits": edits})


def assert_rejected(local, result, code, target=None, before=None):
    assert not result.success and result.error.startswith(code + ":"), result
    if target is not None:
        assert target.read_bytes() == before
    assert not local.changes.snapshots
    assert not any(e.kind == "file_changed" for e in local.trace.events)


@pytest.mark.asyncio
async def test_large_file_local_read_and_byte_exact_edit(cpp_repo, rg_exe, git_exe):
    prefix = ("// 用户已有内容\n" * 2_000).encode()
    original = prefix + b"int value() { return 1; }\n// untouched tail"
    target = cpp_repo / "main.cpp"
    target.write_bytes(original)
    service, _ = service_for(rg_exe, git_exe, [])
    try:
        pending = await service.start(str(cpp_repo), "modify")
        result = json.loads(await service._changes.repository.read_file("main.cpp", 2_001, 2_001))
        assert result["truncated"] and "line_range" in result["limits"]
        service.approve(pending.task_id, 1)
        output = await service._runtime.registry.execute("edit_file", {
            "path": "main.cpp", "edits": [edit("return 1;", "return 2;")]})
        assert output.success, output
        expected = prefix + b"int value() { return 2; }\n// untouched tail"
        assert target.read_bytes() == expected
        assert json.loads(output.output) == {"path": "main.cpp", "edits_applied": 1,
            "bytes_written": len(expected), "created": False}
        assert service._changes.snapshots["main.cpp"].original == original
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 8])
@pytest.mark.parametrize("reverse", [False, True])
async def test_adjacent_edits_use_original_offsets_without_cascade(local, count, reverse, monkeypatch):
    old = [f"<{i}>" for i in range(count)]
    new = [old[1]] + [f"[{i}]" for i in range(1, count)]
    target = await prepare(local, "".join(old).encode())
    edits = [edit(a, b) for a, b in zip(old, new)]
    calls = []
    original_write = changes_module.write_bytes

    def record_write(stream, data):
        calls.append(data)
        return original_write(stream, data)

    monkeypatch.setattr(changes_module, "write_bytes", record_write)
    result = await apply(local, edits[::-1] if reverse else edits)
    assert result.success, result
    expected = "".join(new).encode()
    assert target.read_bytes() == expected and calls == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("data,edits,code", [
    (b"private text", [edit("missing-sensitive", "x")], "edit_not_found"),
    (b"abc abc", [edit("abc", "x")], "edit_ambiguous"),
    (b"aaa", [edit("aa", "x")], "edit_ambiguous"),
    (b"abcdef", [edit("abcd", "x"), edit("cdef", "y")], "edit_overlap"),
    (b"abcdef", [edit("abc", "x"), edit("abc", "y")], "edit_overlap"),
    (b"abcdef", [edit("abc", "x"), edit("missing-sensitive", "y")], "edit_not_found"),
    (b"abc def def", [edit("abc", "x"), edit("def", "y")], "edit_ambiguous"),
])
async def test_match_failure_is_zero_write_and_does_not_echo_text(local, data, edits, code):
    target = await prepare(local, data)
    result = await apply(local, edits)
    assert_rejected(local, result, code, target, data)
    assert "missing-sensitive" not in result.error and "private text" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"path": "main.cpp", "edits": []},
    {"path": "main.cpp", "edits": [edit("a", "b")] * 9},
    {"path": "main.cpp", "edits": [edit("", "b")]},
    {"path": "main.cpp", "edits": [edit("a" * 6_001, "b")]},
    {"path": "main.cpp", "edits": [edit("a", "b" * 6_001)]},
    {"path": "main.cpp", "edits": [edit("a" * 6_000, "b" * 6_000), edit("x", "")]},
    {"path": "main.cpp", "edits": [edit("a\x00", "b")]},
    {"path": "main.cpp", "edits": [edit("a", "b\x00")]},
    {"path": "main.cpp", "edits": [edit("a\r", "b")]},
    {"path": "main.cpp", "edits": [edit("a", "b\r")]},
    {"path": "main.cpp", "edits": [edit("a", "a")]},
    {"path": "main.cpp", "edits": [edit("a\r\n", "a\n")]},
    {"path": "main.cpp", "edits": [edit("a", "b")], "expected_hash": "extra"},
    {"path": "main.cpp", "edits": [{**edit("a", "b"), "replace_all": True}]},
    {"path": "main.cpp", "edits": [edit(1, "b")]},
    {"path": "main.cpp", "edits": [edit("a", None)]},
    {"path": 1, "edits": [edit("a", "b")]},
    {"path": "main.cpp", "edits": "a"},
    {"path": "main.cpp", "edits": [{"old_text": "a"}]},
])
async def test_strict_arguments_reject_before_mutation(local, arguments):
    target = await prepare(local, b"a")
    result = await local.registry.execute("edit_file", arguments)
    assert_rejected(local, result, "invalid_arguments", target, b"a")


@pytest.mark.asyncio
async def test_json_argument_limit_stays_16384(local):
    target = await prepare(local, "中".encode() * 3_000)
    arguments = json.dumps({"path": "main.cpp", "edits": [edit("中" * 3_000, "x")]})
    assert len(arguments) > 16_384
    result = await local.registry.execute("edit_file", arguments)
    assert_rejected(local, result, "invalid_arguments", target, "中".encode() * 3_000)


@pytest.mark.asyncio
async def test_exact_text_limits_and_total_count_raw_crlf(local):
    target = await prepare(local, b"a" * 6_000)
    result = await apply(local, [edit("a" * 6_000, "b" * 6_000)])
    assert result.success and target.read_bytes() == b"b" * 6_000
    result = await apply(local, [edit("x\r\n" * 2_000, "y\r\n" * 2_000), edit("z", "")])
    assert not result.success and result.error.startswith("invalid_arguments:")
    assert target.read_bytes() == b"b" * 6_000


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new,expected", [("middle", "", b"begin  end"),
    ("middle", "middle inserted", b"begin middle inserted end")])
async def test_deletion_and_anchored_insertion(local, old, new, expected):
    target = await prepare(local, b"begin middle end")
    assert (await apply(local, [edit(old, new)])).success
    assert target.read_bytes() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("size,code", [(MAX_SNAPSHOT_BYTES, "write_limit"),
    (MAX_SNAPSHOT_BYTES + 1, "snapshot_limit")])
async def test_file_size_before_and_after_is_bounded(local, size, code):
    data = b"anchor" + b"x" * (size - 6)
    target = await prepare(local, data)
    result = await apply(local, [edit("anchor", "longer anchor")])
    assert_rejected(local, result, code, target, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["files", "bytes"])
async def test_snapshot_limits_do_not_add_a_snapshot_or_write(local, limit):
    target = await prepare(local, b"old")
    count = 100 if limit == "files" else MAX_TOTAL_SNAPSHOT_BYTES // MAX_SNAPSHOT_BYTES
    data = b"" if limit == "files" else b"x" * MAX_SNAPSHOT_BYTES
    local.changes.snapshots.update({f"saved-{i}": FileSnapshot(f"saved-{i}", data) for i in range(count)})
    result = await apply(local, [edit("old", "new")])
    assert not result.success and result.error.startswith("snapshot_limit:")
    assert target.read_bytes() == b"old" and len(local.changes.snapshots) == count
    assert not any(e.kind == "file_changed" for e in local.trace.events)


@pytest.mark.asyncio
async def test_explicit_read_first_conflict_reread_and_later_conflict(local):
    target = await prepare(local, b"old", read=False)
    local.repo._read("main.cpp")  # Internal reads do not grant a baseline.
    assert_rejected(local, await apply(local, [edit("old", "new")]), "write_requires_read", target, b"old")
    await local.repo.read_file("main.cpp")
    target.write_bytes(b"user")
    assert_rejected(local, await apply(local, [edit("old", "new")]), "write_conflict", target, b"user")
    await local.repo.read_file("main.cpp")
    assert (await apply(local, [edit("user", "agent")])).success
    assert (await apply(local, [edit("agent", "next")])).success  # No reread required.
    assert local.changes.snapshots["main.cpp"].original == b"user"
    target.write_bytes(b"external")
    await local.repo.read_file("main.cpp")
    result = await apply(local, [edit("external", "overwrite")])
    assert not result.success and result.error.startswith("write_conflict:")
    assert target.read_bytes() == b"external"
    assert local.changes.snapshots["main.cpp"].last_written == b"next"
    assert local.changes.diff()["files"][0]["conflict"]
    assert (await local.changes.restore())["conflicts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("trailing", [False, True])
@pytest.mark.parametrize("request_newline", ["\n", "\r\n"])
async def test_newlines_bom_unicode_indent_and_final_newline(local, newline, bom, trailing, request_newline):
    source = "前言🙂" + newline + "  旧值" + newline + "\t后记" + (newline if trailing else "")
    raw = bom + source.encode()
    target = await prepare(local, raw)
    result = await apply(local, [edit("  旧值" + request_newline, "    新值界🙂" + request_newline)])
    assert result.success, result
    expected = raw.replace(("  旧值" + newline).encode(), ("    新值界🙂" + newline).encode())
    assert target.read_bytes() == expected
    assert json.loads(result.output)["bytes_written"] == len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
async def test_no_newline_file_uses_lf_and_bom_is_not_matchable(local, bom):
    target = await prepare(local, bom + "前言旧值后记".encode())
    if bom:
        result = await apply(local, [edit("\ufeff前言", "change")])
        assert_rejected(local, result, "edit_not_found", target, bom + "前言旧值后记".encode())
    result = await apply(local, [edit("旧值", "新值\r\n")])
    assert result.success and target.read_bytes() == bom + "前言新值\n后记".encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"old\r\nkeep\n", b"old\rkeep", b"old\r\nkeep\r"])
async def test_unsupported_file_newlines_are_zero_write(local, data):
    target = await prepare(local, data)
    assert_rejected(local, await apply(local, [edit("old", "new")]), "edit_newline_unsupported", target, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["missing.cpp", "missing/parent/file.cpp"])
async def test_missing_files_and_parents_are_never_created(local, path):
    before = set(local.root.rglob("*"))
    assert_rejected(local, await apply(local, [edit("old", "new")], path), "edit_file_missing")
    assert set(local.root.rglob("*")) == before


@pytest.mark.asyncio
async def test_file_disappears_before_exclusive_open(local, monkeypatch):
    target = await prepare(local, b"old")
    original_open = file_access._open

    def delete_at_parent_pin(api, path, access, share, disposition, flags):
        if path == local.root:
            target.unlink()
        return original_open(api, path, access, share, disposition, flags)

    monkeypatch.setattr(file_access, "_open", delete_at_parent_pin)
    assert_rejected(local, await apply(local, [edit("old", "new")]), "edit_file_missing")
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.cpp", "/outside.cpp", "D:\\outside.cpp", "main.cpp:stream",
    ".git/config", ".env", "build/new.cpp", "NUL", "src/x. ", "folder"])
async def test_protected_and_invalid_paths(local, path):
    (local.root / "folder").mkdir()
    before = set(local.root.rglob("*"))
    result = await apply(local, [edit("old", "new")], path)
    assert not result.success and result.error.startswith("repository_")
    assert not local.changes.snapshots and set(local.root.rglob("*")) == before


@pytest.mark.asyncio
async def test_junction_and_hardlink_are_denied(local, tmp_path_factory):
    outside = tmp_path_factory.mktemp("edit-outside")
    target = outside / "file.cpp"
    target.write_bytes(b"old")
    junction = local.root / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    os.link(target, local.root / "hard.cpp")
    try:
        for path in ("junction/file.cpp", "hard.cpp"):
            result = await apply(local, [edit("old", "new")], path)
            assert_rejected(local, result, "repository_boundary")
        assert target.read_bytes() == b"old"
    finally:
        junction.rmdir()  # Remove only the known test junction, never its target.


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"old\x00", b"old\xff"])
async def test_non_utf8_or_nul_file_is_rejected_even_with_recorded_digest(local, data):
    target = await prepare(local, data, read=False)
    local.repo._read_versions["main.cpp"] = changes_module.digest(data)
    assert_rejected(local, await apply(local, [edit("old", "new")]), "repository_encoding", target, data)


@pytest.mark.asyncio
async def test_partial_io_split_utf8_cannot_be_edited_but_can_be_restored(local, monkeypatch):
    target = await prepare(local, b"aX")

    def fail_partway(stream, data):
        stream.seek(0)
        stream.write(data[:1])
        raise OSError("split UTF-8 character")

    with monkeypatch.context() as patch:
        patch.setattr(changes_module, "write_bytes", fail_partway)
        result = await apply(local, [edit("a", "é")])
    assert not result.success and result.error.startswith("file_access:")
    assert target.read_bytes() == b"\xc3X"
    snapshot = local.changes.snapshots["main.cpp"]
    assert snapshot.original == b"aX" and snapshot.last_written == b"\xc3X"
    result = await apply(local, [edit("X", "Y")])
    assert not result.success and result.error.startswith("repository_encoding:")
    assert target.read_bytes() == b"\xc3X" and snapshot.last_written == b"\xc3X"
    assert not any(e.kind == "file_changed" for e in local.trace.events)
    assert (await local.changes.restore())["conflicts"] == 0 and target.read_bytes() == b"aX"


@pytest.mark.asyncio
async def test_failed_later_batch_keeps_existing_snapshot_and_trace(local):
    target = await prepare(local, b"one two")
    assert (await apply(local, [edit("one", "first")])).success
    snapshot = local.changes.snapshots["main.cpp"]
    result = await apply(local, [edit("first", "second"), edit("missing", "third")])
    assert not result.success and result.error.startswith("edit_not_found:")
    assert target.read_bytes() == b"first two"
    assert len(local.changes.snapshots) == 1 and snapshot.original == b"one two" and snapshot.last_written == b"first two"
    assert len([e for e in local.trace.events if e.kind == "file_changed"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path,data,edits", [
    ("tests/check.cpp", b"int value = 1;", [edit("1", "2")]),
    ("main.cpp", b"assert(value == 1);", [edit("1", "2")]),
    ("main.cpp", b"#define NAAA BBB\n", [edit("AAA", "DE"), edit(" BBB", "BUG")]),
    ("CMakeLists.txt", b"add_test(NAME keep COMMAND program)\n", [edit("COMMAND program", "COMMAND other")]),
])
async def test_complete_result_integrity_guard(local, path, data, edits):
    target = await prepare(local, data, path)
    assert_rejected(local, await apply(local, edits, path), "validation_integrity", target, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [False, True])
async def test_mixed_writes_edits_diff_and_restore_keep_first_snapshot(local, created):
    original = b"\xef\xbb\xbfstart\r\n" if not created else None
    target = local.root / "main.cpp"
    if original is not None:
        await prepare(local, original)
    else:
        await local.changes.write_file("main.cpp", "start\n")
    assert (await apply(local, [edit("start", "first")])).success
    await local.changes.write_file("main.cpp", "second\n")
    result = await apply(local, [edit("second", "final")])
    assert result.success and json.loads(result.output)["created"] is False
    snapshot = local.changes.snapshots["main.cpp"]
    assert snapshot.original == original and snapshot.last_written == b"final\n"
    diff = local.changes.diff()["files"][0]
    assert diff["original_sha256"] == changes_module.digest(original)
    assert diff["agent_sha256"] == changes_module.digest(b"final\n") and not diff["conflict"]
    assert "+final\n" in diff["diff"] and ("-\ufeffstart" in diff["diff"] if original else diff["created"])
    assert (await local.changes.restore())["conflicts"] == 0
    assert target.read_bytes() == original if original is not None else not target.exists()
    assert local.changes.diff()["files"][0]["restored"]
    if original is not None:
        await local.repo.read_file("main.cpp")
        denied = await apply(local, [edit("start", "again")])
        assert not denied.success and denied.error.startswith("write_conflict:")


@pytest.mark.asyncio
async def test_edit_scope_change_defers_entire_batch_and_requires_new_approval(cpp_repo, rg_exe, git_exe):
    (cpp_repo / "Extra.cpp").write_bytes(b"old")
    original = (cpp_repo / "main.cpp").read_bytes()
    scoped = call("edit_file", json.dumps({"path": "Extra.cpp", "edits": [edit("old", "new")]}), "scope")
    calls = [scoped, write("main.cpp", "must defer", "write-later"),
        call("edit_file", json.dumps({"path": "main.cpp", "edits": [edit("int", "long")]}), "edit-later"),
        call("read_file", '{"path":"main.cpp"}', "read-later"), call("cmake_build", "{}", "build-later")]
    service, _ = service_for(rg_exe, git_exe, [reply(calls=calls), reply(json.dumps(proposal("main.cpp", "Extra.cpp"))),
        reply(calls=[call("read_file", '{"path":"Extra.cpp"}', "read-extra"), scoped]), reply("done")])
    try:
        pending = await service.start(str(cpp_repo), "modify")
        service.approve(pending.task_id, 1)
        revised = await service.execute(pending.task_id)
        assert revised.state == TaskState.WAITING_APPROVAL and revised.plan_version == 2
        assert not revised.approved_files and revised.approved_plan_version is None
        assert not service.is_file_approved(pending.task_id, 1, "main.cpp")
        assert (cpp_repo / "main.cpp").read_bytes() == original and (cpp_repo / "Extra.cpp").read_bytes() == b"old"
        assert not service._changes.snapshots
        results = revised.tool_results[-len(calls):]
        assert [r["tool_call_id"] for r in results] == [c["id"] for c in calls]
        assert all(r["error"].startswith("approval_required:") for r in results)
        assert all("deferred" in r["error"] for r in results[1:])
        assert [m["tool_call_id"] for m in revised.conversation if m["role"] == "tool"] == [c["id"] for c in calls]
        with pytest.raises(ForgePilotError, match="stale_plan"):
            service.approve(pending.task_id, 1)
        service.approve(pending.task_id, 2)
        done = await service.execute(pending.task_id)
        assert done.state == TaskState.COMPLETED and (cpp_repo / "Extra.cpp").read_bytes() == b"new"
    finally:
        service.close()
