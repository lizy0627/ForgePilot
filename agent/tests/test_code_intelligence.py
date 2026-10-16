import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

import pytest

from forgepilot.code_intelligence import CodeIntelligence, CodeIntelligenceConfig, position, public_position
from forgepilot.compile_database import checked_arguments, load_database, locate_database
from forgepilot.errors import ForgePilotError
from forgepilot.lsp import LspClient
from forgepilot.process import executable
from forgepilot.repository import Repository
from forgepilot.task_service import TaskService
from forgepilot.tools import ToolRegistry
from forgepilot.tools.symbol_tools import register_symbol_tools
from forgepilot.trace import MemoryTrace
from forgepilot.context import TaskContext
from forgepilot.runtime import AgentRuntime
from forgepilot.recording import TaskJournal
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.security import Redactor
from conftest import ScriptedLLM, call, reply


@pytest.fixture
def symbol_repo(tmp_path, git_exe, rg_exe):
    root = tmp_path / "C++ 符号 with spaces"; root.mkdir()
    (root / "include").mkdir(); (root / "src").mkdir()
    sources = {
        "include/calc.h": "#pragma once\nnamespace math {\nint add(int left, int right);\nint twice(int value);\n}\nnamespace other { int add(int left, int right); }\n",
        "src/calc.cpp": '#include "calc.h"\nnamespace math {\nint add(int left, int right) { return left + right; }\nint twice(int value) { return add(value, value); }\n}\nnamespace other { int add(int left, int right) { return left - right; } }\n',
        "src/main.cpp": '#include "calc.h"\n/* 中文😀 */ int main() { return math::add(1, 2) - math::twice(2); }\n',
        "src/helper.cpp": '#include "calc.h"\nint use_add() { return math::add(4, 5); }\nint use_other() { return other::add(4, 5); }\n',
        "CMakeLists.txt": 'cmake_minimum_required(VERSION 3.24)\nproject(SymbolFixture LANGUAGES CXX)\nset(CMAKE_CXX_STANDARD 20)\nadd_executable(symbols src/main.cpp src/calc.cpp src/helper.cpp)\ntarget_include_directories(symbols PRIVATE include)\n',
    }
    for path, text in sources.items(): (root / path).write_text(text, encoding="utf-8", newline="\n")
    subprocess.run([git_exe, "init", "-q", str(root)], check=True, capture_output=True)
    return Repository(root.resolve(), rg_exe)


def args(repository, path="src/main.cpp", token="add", row=2):
    text = (repository.root / path).read_text(encoding="utf-8")
    return dict(path=path, line=row, column=text.splitlines()[row - 1].index(token) + 1)


