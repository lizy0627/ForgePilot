"""Real owner/named-event restart keeps active work and adopts one saved revision."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4

import httpx
import pytest

from forgepilot import desktop_configuration as configuration
from test_build_process import alive


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts/desktop_launcher.py"
OLD_KEY = "offline-restart-old-dpapi-credential"
NEW_KEY = "offline-restart-new-dpapi-credential"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owned desktop lifecycle")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(operation, predicate, *, seconds=20):
    deadline = time.monotonic() + seconds
    result = None
    while time.monotonic() < deadline:
        result = operation()
        if predicate(result):
            return result
        time.sleep(0.05)
    raise AssertionError("Fixture condition did not converge: " + repr(result))


def runtime(config):
    try:
        return json.loads(config.with_name("runtime.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def invoke(action, config, environment, *, request=None, timeout=60):
    command = [sys.executable, "-X", "utf8", "-I", str(LAUNCHER), action, "--config", str(config)]
    assert all(secret not in repr(command) for secret in (OLD_KEY, NEW_KEY))
    return subprocess.run(command, input=json.dumps(request).encode() if request is not None else None,
                          capture_output=True, env=environment, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def audit_owned_argv(state):
    pids = [state[name] for name in ("supervisor_pid", "backend_pid", "client_worker_pid", "client_pid")]
    # Audit only recorded live fixture processes. The credentials enter the audit
    # through stdin, never through the PowerShell process's own command line.
    script = ("$ErrorActionPreference='Stop'; $data=ConvertFrom-Json ([Console]::In.ReadToEnd()); "
              "$count=0; foreach($ownedPid in $data.pids) { "
              "$record=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$ownedPid); "
              "if($record) { foreach($secret in $data.secrets) { "
              "if($record.CommandLine.Contains($secret)) { throw 'Credential found in owned argv' } }; $count++ } }; "
              "[Console]::Out.Write($count)")
    environment = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps({"pids": pids, "secrets": [OLD_KEY, NEW_KEY]}).encode(),
        capture_output=True, timeout=20, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert int(result.stdout) == len(pids)


@contextmanager
def offline_models():
    entered, release = Event(), Event()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            index = len(calls)
            calls.append({"model": payload["model"], "authorization": self.headers.get("Authorization"),
                          "stream": payload.get("stream")})
            if index == 0:
                entered.set()
                if not release.wait(45):
                    self.send_error(504)
                    return
            if payload.get("stream"):
                items = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "offline persisted reply"}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
                ]
                body = ("".join("data: " + json.dumps(item) + "\n\n" for item in items) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "offline persisted reply"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", entered, release, calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_real_restart_refuses_active_chat_then_adopts_saved_models_key_port_and_history(tmp_path):
    config = tmp_path / "settings with spaces" / "config.json"
    token = secrets.token_urlsafe(32)
    environment = {**os.environ, "FORGEPILOT_LOCAL_API_TOKEN": token}
    environment.pop("FORGEPILOT_API_KEY", None)
    environment.pop("FORGEPILOT_EMBEDDING_API_KEY", None)
    states = []
    with offline_models() as (base_url, entered, release, model_calls):
        fields = {**configuration.RuntimeFields().model_dump(), "base_url": base_url, "port": free_port(),
            "chat_model": "chat-old", "task_model": "task-local", "review_model": "review-local",
            "summary_model": "summary-old", "task_model_family": "writer", "review_model_family": "reviewer"}
        loaded = asyncio.run(configuration.handle_request(config, {"action": "load"}))
        saved = asyncio.run(configuration.handle_request(config, {"action": "save", "fields": fields,
            "api_key": OLD_KEY, "config_revision": loaded["config_revision"]}))
        assert saved["ok"], saved
        initial_config = configuration.read_config(config)
        initial_revision = saved["config_revision"]
        try:
            started = invoke("desktop", config, environment)
            assert started.returncode == 0, started.stdout.decode(errors="replace") + started.stderr.decode(errors="replace")
            before = wait_for(lambda: runtime(config), lambda state: state.get("status") == "running")
            states.append(before)
            assert before["running_revision"] == initial_revision and before["port"] == fields["port"]
            audit_owned_argv(before)
            snapshot_path = Path(before["log_directory"]) / "launch-config.json"
            snapshot_bytes = snapshot_path.read_bytes()
            assert json.loads(snapshot_bytes)["server_args"] == initial_config["server_args"]
            old_url = f"http://127.0.0.1:{fields['port']}"
            with httpx.Client(base_url=old_url, headers={"X-ForgePilot-Token": token}, trust_env=False, timeout=5) as client:
                created = client.post("/api/v1/conversations", json={"title": "Persistent restart fixture"})
                assert created.status_code == 201, created.text
                conversation = created.json()["conversation_id"]
                request_id = str(uuid4())
                request_path = f"/api/v1/conversations/{conversation}/requests/{request_id}"
                response = client.post(f"/api/v1/conversations/{conversation}/requests", json={
                    "request_id": request_id, "mode": "chat", "tool_mode": "fast", "prompt": "first offline request"})
                assert response.status_code == 202, response.text
                assert entered.wait(10)
                active = client.get("/api/v2/runtime/status").json()
                assert active["active_count"] == 1 and not active["restarting"]
                changed = {**fields, "port": free_port(), "chat_model": "chat-new", "summary_model": "summary-new"}
                assert changed["port"] != fields["port"]
                updated = asyncio.run(configuration.handle_request(config, {"action": "save", "fields": changed,
                    "api_key": NEW_KEY, "config_revision": initial_revision}))
                assert updated["ok"] and updated["restart_required"]
                assert runtime(config)["backend_pid"] == before["backend_pid"]
                assert runtime(config)["running_revision"] == initial_revision
                assert snapshot_path.read_bytes() == snapshot_bytes
                denied = invoke("config-helper", config, environment, request={
                    "action": "restart", "config_revision": updated["config_revision"]})
                assert denied.returncode == 0, denied.stderr.decode(errors="replace")
                denial = json.loads(denied.stdout)
                assert not denial["ok"] and denial["error_code"] == "runtime_busy", denial
                assert client.get(request_path).json()["status"] == "running"
                assert alive(before["backend_pid"]) and len(model_calls) == 1
                release.set()
                completed = wait_for(lambda: client.get(request_path).json(), lambda value: value.get("status") != "running")
                assert completed["status"] == "completed", completed
                assert model_calls[0]["model"] == "chat-old" and model_calls[0]["authorization"] == "Bearer " + OLD_KEY
                wait_for(lambda: client.get("/api/v2/runtime/status").json(), lambda value: value["active_count"] == 0)
            restarted = invoke("config-helper", config, environment, request={
                "action": "restart", "config_revision": updated["config_revision"]})
            assert restarted.returncode == 0, restarted.stderr.decode(errors="replace")
            accepted = json.loads(restarted.stdout)
            assert accepted["ok"], accepted
            after = wait_for(lambda: runtime(config), lambda state: state.get("status") == "running"
                and state.get("running_revision") == updated["config_revision"], seconds=40)
            states.append(after)
            assert after["port"] == changed["port"] and after["backend_pid"] != before["backend_pid"]
            wait_for(lambda: alive(before["backend_pid"]), lambda value: not value)
            wait_for(lambda: alive(before["client_pid"]), lambda value: not value)
            assert snapshot_path.read_bytes() == snapshot_bytes
            new_snapshot = json.loads((Path(after["log_directory"]) / "launch-config.json").read_text(encoding="utf-8"))
            new_saved_config = configuration.read_config(config)
            assert new_snapshot["server_args"] == new_saved_config["server_args"]
            assert new_snapshot["credential_file"] != initial_config["credential_file"]
            audit_owned_argv(after)
            with httpx.Client(base_url=f"http://127.0.0.1:{changed['port']}", headers={"X-ForgePilot-Token": token}, trust_env=False, timeout=5) as client:
                history = client.get(f"/api/v1/conversations/{conversation}")
                assert history.status_code == 200, history.text
                assert history.json()["last_request"]["request_id"] == request_id
                assert [message["content"] for message in history.json()["messages"]] == ["first offline request", "offline persisted reply"]
                assert len(model_calls) == 1  # Read-only discovery must not replay the first request.
                second = str(uuid4())
                response = client.post(f"/api/v1/conversations/{conversation}/requests", json={
                    "request_id": second, "mode": "chat", "tool_mode": "fast", "prompt": "second offline request"})
                assert response.status_code == 202
                final = wait_for(lambda: client.get(f"/api/v1/conversations/{conversation}/requests/{second}").json(),
                                 lambda value: value.get("status") != "running")
                assert final["status"] == "completed", final
                assert len(model_calls) == 2 and model_calls[1]["model"] == "chat-new"
                assert model_calls[1]["authorization"] == "Bearer " + NEW_KEY
            for state in states:
                text = json.dumps(state)
                for log in Path(state["log_directory"]).glob("*.log"):
                    text += log.read_text(encoding="utf-8", errors="replace")
                assert all(secret not in text for secret in (OLD_KEY, NEW_KEY, token))
            evidence = {"active_restart_rejected": True, "old_request_completed_once": True,
                "old_revision": initial_revision, "new_revision": updated["config_revision"],
                "old_port": fields["port"], "new_port": changed["port"], "history_preserved": True,
                "model_requests": [{"model": call["model"], "stream": call["stream"]} for call in model_calls],
                "old_backend_released": True, "owned_argv_and_logs_secret_free": True}
            (tmp_path / "restart-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        finally:
            release.set()
            stopped = invoke("stop", config, environment)
            assert stopped.returncode == 0, stopped.stdout.decode(errors="replace") + stopped.stderr.decode(errors="replace")
