from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import pytest

from conftest import call
from test_build import build_config, build_repo
from test_changes import write
from test_planning import proposal


@pytest.mark.parametrize("repairing", [False, True])
def test_real_cli_mvp_local_http_and_real_cmake(build_repo, build_config, rg_exe, git_exe, repairing):
    original = (build_repo / "main.cpp").read_bytes()
    requests, errors = [], []
    plan = proposal("main.cpp")
    plan["validation_steps"] = ["Configure, Build and CTest the selected Debug configuration"]
    messages = [
        {"role": "assistant", "content": "main.cpp 的返回值导致 CTest 失败。"},
        {"role": "assistant", "content": json.dumps(plan)},
        {"role": "assistant", "content": None, "tool_calls": [call("read_file", '{"path":"main.cpp"}', "read-source"),
            write("main.cpp", "int main(){return 0;}\n// existing user change\n")]},
        {"role": "assistant", "content": "已修改返回值，交给 Runtime 执行验证。"},
    ]
    if repairing:
        from test_repair import reflection
        messages[2] = {"role": "assistant", "tool_calls": [call("read_file", '{"path":"main.cpp"}', "read-source"),
            write("main.cpp", "int main(){return missing_cli_name;}\n// existing user change\n")]}
        messages += [reflection().message.model_dump(exclude_none=True),
            {"role": "assistant", "tool_calls": [write("main.cpp", "int main(){return 0;}\n// existing user change\n")]},
            {"role": "assistant", "content": "Repair implemented; awaiting validation"}]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(data)
                assert not any(t["function"]["name"] in {"cmake_configure", "cmake_build", "run_tests", "shell"} for t in data.get("tools", []))
                if len(requests) == 4:
                    observation = next(m for m in reversed(data["messages"]) if m["role"] == "tool")
                    assert observation["tool_call_id"] == "write" and json.loads(observation["content"])["success"]
                message = messages[len(requests) - 1]
                payload = json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(500)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = [sys.executable, str(Path(__file__).parents[1] / "main.py"), str(build_repo), "修复返回值并验证",
            "--base-url", f"http://127.0.0.1:{server.server_port}/v1", "--model", "offline-phase4",
            "--rg", rg_exe, "--git", git_exe, "--cmake", build_config.cmake, "--ctest", build_config.ctest,
            "--generator", "Ninja", "--make-program", build_config.make_program]
        result = subprocess.run(command, input="approve 1\nrestore\n", capture_output=True, text=True, encoding="utf-8", timeout=90,
                                env={**os.environ, "FORGEPILOT_API_KEY": "fake-phase4-key", "PYTHONIOENCODING": "utf-8"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not errors and len(requests) == (7 if repairing else 4)
        assert "BuildSelection=" in result.stdout and "BuildResult=" in result.stdout
        report = json.loads(next(line.removeprefix("BuildResult=") for line in result.stdout.splitlines() if line.startswith("BuildResult=")))
        assert report["tests"]["passed"] == 1 and report["tests"]["total"] == 1
        stats = json.loads(result.stdout.splitlines()[-1])
        assert stats["state"] == "completed" and stats["outcome"] == "modification_validated"
        assert stats["approved_files"] == ["main.cpp"] and stats["usage"]["total_tokens"] == (105 if repairing else 60)
        assert stats["repair_round"] == int(repairing)
        history = json.loads(next(line.removeprefix("ValidationHistory=") for line in result.stdout.splitlines() if line.startswith("ValidationHistory=")))
        assert len(history) == (2 if repairing else 1)
        if repairing:
            assert history[0]["stages"]["cmake_build"] == "failed" and history[0]["tests"]["total"] is None
            assert "reflection" in result.stdout and "missing_cli_name" in json.dumps(requests[4])
        assert "fake-phase4-key" not in result.stdout + result.stderr
        assert (build_repo / "main.cpp").read_bytes() == original
        print("OFFLINE MODEL / REAL CLI, HTTP, WRITE, CONFIGURE, BUILD, CTEST AND RESTORE")
        print(result.stdout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