def database(repository, directory="build"):
    target = repository.root / directory; target.mkdir(parents=True, exist_ok=True)
    entries = [{"directory": str(repository.root), "file": str(repository.root / f"src/{name}.cpp"),
        "arguments": ["clang++", "-std=c++20", "-I" + str(repository.root / "include"), "-c", str(repository.root / f"src/{name}.cpp")]} for name in ("main", "calc", "helper")]
    (target / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    return target


def peer_factory(tmp_path, mode="fragment"):
    sessions = []; log = tmp_path / (mode + "-peer.jsonl")
    def factory(command, root, **options):
        client = LspClient([sys.executable, "-I", str(Path(__file__).parent / "fixtures/lsp_peer.py"), mode, str(log)], root, **options)
        sessions.append(client)
        return client
    return factory, sessions, log


def assert_closed(sessions):
    assert sessions and all(s.process.returncode is not None and s.job.handle is None and s.stderr_reader.done() for s in sessions)


def test_coordinate_conversion_and_surrogate_boundary():
    text = "first\r\n中文😀 identifier\n"
    assert position(text, 2, 5) == {"line": 1, "character": 5}
    assert public_position(text, {"line": 1, "character": 5}) == (2, 5)
    assert public_position(text, {"line": 0, "character": 999}) == (1, 6)
    with pytest.raises(ForgePilotError, match="surrogate"): public_position(text, {"line": 1, "character": 3})
    for line, col in [(0, 1), (4, 1), (1, 99)]:
        with pytest.raises(ForgePilotError, match="symbol_position"): position(text, line, col)


@pytest.mark.asyncio
async def test_real_clangd_cmake_definitions_references_and_fresh_source(symbol_repo, tmp_path, git_exe):
    clangd = executable(os.environ.get("FORGEPILOT_TEST_CLANGD"), "clangd")
    cmake = executable(os.environ.get("FORGEPILOT_CMAKE"), "cmake")
    ninja = executable(os.environ.get("FORGEPILOT_NINJA"), "ninja")
    build = symbol_repo.root / "custom/analysis-ninja"
    run = subprocess.run([cmake, "-S", str(symbol_repo.root), "-B", str(build), "-G", "Ninja",
        "-DCMAKE_MAKE_PROGRAM=" + ninja, "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"], capture_output=True, timeout=45)
    assert run.returncode == 0, run.stdout + run.stderr
    sessions = []
    def factory(*a, **kw):
        value = LspClient(*a, **kw); sessions.append(value); return value
    intelligence = CodeIntelligence(symbol_repo, CodeIntelligenceConfig(clangd, str(build)), client_factory=factory)
    registry = ToolRegistry(); register_symbol_tools(registry, intelligence)
    definition = await registry.execute("find_symbol", args(symbol_repo)); assert definition.success, definition.error
    defined = json.loads(definition.output)
    assert defined["source"] == "clangd" and defined["semantic"], defined
    assert {(r["path"], r["line"]) for r in defined["locations"]} == {("src/calc.cpp", 3)}
    references = await registry.execute("find_references", args(symbol_repo)); assert references.success, references.error
    refs = json.loads(references.output); assert refs["source"] == "clangd", refs
    locations = {(r["path"], r["line"]) for r in refs["locations"]}
    assert {("src/main.cpp", 2), ("src/helper.cpp", 2), ("src/calc.cpp", 4)} <= locations
    assert ("src/helper.cpp", 3) not in locations
    limited = json.loads((await registry.execute("find_references", {**args(symbol_repo), "max_results": 1})).output)
    assert limited["source"] == "clangd" and len(limited["locations"]) == 1 and limited["truncated"]
    main = next(r for r in refs["locations"] if r["path"] == "src/main.cpp")
    assert main["column"] == args(symbol_repo)["column"] and "中文😀" in main["context"]
    header = json.loads((await registry.execute("find_symbol", args(symbol_repo, "include/calc.h", row=3))).output)
    assert header["source"] == "clangd" and header["locations"][0]["path"] == "src/calc.cpp"
    file = symbol_repo.root / "src/main.cpp"
    file.write_text(file.read_text(encoding="utf-8").replace("math::add", "other::add"), encoding="utf-8", newline="\n")
    changed = json.loads((await registry.execute("find_symbol", args(symbol_repo))).output)
    assert changed["source"] == "clangd" and changed["locations"][0]["line"] == 6
    assert_closed(sessions)
    class InterruptedClangd(LspClient):
        async def request(self, method, params=None, **options):
            if method == "textDocument/definition": self.job.terminate()
            return await super().request(method, params, **options)
    def interrupted_factory(*a, **kw):
        value = InterruptedClangd(*a, **kw); sessions.append(value); return value
    interrupted = json.loads(await CodeIntelligence(symbol_repo, CodeIntelligenceConfig(clangd, str(build)),
        client_factory=interrupted_factory).query("find_symbol", **args(symbol_repo)))
    assert interrupted["source"] == "rg" and interrupted["fallback_reason"] == "clangd_exited" and interrupted["text_matches"]
    assert all(row["path"].endswith((".cpp", ".h")) for row in interrupted["text_matches"])
    assert_closed(sessions)
    store = SqliteEventStore(tmp_path / "semantic-trace.sqlite3")
    journal = TaskJournal(store, lambda: service.snapshot(), Redactor())
    service = TaskService(ScriptedLLM([reply(calls=[call("find_symbol", json.dumps(args(symbol_repo)))]), reply("development semantic evidence")]),
        rg_executable=symbol_repo.rg_executable, git_executable=git_exe, trace=journal,
        code_intelligence_config=CodeIntelligenceConfig(clangd, str(build)))
    try:
        result = await service.analyze(str(symbol_repo.root), "development semantic fixture"); journal.flush()
        assert result.state == "completed" and result.steps == 2 and result.tool_calls == 1
        saved = json.loads(store._db.execute("SELECT result_json FROM tool_calls WHERE name='find_symbol'").fetchone()[0])
        assert saved["success"] and json.loads(saved["output"])["source"] == "clangd"
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally: service.close(); store.close()
    assert not (symbol_repo.root / ".cache").exists()
    print("REAL CLANGD " + json.dumps({"task_id": result.task_id, "definition": defined, "references": refs,
        "fresh_definition": changed, "interrupted_fallback": interrupted}, ensure_ascii=False))


@pytest.mark.asyncio
async def test_semantic_source_reads_do_not_establish_or_refresh_write_baseline(symbol_repo, tmp_path):
    database(symbol_repo)
    factory, sessions, _ = peer_factory(tmp_path)
    tool = CodeIntelligence(symbol_repo, CodeIntelligenceConfig(sys.executable), client_factory=factory)
    result = json.loads(await tool.query("find_symbol", **args(symbol_repo)))
    assert result["source"] == "clangd" and symbol_repo._read_versions == {}
    await symbol_repo.read_file("src/main.cpp")
    versions = dict(symbol_repo._read_versions)
    target = symbol_repo.root / "src/main.cpp"
    target.write_bytes(target.read_bytes() + b"// external edit\n")
    result = json.loads(await tool.query("find_references", **args(symbol_repo)))
    assert result["source"] == "clangd" and symbol_repo._read_versions == versions
    assert_closed(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition,reason", [("missing", "compile_database_missing"), ("clangd", "clangd_unavailable")])
async def test_missing_database_or_clangd_falls_back(symbol_repo, condition, reason):
    if condition == "clangd": database(symbol_repo)
    config = CodeIntelligenceConfig(str(symbol_repo.root / "absent-clangd.exe"))
    result = json.loads(await CodeIntelligence(symbol_repo, config).query("find_references", **args(symbol_repo)))
    assert result["source"] == "rg" and not result["semantic"] and result["fallback_reason"] == reason
    assert result["locations"] == [] and result["text_matches"] and "NOT reliable" in result["limitations"][0]
    assert not (symbol_repo.root / ".forgepilot").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("exit", "clangd_exited"), ("hang", "clangd_timeout"),
    ("oversized", "clangd_limit"), ("request_error", "clangd_request"), ("bad_initialize", "clangd_protocol"),
    ("diagnostic", "clangd_diagnostics")])
