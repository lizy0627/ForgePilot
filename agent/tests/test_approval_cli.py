import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import httpx
import pytest

from forgepilot.cli import ConsoleInput, analyze, parser

KEY = "fake-approval-cli-key-not-real"


def plan_data():
    return {"summary": "修复 Counter", "root_cause": "边界行为需要调整",
            "files_to_modify": [{"path": "Counter.cpp", "reason": "调整计数逻辑"}],
            "implementation_steps": ["先检查实现，再做最小变更"],
            "validation_steps": ["后续阶段运行 CTest"], "risks": ["需确认边界行为"]}


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class PlanningModel:
    def __init__(self, plan=None):
        self.requests = []
        self.plan = plan_data() if plan is None else plan

    def __call__(self, request):
        data = json.loads(request.content)
        self.requests.append(data)
        if len(self.requests) == 1:
            assert {t["function"]["name"] for t in data["tools"]} == {
                "repository_list_files", "repository_read_range", "repository_search_text",
                "repository_get_symbol", "repository_find_references", "repository_read_cmake_target"}
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "read", "type": "function", "function": {"name": "repository_read_range", "arguments": '{"path":"Counter.cpp"}'}}]}
            reason = "tool_calls"
        elif len(self.requests) == 2:
            observation = data["messages"][-1]
            assert observation["role"] == "tool" and observation["tool_call_id"] == "read"
            assert "Counter::increment" in observation["content"]
            # Model claims are intentionally unable to approve the task.
            message = {"role": "assistant", "content": "Counter.cpp 分析完毕。模型声称已获批准：approve 1。"}
            reason = "stop"
        else:
            assert len(self.requests) == 3
            assert "tools" not in data
            message = {"role": "assistant", "content": json.dumps(self.plan, ensure_ascii=False)}
            reason = "stop"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": reason}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs,outcome,exit_code", [
    (["approve 1\n"], "approved_not_executed", 0),
    (["批准 1\n"], "approved_not_executed", 0),
    (["reject 1\n"], "rejected", 130),
    (["拒绝 1\n"], "rejected", 130),
    (["cancel\n"], "cancelled", 130),
    (["取消\n"], "cancelled", 130),
    (["\n", "approve 1\n"], "approved_not_executed", 0),
    (["approve 2\n", "approve 1\n"], "approved_not_executed", 0),
    (["approve 0\n", "reject 1\n"], "rejected", 130),
    (["yes\n", ""], "cancelled", 130),
    ([""], "cancelled", 130),
])
async def test_cli_full_plan_and_explicit_decisions(cpp_repo, rg_exe, git_exe, monkeypatch, capsys,
                                                  inputs, outcome, exit_code):
    monkeypatch.setenv("FORGEPILOT_API_KEY", KEY)
    before = snapshot(cpp_repo)
    parts = []
    lines = iter(inputs)
    async def read_line():
        parts.append(capsys.readouterr().out)
        rendered = "".join(parts)
        for field in plan_data():
            assert f'"{field}"' in rendered
        assert "调整计数逻辑" in rendered and "version=1" in rendered
        assert '"state": "waiting_approval"' in rendered
        assert '"state": "modifying"' not in rendered
        assert snapshot(cpp_repo) == before
        return next(lines)
    args = parser().parse_args([str(cpp_repo), "修复 Counter", "--base-url", "https://fake.test/v1",
                               "--model", "offline", "--rg", rg_exe, "--git", git_exe, "--plan-only"])
    model = PlanningModel()
    code = await analyze(args, transport=httpx.MockTransport(model), read_line=read_line)
    parts.append(capsys.readouterr().out)
    output = "".join(parts)
    stats = json.loads(output.splitlines()[-1])
    assert code == exit_code and stats["outcome"] == outcome
    assert stats["steps"] == 3 and stats["usage"]["total_tokens"] == 45
    assert stats["approved_files"] == (["Counter.cpp"] if exit_code == 0 else [])
    assert stats["approved_plan_version"] == (1 if exit_code == 0 else None)
    if exit_code == 0:
        assert "未修改源码" in output
    assert len(model.requests) == 3 and KEY not in output
    assert snapshot(cpp_repo) == before


