from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from conftest import call
from test_planning import proposal, contents


@pytest.mark.parametrize("decision,restore,expected_code", [
    ("approve 2", True, 0), ("approve 2", False, 0), ("reject 2", True, 130),
])
def test_real_modifying_cli_reapproval_and_explicit_restore(cpp_repo, rg_exe, git_exe, decision, restore, expected_code):
    # Canonical patches require uniform newlines; mixed-newline rejection has
    # separate editor coverage. Keep the pre-existing user change in this fixture.
    source = cpp_repo / "main.cpp"
    source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    original = (cpp_repo / "main.cpp").read_bytes()
    before = contents(cpp_repo)
    requests, errors = [], []
    key = "fake-phase3-key"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(data)
                step = len(requests)
                if step == 1:
                    assert "repository_apply_patch" not in [t["function"]["name"] for t in data["tools"]]
                    message = {"role": "assistant", "content": "分析完成，需要修改 main.cpp。"}
                elif step == 2:
                    assert "tools" not in data
                    message = {"role": "assistant", "content": json.dumps(proposal("main.cpp"))}
                elif step == 3:
                    assert "repository_apply_patch" in [t["function"]["name"] for t in data["tools"]]
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        call("repository_read_range", '{"path":"main.cpp"}', "read-source"),
                        call("repository_apply_patch", json.dumps({"path": "main.cpp",
                            "expected_hash": hashlib.sha256(original).hexdigest(), "edits": [
                                {"old_text": original.decode(), "new_text": original.decode() + "// CLI Agent change\n"}]}), "write"),
                        call("repository_create_file", '{"path":"Extra.cpp","content":"extra\\n"}', "extra")]}
                elif step == 4:
                    assert not (cpp_repo / "Extra.cpp").exists()
                    observations = [m for m in data["messages"] if m["role"] == "tool"]
                    assert json.loads(observations[-2]["content"])["success"], observations[-2]["content"]
                    assert "approval_required" in observations[-1]["content"]
                    message = {"role": "assistant", "content": json.dumps(proposal("main.cpp", "Extra.cpp"))}
                elif step == 5:
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        call("repository_create_file", '{"path":"Extra.cpp","content":"extra\\n"}', "write")]}
                else:
                    assert step == 6
                    message = {"role": "assistant", "content": "文件修改完成，尚未编译或测试。"}
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
        args = [sys.executable, str(Path(__file__).parents[1] / "main.py"), str(cpp_repo), "修改并展示任务 Diff",
                "--base-url", f"http://127.0.0.1:{server.server_port}/v1", "--model", "offline-phase3",
                "--rg", rg_exe, "--git", git_exe, "--modify-only"]
        result = subprocess.run(args, input=f"approve 1\n{decision}\n{'restore' if restore else 'keep'}\n",
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "FORGEPILOT_API_KEY": key, "PYTHONIOENCODING": "utf-8"})
        assert not errors, errors
        assert result.returncode == expected_code, result.stdout + result.stderr
        assert "version=1" in result.stdout and "version=2" in result.stdout
        assert "TaskDiff=" in result.stdout
        if expected_code == 0:
            assert "未执行 Build/Test" in result.stdout
        stats = json.loads(result.stdout.splitlines()[-1])
        assert stats["plan_version"] == 2
        assert key not in result.stdout + result.stderr
        assert (cpp_repo / ".git/index").read_bytes() == before[".git/index"]
        if restore:
            assert '"conflicts": 0' in result.stdout and contents(cpp_repo) == before
        else:
            assert (cpp_repo / "Extra.cpp").read_bytes() == b"extra\n"
            newline = b"\r\n" if b"\r\n" in original else b"\n"
            assert (cpp_repo / "main.cpp").read_bytes() == original + b"// CLI Agent change" + newline
        if decision == "approve 2" and restore:
            print("OFFLINE PHASE 3 — real CLI/HTTP/Git/file writes, scripted LLM, explicit reapproval and restore")
            print(result.stdout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