async def test_process_failure_timeout_and_protocol_fallback(symbol_repo, tmp_path, mode, reason):
    database(symbol_repo)
    factory, sessions, log = peer_factory(tmp_path, mode)
    tool = CodeIntelligence(symbol_repo, CodeIntelligenceConfig(sys.executable, request_timeout=0.3), client_factory=factory)
    result = json.loads(await tool.query("find_symbol", **args(symbol_repo)))
    assert result["source"] == "rg" and result["fallback_reason"] == reason and result["text_matches"]
    assert_closed(sessions)
    if mode == "hang": assert "$/cancelRequest" in log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fragmented_lsp_sync_encoding_server_requests_and_shutdown(symbol_repo, tmp_path, monkeypatch):
    factory, sessions, log = peer_factory(tmp_path)
    monkeypatch.setenv("FORGEPILOT_API_KEY", "never-in-child-credential")
    client = factory([], symbol_repo.root)
    try:
        await client.start()
        await client.sync(symbol_repo.root / "src/main.cpp", "中文😀 alpha")
        await client.sync(symbol_repo.root / "src/main.cpp", "中文😀 beta")
        assert await client.request("textDocument/definition", {}) == []
    finally: await client.close()
    assert_closed(sessions)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not any(r["credential_present"] for r in records)
    change = next(r for r in records if r.get("method") == "textDocument/didChange")
    assert change["params"]["textDocument"]["version"] == 2 and change["params"]["contentChanges"] == [{"text": "中文😀 beta"}]
    assert any(r.get("id") == "server-edit" and r["error"]["code"] == -32601 for r in records)
    assert [r.get("method") for r in records[-3:]] == ["textDocument/didClose", "shutdown", "exit"]


@pytest.mark.asyncio
async def test_explicit_cancel_propagates_and_reaps_process(symbol_repo, tmp_path):
    database(symbol_repo); factory, sessions, log = peer_factory(tmp_path, "hang")
    tool = CodeIntelligence(symbol_repo, CodeIntelligenceConfig(sys.executable), client_factory=factory)
    task = asyncio.create_task(tool.query("find_symbol", **args(symbol_repo)))
    for _ in range(300):
        if log.exists() and "textDocument/definition" in log.read_text(encoding="utf-8"): break
        await asyncio.sleep(0.01)
    assert log.exists() and "textDocument/definition" in log.read_text(encoding="utf-8")
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
    assert_closed(sessions)
    assert "$/cancelRequest" in log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shutdown_timeout_still_reaps_tree(symbol_repo, tmp_path):
    factory, sessions, log = peer_factory(tmp_path, "shutdown_hang")
    client = factory([], symbol_repo.root); await client.start()
    start = monotonic(); await client.close()
    assert monotonic() - start < 3 and sessions[0].process.returncode != 0
    assert_closed(sessions)


