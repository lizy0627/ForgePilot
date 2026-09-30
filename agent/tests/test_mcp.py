"""Real local stdio subprocesses; only the OpenAI-Compatible model is an offline fixture."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import logging
from pathlib import Path
import sys

import httpx
import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.context import TaskContext
from forgepilot.errors import ForgePilotError
from forgepilot.llm import OpenAICompatibleClient
from forgepilot.mcp import MCPClient
from forgepilot.mcp_config import MCPConfig, TOOL_NAME, load_mcp_config
from forgepilot.mcp_schema import checked_schema, validate
from forgepilot.repository import Repository
from forgepilot.runtime import AgentRuntime
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.tools import ToolRegistry, register_repository_tools
from forgepilot.tools.mcp_tools import MCPToolAdapter
from forgepilot.trace import MemoryTrace

EXAMPLE = Path(__file__).parents[1] / "examples/mcp_project_notes.py"
PEER = Path(__file__).parent / "fixtures/mcp_peer.py"
ARGS = {"topic": "architecture"}


def config(tmp_path, mode=None, timeout=2.0):
    args = ("-I", "-u", str(EXAMPLE)) if mode is None else ("-I", "-u", str(PEER), mode, str(tmp_path / "wire.jsonl"))
    return MCPConfig(command=sys.executable, args=args, cwd=str(tmp_path), request_timeout=timeout)


def wire(tmp_path):
    return [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text(encoding="utf-8").splitlines()]


def closed(client):
    assert client.job.handle is None
    assert client.process.returncode is not None
    assert client.reader.done() and client.stderr_reader.done() and client.closer.done()


async def adapter_for(tmp_path, mode=None, *, timeout=2.0, registry=None):
    registry = registry or ToolRegistry()
    adapter = MCPToolAdapter(config(tmp_path, mode, timeout))
    await adapter.register(registry)
    return registry, adapter


async def wait_method(tmp_path, method):
    async with asyncio.timeout(5):
        while not (tmp_path / "wire.jsonl").exists() or not any(m.get("method") == method for m in wire(tmp_path)):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stderr_128k_is_discarded_without_failing_call(tmp_path,caplog):
    caplog.set_level(logging.DEBUG)
    registry,adapter=await adapter_for(tmp_path,'stderr_128k')
    assert not hasattr(adapter,'timeout_seconds')  # Legacy Registry timeout stays unchanged.
    client=adapter.client
    try:
        result=await registry.execute(adapter.name,ARGS)
        assert result.success,result.error
        assert client.failure is None and not client.stderr_reader.done()
        assert any(row.get('stderr_bytes')==131072 for row in wire(tmp_path))
        assert len([r for r in wire(tmp_path) if r.get('method')=='tools/call'])==1
    finally: await adapter.close()
    closed(client)
    assert 'private stderr' not in caplog.text and 'sk-development-secret' not in caplog.text


@pytest.mark.asyncio
async def test_continuous_stderr_can_cancel_and_reap_without_logging(tmp_path,caplog):
    caplog.set_level(logging.DEBUG)
    registry,adapter=await adapter_for(tmp_path,'stderr_flood',timeout=5)
    client=adapter.client
    pending=asyncio.create_task(registry.execute(adapter.name,ARGS))
    try:
        await wait_method(tmp_path,'tools/call')
        await asyncio.sleep(.15)
        assert not pending.done() and client.failure is None
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending,3)
    finally: await adapter.close()
    closed(client)
    assert any(r.get('method')=='notifications/cancelled' for r in wire(tmp_path))
    assert 'private stderr' not in caplog.text and 'sk-development-secret' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["mcp_status", "tool_finished"])
async def test_storage_failure_closes_real_stdio_and_llm(cpp_repo, tmp_path, rg_exe, git_exe, monkeypatch, phase):
    from forgepilot.task_host import TaskHost
    from forgepilot.sqlite_store import SqliteEventStore
    clients, services = [], []
    def client_factory(value):
        client = MCPClient(value); clients.append(client); return client
    monkeypatch.setattr('forgepilot.task_service.MCPToolAdapter',
        lambda config: MCPToolAdapter(config, client_factory=client_factory))
    class Model(ScriptedLLM):
        closed = False
        async def aclose(self): self.closed = True
    def factory(trace):
        service = TaskService(Model([reply(calls=[call(TOOL_NAME, json.dumps(ARGS))])]),
            rg_executable=rg_exe, git_executable=git_exe, trace=trace, mcp_config=config(tmp_path, "fragment"))
        services.append(service); return service
    store = SqliteEventStore(tmp_path / 'mcp-publication.sqlite3')
    host = TaskHost(factory, store=store)
    publish = store._publish
    def fail(tid, snapshot, events, traces, closed, **kwargs):
        result = publish(tid, snapshot, events, traces, closed, **kwargs)
        if any(t.kind == phase for t in traces): raise sqlite3.OperationalError('fixture')
        return result
    monkeypatch.setattr(store, '_publish', fail)
    try:
        created = await host._create(str(cpp_repo), 'fixture')
        tid = created['task_id']
        entry = host._tasks[tid]
        await asyncio.wait_for(asyncio.shield(entry.running), 3)
        assert entry.persistence_error.code == 'storage_unavailable'
        assert clients and all(c.process is not None for c in clients)
        for client in clients: closed(client)
        assert services[0].llm.closed and services[0]._mcp.client is None
        assert services[0]._active is None and services[0]._lock.handle is None
        assert not host._tasks and not host._repositories
        with pytest.raises(ForgePilotError, match='storage_unavailable'): store.get(tid)
    finally:
        await host._shutdown()
        store.close()


@pytest.mark.asyncio
async def test_example_openai_compatible_roundtrip_trace_and_sqlite(cpp_repo, tmp_path, rg_exe, git_exe):
    from forgepilot.recording import TaskJournal
    from forgepilot.sqlite_store import SqliteEventStore
    requests = []
    def respond(request):
        data = json.loads(request.content); requests.append(data)
        if len(requests) == 1:
            schema = next(t["function"] for t in data["tools"] if t["function"]["name"] == TOOL_NAME)
            assert schema["parameters"]["properties"]["topic"]["enum"] == ["overview", "architecture", "workflow"]
            message = {"role": "assistant", "content": None,
                       "tool_calls": [call(TOOL_NAME, json.dumps(ARGS), "mcp-call")]}
        else:
            observation = data["messages"][-1]
            assert observation["role"] == "tool" and observation["tool_call_id"] == "mcp-call"
            result = json.loads(observation["content"])
            assert result["success"] and "HTTP and SSE" in result["output"] and result["duration_ms"] >= 0
            message = {"role": "assistant", "content": "OFFLINE FIXTURE: project notes received"}
        return httpx.Response(200, json={"choices": [{"message": message,
            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]})
    path = tmp_path / "mcp.sqlite3"
    redactor = Redactor(("sk-development-secret",))
    store = SqliteEventStore(path, redactor=redactor)
    async with OpenAICompatibleClient("http://localhost/v1", "sk-development-secret", "offline-fixture",
                                      transport=httpx.MockTransport(respond)) as llm:
        service = None
        trace = TaskJournal(store, lambda: service.snapshot(), redactor)
        service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace,
                              mcp_config=config(tmp_path))
        result = await service.analyze(str(cpp_repo), "Read preset architecture notes")
        trace.flush()
        assert result.state == "completed" and result.steps == 2 and result.tool_calls == 1, result.error
        assert len(requests) == 2 and service._mcp.client is None
        row = store._db.execute("SELECT * FROM tool_calls").fetchone()
        assert row["name"] == TOOL_NAME and row["status"] == "succeeded"
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
        details = store.trace_page(result.task_id, limit=100)["items"]
        assert {"mcp_status", "tool_started", "tool_finished", "llm_response"} <= {x["kind"] for x in details}
        print(f"OFFLINE MCP E2E: task={result.task_id} steps={result.steps} calls={result.tool_calls} state={result.state}")
        service.close()
    store.close()
    assert b"sk-development-secret" not in path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["split", "pagination", "requests"])
async def test_lifecycle_pagination_client_requests_and_secret_free_environment(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FORGEPILOT_API_KEY", "sk-development-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-development-secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    registry, adapter = await adapter_for(tmp_path, mode)
    client = adapter.client
    try:
        assert (await registry.execute(TOOL_NAME, ARGS)).success
    finally:
        await adapter.close()
    closed(client)
    log = wire(tmp_path)
    assert not log[0]["environment_has_secret"] and log[0]["cwd"] == str(tmp_path)
    methods = [m["method"] for m in log if "method" in m]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    assert methods[-1] == "tools/call" and log[-1] == {"eof": True}
    assert "shutdown" not in methods and "exit" not in methods
    if mode == "pagination": assert methods.count("tools/list") == 2
    if mode == "requests":
        assert any(m.get("id") == "server-edit" and m["error"]["code"] == -32601 for m in log)
        assert any(m.get("id") == "server-ping" and m.get("result") == {} for m in log)


@pytest.mark.asyncio
async def test_namespace_collision_and_unlisted_tools_never_override_builtins(cpp_repo, tmp_path, rg_exe, git_exe):
    repo = await Repository.open(str(cpp_repo), rg_executable=rg_exe, git_executable=git_exe)
    registry = ToolRegistry(); register_repository_tools(registry, repo)
    original = registry.get("read_file")
    registry, adapter = await adapter_for(tmp_path, "split", registry=registry)
    second = MCPToolAdapter(config(tmp_path))
    try:
        assert registry.get("read_file") is original
        assert registry.get("project_info") is None and registry.get("write_file") is None
        assert (await registry.execute("read_file", {"path": "Counter.cpp"})).success
        with pytest.raises(ForgePilotError, match="mcp_name_conflict"):
            await second.register(registry)
        assert registry.get(TOOL_NAME) is adapter
    finally:
        await adapter.close(); await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{}, {"topic": 1}, {"topic": True}, {"topic": "../private"},
                                   {"topic": "overview", "command": "anything"}, "{bad", "[]"])
async def test_invalid_arguments_do_not_reach_server(tmp_path, value):
    registry, adapter = await adapter_for(tmp_path, "split")
    try:
        result = await registry.execute(TOOL_NAME, value)
        assert not result.success and result.error.startswith("invalid_arguments:")
        assert not any(m.get("method") == "tools/call" for m in wire(tmp_path))
    finally: await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("version", "mcp_version"), ("capability", "mcp_capability"),
    ("schema", "mcp_schema"), ("unsafe", "mcp_allowlist"), ("execution", "mcp_allowlist"),
    ("missing", "mcp_allowlist"), ("duplicate", "mcp_catalog"), ("cursor", "mcp_catalog")])
async def test_failed_discovery_is_explicit_and_builtin_chain_continues(cpp_repo, tmp_path, rg_exe, git_exe, mode, code):
    trace = MemoryTrace()
    llm = ScriptedLLM([reply(calls=[call()]), reply("builtin still works")])
    service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace, mcp_config=config(tmp_path, mode))
    result = await service.analyze(str(cpp_repo), "fixture")
    assert result.state == "completed" and result.tool_calls == 1
    status = next(e for e in trace.events if e.kind == "mcp_status")
    assert status.data["status"] == "unavailable" and status.data["error"].startswith(code + ":")
    assert service._runtime.registry.get(TOOL_NAME) is None and service._mcp.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("rpc_error", "mcp_request"), ("tool_error", "mcp_tool_error"),
    ("nontext", "mcp_unsupported_content"), ("bad_content", "mcp_content"),
    ("disconnect", "mcp_disconnected"), ("malformed", "mcp_protocol"), ("oversized", "mcp_protocol"),
    ("changed", "mcp_catalog_changed")])
async def test_call_errors_preserved_as_tool_result(tmp_path, mode, code):
    registry = ToolRegistry(redactor=Redactor(("sk-development-secret",)))
    registry, adapter = await adapter_for(tmp_path, mode, registry=registry)
    client = adapter.client
    try:
        result = await registry.execute(TOOL_NAME, ARGS)
        assert not result.success and result.error.startswith(code + ":")
        assert result.duration_ms >= 0 and "sk-development-secret" not in result.model_dump_json()
        if mode == "nontext":
            assert json.loads(result.output)["content"][1]["payload_omitted"]
        if mode == "tool_error": assert "fixture tool failure" in result.output
    finally: await adapter.close()
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["structured", "mixed_error", "long"])
async def test_structured_output_mixed_error_and_truncation_are_explicit(tmp_path, mode):
    registry, adapter = await adapter_for(tmp_path, mode)
    try:
        result = await registry.execute(TOOL_NAME, ARGS)
        if mode == "structured": assert json.loads(result.output)["structuredContent"]["count"] == 1
        if mode == "mixed_error":
            assert "mcp_tool_error" in result.error and "mcp_unsupported_content" in result.error
            assert json.loads(result.output)["content"][1]["type"] == "resource_link"
        if mode == "long":
            assert len(result.output) <= 16_384
            assert json.loads(result.output)["truncation"] == "[TRUNCATED: tool output limit]"
    finally: await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", [False, True])
async def test_request_and_registry_timeouts_send_cancellation_and_reap(tmp_path, outer):
    registry, adapter = await adapter_for(tmp_path, "hang", timeout=2.0)
    client = adapter.client
    if outer: registry.timeout = 0.15
    else: client.config = client.config.model_copy(update={"request_timeout": 0.15})
    result = await registry.execute(TOOL_NAME, ARGS)
    assert not result.success and result.error.startswith("tool_timeout:" if outer else "mcp_timeout:")
    closed(client)
    cancel = next(m for m in wire(tmp_path) if m.get("method") == "notifications/cancelled")
    request = next(m for m in wire(tmp_path) if m.get("method") == "tools/call")
    assert cancel["params"]["requestId"] == request["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,method", [("init_hang", "initialize"), ("list_hang", "tools/list"), ("hang", "tools/call")])
async def test_cancel_during_lifecycle_and_runtime_records_terminal(cpp_repo, tmp_path, rg_exe, git_exe, mode, method):
    trace = MemoryTrace()
    llm = ScriptedLLM([reply(calls=[call(TOOL_NAME, json.dumps(ARGS))])])
    service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace, mcp_config=config(tmp_path, mode))
    task = asyncio.create_task(service.analyze(str(cpp_repo), "cancel fixture"))
    await wait_method(tmp_path, method)
    client = service._mcp.client
    assert service.cancel()
    result = await asyncio.wait_for(task, 3)
    assert result.state == "cancelled" and service._mcp.client is None
    closed(client)
    methods = [m.get("method") for m in wire(tmp_path)]
    assert ("notifications/cancelled" in methods) == (method != "initialize")
    if method == "tools/call":
        finished = next(e for e in trace.events if e.kind == "tool_finished")
        assert finished.data["error"].startswith("cancelled:") and result.tool_calls == 1


@pytest.mark.asyncio
async def test_budget_shared_and_disconnect_keeps_builtin_chain(cpp_repo, tmp_path, rg_exe, git_exe):
    registry, adapter = await adapter_for(tmp_path, "disconnect")
    repo = await Repository.open(str(cpp_repo), rg_executable=rg_exe, git_executable=git_exe)
    register_repository_tools(registry, repo)
    llm = ScriptedLLM([reply(calls=[call(TOOL_NAME, json.dumps(ARGS))]), reply(calls=[call()]), reply("must not run")])
    runtime = AgentRuntime(llm, registry, MemoryTrace(), max_steps=2)
    try:
        result = await runtime.run(TaskContext(str(cpp_repo), "budget fixture"))
        assert result.error.startswith("step_limit:") and result.steps == 2 and result.tool_calls == 2
        assert len(llm.requests) == 2
        observations = [json.loads(m["content"]) for m in result.conversation if m["role"] == "tool"]
        assert observations[0]["error"].startswith("mcp_disconnected:") and observations[1]["success"]
        assert (await registry.execute(TOOL_NAME, ARGS)).error.startswith("mcp_unavailable:")
    finally: await adapter.close()


def test_schema_subset_checks_nested_types_and_rejects_unsupported_constraints():
    schema = checked_schema({"type": "object", "additionalProperties": False,
        "properties": {"items": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 2}},
        "required": ["items"]})
    validate(schema, {"items": [0, 1]})
    for args in ({"items": [True]}, {"items": [-1]}, {"items": [1, 2, 3]}, {"items": [], "x": 1}):
        with pytest.raises(ValueError): validate(schema, args)
    for change in ({"$ref": "https://example.invalid"}, {"additionalProperties": 1}, {"allOf": []}, {"patternProperties": {}}):
        with pytest.raises(ForgePilotError, match="mcp_schema"): checked_schema({**schema, **change})
    # Open objects are now supported; extra fields remain forbidden when the
    # published schema says so (asserted above), with no silent type coercion.
    validate(checked_schema({**schema, "additionalProperties": True}), {"items": [1], "extra": True})


def test_trusted_config_and_disabled_mode(tmp_path, monkeypatch):
    from forgepilot.cli import parser
    from forgepilot.settings import mcp_from_args
    path = tmp_path / "server.json"
    path.write_text(config(tmp_path).model_dump_json(), encoding="utf-8")
    assert load_mcp_config(str(path)).args[2] == str(EXAMPLE)
    monkeypatch.setenv("FORGEPILOT_MCP_CONFIG", str(path))
    assert mcp_from_args(parser().parse_args(["repo", "prompt"])).command == sys.executable
    path.write_text('{"command":"malicious"}', encoding="utf-8")
    assert mcp_from_args(parser().parse_args(["repo", "prompt", "--disable-mcp"])) is None
    with pytest.raises(ForgePilotError, match="mcp_config"): load_mcp_config(str(path))
    with pytest.raises(ForgePilotError, match="mcp_config"): load_mcp_config("relative.json")
    for key, value in (("command", "python.exe"), ("cwd", "relative"), ("args", ["bad\0arg"]), ("servers", [])):
        data = config(tmp_path).model_dump(mode="json"); data[key] = value
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ForgePilotError, match="mcp_config"): load_mcp_config(str(path))


@pytest.mark.asyncio
async def test_disabled_mcp_starts_no_process_and_preserves_readonly_chain(cpp_repo, rg_exe, git_exe, monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("Disabled MCP must not launch")
    monkeypatch.setattr(MCPClient, "start", forbidden)
    trace = MemoryTrace()
    service = TaskService(ScriptedLLM([reply(calls=[call()]), reply("builtin success")]),
                          rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    result = await service.analyze(str(cpp_repo), "fixture")
    assert result.state == "completed" and result.tool_calls == 1
    assert all(not s.name.startswith("mcp_") for s in service._runtime.registry.get_specs())
    assert not any(e.kind == "mcp_status" for e in trace.events)


@pytest.mark.asyncio
async def test_cancel_during_process_creation_still_joins_and_reaps_owned_job(tmp_path, monkeypatch):
    actual = asyncio.create_subprocess_exec
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        process = await actual(*args, **kwargs)
        started.set()
        await release.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    clients = []
    def factory(value):
        client = MCPClient(value); clients.append(client); return client
    adapter = MCPToolAdapter(config(tmp_path), client_factory=factory)
    task = asyncio.create_task(adapter.register(ToolRegistry()))
    await started.wait()
    task.cancel(); release.set()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
    client = clients[0]
    assert client.process.returncode is not None and client.job.handle is None and client.closer.done()


@pytest.mark.asyncio
async def test_repeated_cancel_during_close_cannot_abandon_process(tmp_path):
    client = MCPClient(config(tmp_path, "close_hang"))
    await client.start()
    closing = asyncio.create_task(client.close())
    async with asyncio.timeout(3):
        while not any(m.get("eof") for m in wire(tmp_path)): await asyncio.sleep(0.01)
    closing.cancel(); await asyncio.sleep(0); closing.cancel()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(closing, 3)
    closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,method", [("init_hang", "initialize"), ("list_hang", "tools/list")])
async def test_discovery_request_timeout_closes_and_does_not_cancel_initialize(tmp_path, mode, method):
    clients = []
    def factory(value):
        client = MCPClient(value); clients.append(client); return client
    adapter = MCPToolAdapter(config(tmp_path, mode, 0.3), client_factory=factory)
    with pytest.raises(ForgePilotError, match="mcp_timeout"):
        await adapter.register(ToolRegistry())
    closed(clients[0])
    assert any(m.get("method") == method for m in wire(tmp_path))
    assert any(m.get("method") == "notifications/cancelled" for m in wire(tmp_path)) == (method != "initialize")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "missing"])
async def test_http_sse_approval_boundary_reopens_session_and_preserves_authority(cpp_repo, tmp_path, rg_exe, git_exe, mode):
    from forgepilot.api import create_app
    from forgepilot.sqlite_store import SqliteEventStore
    from test_api import until, frames
    from test_planning import proposal
    from test_changes import write
    instances = []
    before = (cpp_repo / "main.cpp").read_bytes()
    def factory(trace):
        llm = ScriptedLLM([reply(calls=[call(TOOL_NAME, json.dumps(ARGS))]) if mode is None else reply(calls=[call()]),
            reply("analysis"), reply(json.dumps(proposal("main.cpp"))),
            reply(calls=[call(TOOL_NAME, json.dumps(ARGS))]) if mode is None else reply(calls=[call()]),
            reply(calls=[call("read_file", '{"path":"main.cpp"}', "read-source"),
                         write("main.cpp", "int main(){return 0;}\n")]), reply("modified")])
        service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe,
                              trace=trace, mcp_config=config(tmp_path, mode))
        instances.append(service)
        return service
    store = SqliteEventStore(tmp_path / "http.sqlite3")
    app = create_app(factory, store=store)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "offline fixture"})
                assert created.status_code == 202
                task_id = created.json()["task_id"]
                pending = await until(client, task_id, {"waiting_approval", "failed"})
                assert pending["state"] == "waiting_approval" and instances[0]._mcp.client is None
                assert (cpp_repo / "main.cpp").read_bytes() == before
                assert not (await instances[0]._runtime.registry.execute("write_file", {"path": "main.cpp", "content": "forbidden"})).success
                assert (cpp_repo / "main.cpp").read_bytes() == before
                approved = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
                assert approved.status_code == 202
                completed = await until(client, task_id, {"completed", "failed"})
                assert completed["state"] == "completed" and completed["tool_calls"] == 4
                assert completed["steps"] == 6 and completed["approved_files"] == ["main.cpp"]
                assert instances[0]._mcp.client is None and instances[0]._lock.handle is None
                stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
                status = next(p["data"] for _, p in stream if p["event"] == "message" and p["data"].get("kind") == "mcp_status")
                assert status["content"] and status["status"] == ("ready" if mode is None else "unavailable")
                assert stream[-1][1]["event"] == "task_completed"
                assert (cpp_repo / "main.cpp").read_text() == "int main(){return 0;}\n"
    finally: store.close()


@pytest.mark.asyncio
async def test_cli_uses_explicit_config_and_offline_model(cpp_repo, tmp_path, rg_exe, git_exe, monkeypatch, capsys):
    from forgepilot.cli import analyze, parser
    path = tmp_path / "server.json"
    path.write_text(config(tmp_path).model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("FORGEPILOT_API_KEY", "sk-development-secret")
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        data = json.loads(request.content)
        if count == 1:
            assert any(t["function"]["name"] == TOOL_NAME for t in data["tools"])
            message = {"role": "assistant", "tool_calls": [call(TOOL_NAME, json.dumps(ARGS))]}
        else:
            assert json.loads(data["messages"][-1]["content"])["success"]
            message = {"role": "assistant", "content": "OFFLINE MCP CLI COMPLETE"}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if count == 1 else "stop"}]})
    args = parser().parse_args([str(cpp_repo), "offline MCP", "--analysis-only", "--base-url", "http://localhost/v1",
        "--model", "offline-fixture", "--rg", rg_exe, "--git", git_exe, "--mcp-config", str(path)])
    assert await analyze(args, transport=httpx.MockTransport(respond)) == 0
    text = capsys.readouterr().out
    assert "OFFLINE MCP CLI COMPLETE" in text and "mcp_status" in text and "sk-development-secret" not in text