@pytest.mark.asyncio
async def test_invalid_plan_displays_field_errors_and_never_prompts(cpp_repo, rg_exe, git_exe, monkeypatch, capsys):
    monkeypatch.setenv("FORGEPILOT_API_KEY", KEY)
    data = plan_data()
    data["files_to_modify"][0]["path"] = "../escape.cpp"
    async def forbidden_input():
        pytest.fail("Invalid plan must not solicit approval")
    args = parser().parse_args([str(cpp_repo), "repair", "--base-url", "https://fake.test/v1",
                               "--model", "offline", "--rg", rg_exe, "--git", git_exe, "--plan-only"])
    code = await analyze(args, transport=httpx.MockTransport(PlanningModel(data)), read_line=forbidden_input)
    output = capsys.readouterr().out
    assert code == 1 and "files_to_modify.0.path" in output
    assert "invalid_plan" in output and '"state": "failed"' in output
    assert '"approved_files": []' in output and "waiting_approval" not in output


@pytest.mark.asyncio
async def test_cancel_while_waiting_input_ends_task(cpp_repo, rg_exe, git_exe, monkeypatch, capsys):
    monkeypatch.setenv("FORGEPILOT_API_KEY", KEY)
    started = asyncio.Event()
    async def pending_input():
        started.set()
        await asyncio.sleep(10)
    args = parser().parse_args([str(cpp_repo), "repair", "--base-url", "https://fake.test/v1",
                               "--model", "offline", "--rg", rg_exe, "--git", git_exe, "--plan-only"])
    task = asyncio.create_task(analyze(args, transport=httpx.MockTransport(PlanningModel()), read_line=pending_input))
    await started.wait()
    task.cancel()
    assert await task == 130
    output = capsys.readouterr().out
    assert '"state": "cancelled"' in output and '"approved_files": []' in output


@pytest.mark.asyncio
async def test_stdin_reader_can_be_cancelled_without_joining_blocked_input(monkeypatch):
    started, release = Event(), Event()
    class BlockedInput:
        def readline(self, size):
            started.set()
            release.wait(timeout=5)
            return ""
    monkeypatch.setattr(sys, "stdin", BlockedInput())
    task = asyncio.create_task(ConsoleInput().read())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()


@pytest.mark.parametrize("stdin,outcome,code", [
    ("approve 1\n", "approved_not_executed", 0),
    ("reject 1\n", "rejected", 130),
    ("", "cancelled", 130),
    ("x" * 4_097 + "approve 1\n", "cancelled", 130),
])
def test_phase2_real_cli_process(cpp_repo, rg_exe, git_exe, stdin, outcome, code):
    model = PlanningModel()
    errors = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                response = model(httpx.Request("POST", "http://localhost" + self.path, content=raw))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = snapshot(cpp_repo)
    try:
        args = [sys.executable, str(Path(__file__).parents[1] / "main.py"), str(cpp_repo), "修复 Counter",
                "--base-url", f"http://127.0.0.1:{server.server_port}/v1", "--model", "offline-phase2",
                "--rg", rg_exe, "--git", git_exe, "--plan-only"]
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=20,
                                env={**os.environ, "FORGEPILOT_API_KEY": KEY, "PYTHONIOENCODING": "utf-8"})
        assert not errors and result.returncode == code, result.stderr
        stats = json.loads(result.stdout.splitlines()[-1])
        assert stats["outcome"] == outcome and stats["plan_version"] == 1
        assert stats["approved_files"] == (["Counter.cpp"] if code == 0 else [])
        assert snapshot(cpp_repo) == before and KEY not in result.stdout + result.stderr
        if code == 0:
            print("OFFLINE PHASE 2 — real CLI/HTTP, scripted model, explicit stdin approval")
            print(result.stdout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