@pytest.mark.asyncio
async def test_cancel_during_process_start_reaps_launcher(symbol_repo, tmp_path):
    factory, sessions, log = peer_factory(tmp_path)
    client = factory([], symbol_repo.root)
    async def run():
        try:
            await client.start()
        finally:
            await client.close()
    task = asyncio.create_task(run())
    while client.launcher is None:
        await asyncio.sleep(0)
    assert not client.initialized
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert client.process.returncode is not None and client.job.handle is None and client.closer.done()
    assert client.stderr_reader is None or client.stderr_reader.done()
    await client.close()  # Idempotent after early cancellation.


@pytest.mark.asyncio
async def test_repeated_cancel_during_shutdown_does_not_abandon_cleanup(symbol_repo, tmp_path):
    factory, sessions, log = peer_factory(tmp_path, "shutdown_hang")
    client = factory([], symbol_repo.root); await client.start()
    closing = asyncio.create_task(client.close())
    for _ in range(100):
        if '"method": "shutdown"' in log.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.01)
    assert '"method": "shutdown"' in log.read_text(encoding="utf-8")
    closing.cancel(); await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 3)
    assert_closed(sessions)
    assert client.closer.done()


@pytest.mark.asyncio
async def test_operation_deadline_cancels_lsp_before_rg_fallback(symbol_repo, tmp_path):
    database(symbol_repo); factory, sessions, log = peer_factory(tmp_path, "hang")
    result = json.loads(await CodeIntelligence(symbol_repo,
        CodeIntelligenceConfig(sys.executable, timeout=0.8, request_timeout=8), client_factory=factory).query("find_symbol", **args(symbol_repo)))
    assert result["source"] == "rg" and result["fallback_reason"] == "clangd_timeout"
    assert "$/cancelRequest" in log.read_text(encoding="utf-8")
    assert_closed(sessions)


