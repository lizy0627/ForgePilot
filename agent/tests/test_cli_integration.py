import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from forgepilot.cli import analyze, parser
from forgepilot.context import TaskState
from forgepilot.llm import OpenAICompatibleClient
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from conftest import call, reply

KEY = "fake-integration-key-not-a-real-credential"


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class FixtureModel:
    """Deterministic mock checks actual observations before returning a fixed answer."""
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        data = json.loads(request.content)
        self.requests.append(data)
        assert {t["function"]["name"] for t in data["tools"]} == {"list_files", "read_file", "search_code", "find_symbol", "find_references"}
        if len(self.requests) == 1:
            calls = [call("list_files", "{}", "list"), call("search_code", '{"query":"Counter"}', "search")]
            response = reply(None, calls, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        elif len(self.requests) == 2:
            observations = {m["tool_call_id"]: json.loads(m["content"]) for m in data["messages"] if m["role"] == "tool"}
            assert "Counter.cpp" in observations["list"]["output"]
            assert "App.cpp" in observations["search"]["output"]
            response = reply(None, [call("read_file", json.dumps({"path": name}), name)
                                    for name in ("Counter.hpp", "Counter.cpp", "App.cpp", "main.cpp")])
        else:
            observations = {m["tool_call_id"]: json.loads(m["content"]) for m in data["messages"] if m["role"] == "tool"}
            assert "3: void Counter::increment()" in observations["Counter.cpp"]["output"]
            assert "6:         counter_.increment();" in observations["App.cpp"]["output"]
            assert "4:     return runApp()" in observations["main.cpp"]["output"]
            response = reply("离线 fixture 分析：Counter 定义在 Counter.hpp:3，实现在 Counter.cpp:3、7。"
                             "调用关系：main.cpp:4 → App.cpp:13 runApp → App.cpp:15 onClicked → "
                             "App.cpp:6 Counter::increment；随后 App.cpp:7 调用 value。"
                             "依据源码文本，未使用 clangd 语义索引，也未编译或修改仓库。")
        return httpx.Response(200, json={"choices": [{"message": response.message.model_dump(),
            "finish_reason": "tool_calls" if response.message.tool_calls else "stop"}], "usage": response.usage})


@pytest.mark.asyncio
async def test_full_cli_analysis_with_real_git_rg_and_mock_llm(cpp_repo, rg_exe, git_exe, monkeypatch, capsys):
    monkeypatch.setenv("FORGEPILOT_API_KEY", KEY)
    before = snapshot(cpp_repo)
    args = parser().parse_args([str(cpp_repo), "找到 Counter 类实现并说明调用关系", "--base-url", "https://fake.test/v1",
                               "--model", "offline-fixture", "--rg", rg_exe, "--git", git_exe, "--analysis-only"])
    model = FixtureModel()
    code = await analyze(args, transport=httpx.MockTransport(model))
    output = capsys.readouterr().out
    assert code == 0 and len(model.requests) == 3
    assert "Counter.cpp:3" in output and "main.cpp:4" in output
    assert "tool_started" in output and "tool_finished" in output
    assert '"tool_calls": 6' in output and '"state": "completed"' in output
    assert '"total_tokens": null' in output  # Mock omitted usage on two responses.
    assert KEY not in output
    assert snapshot(cpp_repo) == before  # Includes Git index and existing user edits.


@pytest.mark.asyncio
async def test_service_cancel_busy_and_invalid_repository(cpp_repo, tmp_path, rg_exe, git_exe):
    started = asyncio.Event()
    class SlowLLM:
        async def chat(self, messages, tools=None):
            started.set()
            await asyncio.sleep(10)
    trace = MemoryTrace()
    service = TaskService(SlowLLM(), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    assert not service.cancel()
    task = asyncio.create_task(service.analyze(str(cpp_repo), "analyze"))
    await started.wait()
    from forgepilot.errors import ForgePilotError
    with pytest.raises(ForgePilotError, match="task_busy"):
        await service.analyze(str(cpp_repo), "another")
    assert service.cancel()
    assert (await task).state == TaskState.CANCELLED
    assert not service.cancel()
    failed = await service.analyze(str(tmp_path / "missing"), "analyze")
    assert failed.state == TaskState.FAILED and failed.steps == 0
    empty = await service.analyze(str(cpp_repo), "  ")
    assert empty.error.startswith("invalid_prompt")


@pytest.mark.asyncio
async def test_key_never_reaches_trace_even_if_response_echoes_it(cpp_repo, rg_exe, git_exe):
    trace = MemoryTrace()
    body = {"choices": [{"message": {"role": "assistant", "content": KEY}, "finish_reason": "stop"}]}
    async with OpenAICompatibleClient("https://fake.test/v1", KEY, "fake",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as llm:
        result = await TaskService(llm, rg_executable=rg_exe, git_executable=git_exe,
                                   trace=trace).analyze(str(cpp_repo), KEY)
    assert trace.events
    assert KEY not in json.dumps([asdict(e) for e in trace.events])
    assert KEY not in json.dumps(asdict(result), default=str)


def test_cli_help_missing_arguments_and_no_credentials(cpp_repo, monkeypatch):
    main = Path(__file__).parents[1] / "main.py"
    monkeypatch.delenv("FORGEPILOT_API_KEY", raising=False)
    help_result = subprocess.run([sys.executable, str(main), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0 and "repository_path" in help_result.stdout
    missing = subprocess.run([sys.executable, str(main), str(cpp_repo)], capture_output=True, text=True)
    assert missing.returncode == 2
    no_key = subprocess.run([sys.executable, str(main), str(cpp_repo), "analyze"], capture_output=True, text=True)
    assert no_key.returncode == 2 and "llm_config" in no_key.stderr


def test_real_cli_process_against_local_mock_server(cpp_repo, rg_exe, git_exe):
    """Real CLI + real HTTP + real Git/rg; responses are explicitly scripted."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    model = FixtureModel()
    errors = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log HTTP requests/headers, including the fake auth key.
        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                body = self.rfile.read(int(self.headers["Content-Length"]))
                response = model(httpx.Request("POST", "http://localhost" + self.path, content=body))
                payload = response.content
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = snapshot(cpp_repo)
    try:
        command = [sys.executable, str(Path(__file__).parents[1] / "main.py"), str(cpp_repo),
                   "找到 Counter 类实现并说明调用关系", "--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                   "--model", "offline-fixture", "--rg", rg_exe, "--git", git_exe, "--analysis-only"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20,
                                env={**os.environ, "FORGEPILOT_API_KEY": KEY, "PYTHONIOENCODING": "utf-8"})
        assert not errors
        assert result.returncode == 0, result.stderr
        assert len(model.requests) == 3
        assert '"state": "completed"' in result.stdout and "Counter.cpp:3" in result.stdout
        assert KEY not in result.stdout + result.stderr
        assert snapshot(cpp_repo) == before
        print("OFFLINE MOCK SERVER — real CLI/Git/rg, no external LLM request")
        print(result.stdout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_argument_errors_redact_known_key(cpp_repo, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_API_KEY", KEY)
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "main.py"),
                             str(cpp_repo), "analyze", "--timeout", KEY], capture_output=True, text=True)
    assert result.returncode == 2 and KEY not in result.stderr