def test_database_selection_presets_cache_ambiguity_and_boundaries(symbol_repo):
    build = database(symbol_repo, "custom/debug")
    (symbol_repo.root / "CMakePresets.json").write_text(json.dumps({"version": 6, "configurePresets": [
        {"name": "debug", "generator": "Ninja", "binaryDir": "${sourceDir}/custom/debug"}]}), encoding="utf-8")
    assert locate_database(symbol_repo).path.parent == build
    assert locate_database(symbol_repo, selected=symbol_repo.root / ".forgepilot/build").path.parent == build
    second = database(symbol_repo, "build")
    with pytest.raises(ForgePilotError, match="compile_database_ambiguous"): locate_database(symbol_repo)
    assert locate_database(symbol_repo, selected=build).path.parent == build
    assert locate_database(symbol_repo, configured=second, selected=build).path.parent == second
    with pytest.raises(ForgePilotError, match="build_path"): locate_database(symbol_repo, configured=symbol_repo.root.parent)
    cache = build / "CMakeCache.txt"
    cache.write_text(f"CMAKE_HOME_DIRECTORY:INTERNAL={symbol_repo.root}\nCMAKE_CACHEFILE_DIR:INTERNAL={build}\nCMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="compile_database_generator"): load_database(symbol_repo, build)
    cache.write_text("CMAKE_HOME_DIRECTORY:INTERNAL=C:/not-this-repository\n", encoding="utf-8")
    with pytest.raises(ForgePilotError, match="compile_database_stale"): load_database(symbol_repo, build)


@pytest.mark.asyncio
@pytest.mark.parametrize("data,reason", [("{invalid", "compile_database_invalid"), ("[]", "compile_database_invalid"),
    (" " * 4_194_305, "compile_database_limit")], ids=["malformed", "empty", "oversized"])
async def test_invalid_database_degrades_without_starting_clangd(symbol_repo, data, reason):
    directory = database(symbol_repo)
    (directory / "compile_commands.json").write_text(data, encoding="utf-8")
    def forbidden(*a, **kw): pytest.fail("Invalid metadata must not launch clangd")
    result = json.loads(await CodeIntelligence(symbol_repo, client_factory=forbidden).query("find_symbol", **args(symbol_repo)))
    assert result["source"] == "rg" and result["fallback_reason"] == reason


def test_database_metadata_hardlinks_and_outside_sources(symbol_repo, tmp_path):
    directory = database(symbol_repo)
    path = directory / "compile_commands.json"
    os.link(path, tmp_path / "database-hardlink.json")
    with pytest.raises(ForgePilotError, match="compile_database_boundary"): load_database(symbol_repo, directory)
    (tmp_path / "database-hardlink.json").unlink()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.append({**data[0], "file": str(tmp_path / "outside.cpp")})
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_database(symbol_repo, directory)
    assert loaded.omitted == 1 and len(loaded.files) == 3


@pytest.mark.parametrize("argument", ["@flags.rsp", "-Xclang", "-fplugin=evil.dll", "--config=outside", "/clang:-load"])
def test_database_command_cannot_load_indirect_code(argument):
    with pytest.raises(ForgePilotError, match="compile_database_unsupported"):
        checked_arguments({"arguments": ["clang++", argument, "main.cpp"]})


def test_database_allows_only_cmake_windows_clang_debug_metadata():
    safe = ["clang++", "-Xclang", "-gcodeview", "-Xclang",
            "--dependent-lib=msvcrtd", "-c", "main.cpp"]
    assert checked_arguments({"arguments": safe}) == safe
    for unsafe in ("-load", "-plugin=evil", "--dependent-lib=bad/value"):
        with pytest.raises(ForgePilotError, match="compile_database_unsupported"):
            checked_arguments({"arguments": ["clang++", "-Xclang", unsafe, "main.cpp"]})


@pytest.mark.asyncio
async def test_tool_strict_validation_boundary_and_output_limits(symbol_repo):
    registry = ToolRegistry(); register_symbol_tools(registry, CodeIntelligence(symbol_repo))
    for data in [{**args(symbol_repo), "line": True}, {**args(symbol_repo), "extra": 1},
                 {**args(symbol_repo), "max_results": 101}, {**args(symbol_repo), "path": "../private.cpp"}]:
        result = await registry.execute("find_symbol", data); assert not result.success
    out = json.loads((await registry.execute("find_references", {**args(symbol_repo), "max_results": 1})).output)
    assert out["source"] == "rg" and len(out["text_matches"]) == 1 and out["truncated"]
    locations = CodeIntelligence(symbol_repo)._locations([
        {"uri": (symbol_repo.root.parent / "private.cpp").as_uri(), "range": {}},
        {"uri": "https://example.invalid/private.cpp", "range": {}}], {}, 50)
    assert locations["locations"] == [] and locations["filtered_locations"] == 2
    span = {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 7}}
    link = {"targetUri": (symbol_repo.root / "src/calc.cpp").as_uri(), "targetSelectionRange": span}
    mapped = CodeIntelligence(symbol_repo)._locations([link, link], {}, 50)
    assert len(mapped["locations"]) == 1 and mapped["locations"][0]["column"] == 5


@pytest.mark.asyncio
async def test_runtime_trace_fallback_keeps_task_success_and_existing_budget(symbol_repo, rg_exe, git_exe):
    trace = MemoryTrace(); model = ScriptedLLM([
        reply(calls=[call("find_symbol", json.dumps(args(symbol_repo)))]), reply("Text candidates only; semantic lookup unavailable")])
    service = TaskService(model, rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    try:
        result = await service.analyze(str(symbol_repo.root), "development fixture")
        assert result.state == "completed" and result.steps == 2 and result.tool_calls == 1
        started = next(e for e in trace.events if e.kind == "tool_started")
        finished = next(e for e in trace.events if e.kind == "tool_finished")
        assert started.task_id == finished.task_id == result.task_id and started.sequence < finished.sequence
        assert finished.data["success"] and json.loads(finished.data["output"])["source"] == "rg"
    finally: service.close()


@pytest.mark.asyncio
async def test_symbol_fallback_does_not_reset_agent_budget(symbol_repo):
    registry = ToolRegistry(); register_symbol_tools(registry, CodeIntelligence(symbol_repo))
    model = ScriptedLLM([reply(calls=[call("find_references", json.dumps(args(symbol_repo)))]) for _ in range(3)])
    runtime = AgentRuntime(model, registry, MemoryTrace(), max_steps=2)
    result = await runtime.run(TaskContext(str(symbol_repo.root), "development budget fixture"))
    assert result.state == "failed" and result.steps == 2 and len(model.requests) == 2 and result.tool_calls == 2
    assert result.error.startswith("step_limit:")
