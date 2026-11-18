"""An owned desktop restart validates, freezes admission, then replaces processes."""
import importlib.util
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from forgepilot.errors import ForgePilotError


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("desktop_restart_launcher", ROOT / "scripts/desktop_launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class OwnerFixture:
    """Real owner control flow with deterministic owned children and kernel handles."""

    def __init__(self, tmp_path, monkeypatch, failure=None):
        self.events, self.closed = [], set()
        self.signals = {"stop": False, "restart": True, "show": False}
        self.mutex_released = False
        self.config = tmp_path / "settings/config.json"
        self.config.parent.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.config.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "model": "offline",
            "port": port, "server_args": []}), encoding="utf-8")
        self.result = self.config.with_name("start-result.json")
        self.request_id = "2" * 32
        launcher.write_json(self.config.with_name("restart-request.json"), {"request_id": self.request_id})
        executable = tmp_path / "client/build/Release/ForgePilotClient.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"offline client marker")
        monkeypatch.setattr(launcher, "ROOT", tmp_path)
        monkeypatch.setenv("FORGEPILOT_API_KEY", "old-offline-key")
        monkeypatch.setenv("FORGEPILOT_LOCAL_API_TOKEN", "private-local-token")
        monkeypatch.setenv("FORGEPILOT_RUNTIME_CONFIG", str(self.config))
        api = SimpleNamespace(CreateMutexW=lambda *_: "mutex", WaitForSingleObject=lambda *_: 0,
            CreateEventW=lambda *args: args[-1].rsplit("-", 1)[-1],
            ResetEvent=self._reset, CloseHandle=lambda handle: self.events.append("close:" + handle),
            ReleaseMutex=lambda handle: self._release(handle))
        self.kernel = SimpleNamespace(api=api, name="offline-owner", signaled=self.signals.__getitem__)
        monkeypatch.setattr(launcher, "Kernel", lambda config: self.kernel)
        read_config = launcher.read_config
        reads = []

        def config_read(path):
            reads.append(path)
            self.events.append("validate_config")
            if failure == "config" and len(reads) > 1:
                raise ValueError("model_config")
            return read_config(path)

        monkeypatch.setattr(launcher, "read_config", config_read)

        def key_read(path):
            self.events.append("validate_key")
            if failure == "key":
                raise ForgePilotError("desktop_credential", "offline credential unavailable")
            return "new-offline-key"

        monkeypatch.setattr(launcher.desktop_configuration, "load_saved_key", key_read)

        def backend_state(port, token, *, prepare=False):
            assert token == "private-local-token"
            self.events.append("prepare" if prepare else "status")
            if prepare and failure == "busy":
                raise ForgePilotError("runtime_busy", "offline work is active")
            return {"active_count": 1 if failure == "busy" else 0, "restarting": prepare}

        monkeypatch.setattr(launcher, "_backend_runtime", backend_state)
        self.jobs = []
        fixture = self

        class Job:
            def __init__(self):
                self.name = "job-" + str(len(fixture.jobs))
                fixture.jobs.append(self)

            def close(self):
                fixture.closed.add(self.name)
                fixture.events.append("close:" + self.name)

        monkeypatch.setattr(launcher, "ProcessJob", Job)

        class Process:
            def __init__(self, name, pid):
                self.name, self.pid, self.returncode = name, pid, None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                fixture.events.append("wait:" + self.name)
                self.returncode = 0
                return 0

        def popen(command, **kwargs):
            Path(command[command.index("--ready") + 1]).touch()
            return Process("backend", 101)

        monkeypatch.setattr(launcher.subprocess, "Popen", popen)
        monkeypatch.setattr(launcher, "owned_client", lambda *_: Process("client", 202))
        monkeypatch.setattr(launcher, "client_window", lambda *_: 303)

        def request_stop(kernel, handle):
            assert handle == "stop"
            self.events.append("stop_backend")
            self.signals["stop"] = True

        monkeypatch.setattr(launcher, "request_stop", request_stop)
        write_json = launcher.write_json

        def write(path, state):
            if state.get("restart_request_id") == self.request_id:
                event = "accepted" if state.get("restart_accepted") else "rejected"
                if event not in self.events:
                    self.events.append(event)
            write_json(path, state)

        monkeypatch.setattr(launcher, "write_json", write)

        def sleep(_):
            # Let a denied owner keep running until a separate user stop.
            if "rejected" in self.events:
                self.events.append("user_stop")
                self.signals["stop"] = True

        monkeypatch.setattr(launcher.time, "sleep", sleep)

        def start(path):
            assert self.mutex_released and self.closed == {"job-0", "job-1"}
            assert {"close:stop", "close:restart", "close:show", "close:mutex"} <= set(self.events)
            assert os.environ["FORGEPILOT_API_KEY"] == "new-offline-key"
            self.events.append("start_new_owner")
            return 0

        monkeypatch.setattr(launcher, "start", start)

    def _reset(self, handle):
        self.signals[handle] = False

    def _release(self, handle):
        assert handle == "mutex"
        self.mutex_released = True


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop owner")
def test_owner_restart_outlives_client_job_and_releases_ownership_before_start(tmp_path, monkeypatch):
    fixture = OwnerFixture(tmp_path, monkeypatch)
    assert launcher.supervise(fixture.config, fixture.result) == 0
    events = fixture.events
    assert events.index("validate_key") < events.index("prepare") < events.index("accepted")
    assert events.index("accepted") < events.index("close:job-1") < events.index("start_new_owner")
    assert events.index("close:mutex") < events.index("start_new_owner")
    assert events.count("prepare") == 1 and events.count("start_new_owner") == 1
    state = json.loads(fixture.config.with_name("runtime.json").read_text(encoding="utf-8"))
    assert state["restart_accepted"] and state["restart_request_id"] == fixture.request_id
    assert "private-local-token" not in json.dumps(state)


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop owner")
@pytest.mark.parametrize("failure", ["busy", "config", "key"])
def test_denied_restart_keeps_old_owner_until_separate_user_stop(tmp_path, monkeypatch, failure):
    fixture = OwnerFixture(tmp_path, monkeypatch, failure)
    assert launcher.supervise(fixture.config, fixture.result) == 0
    assert "accepted" not in fixture.events and "start_new_owner" not in fixture.events
    assert fixture.events.index("rejected") < fixture.events.index("user_stop") < fixture.events.index("stop_backend")
    assert fixture.events.count("prepare") == (1 if failure == "busy" else 0)
    state = json.loads(fixture.config.with_name("runtime.json").read_text(encoding="utf-8"))
    assert state["restart_accepted"] is False
    assert state["restart_error"] == ("runtime_busy" if failure == "busy" else
                                      "desktop_credential" if failure == "key" else "restart_unavailable")


@pytest.mark.parametrize("accepted", [False, True])
def test_helper_requests_owner_ack_without_owning_the_restart(tmp_path, monkeypatch, accepted):
    config = tmp_path / "config.json"
    signals, closed = [], []
    kernel = SimpleNamespace(running=lambda: True, restart_event=lambda: "restart",
                             api=SimpleNamespace(CloseHandle=closed.append))
    monkeypatch.setattr(launcher, "Kernel", lambda _: kernel)

    def signal(_kernel, handle):
        signals.append(handle)
        intent = json.loads(config.with_name("restart-request.json").read_text(encoding="utf-8"))
        assert set(intent) == {"request_id"}
        launcher.write_json(config.with_name("runtime.json"), {"restart_request_id": intent["request_id"],
            "restart_accepted": accepted, "restart_error": None if accepted else "runtime_busy"})

    monkeypatch.setattr(launcher, "request_stop", signal)
    monkeypatch.setattr(launcher, "start", lambda *_: pytest.fail("Helper must leave replacement to the owner"))
    response = launcher.request_restart(config)
    assert response["ok"] is accepted and response["restart_required"] is not accepted
    assert signals == closed == ["restart"]


def test_uncertain_restart_ack_is_not_replayed(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    signals = []
    kernel = SimpleNamespace(running=lambda: True, restart_event=lambda: "restart",
                             api=SimpleNamespace(CloseHandle=lambda *_: None))
    monkeypatch.setattr(launcher, "Kernel", lambda _: kernel)
    monkeypatch.setattr(launcher, "request_stop", lambda _, handle: signals.append(handle))
    ticks = iter(range(0, 20, 2))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(launcher.time, "sleep", lambda *_: None)
    response = launcher.request_restart(config)
    assert not response["ok"] and response["restart_required"]
    assert signals == ["restart"]


def test_backend_reads_launch_snapshot_after_saved_configuration_changes(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    original = {"base_url": "http://127.0.0.1:9/v1", "model": "frozen-offline-model",
                "port": 8765, "server_args": []}
    launch_config = tmp_path / "launch-config.json"
    launch_config.write_text(json.dumps(original), encoding="utf-8")
    config.write_text(json.dumps({**original, "base_url": "http://127.0.0.1:8/v1",
                                  "model": "saved-for-next-start"}), encoding="utf-8")
    monkeypatch.setenv("FORGEPILOT_LAUNCH_CONFIG", str(launch_config))
    for variable in ("FORGEPILOT_BASE_URL", "FORGEPILOT_MODEL", "FORGEPILOT_CHAT_MODEL",
                     "FORGEPILOT_TASK_MODEL", "FORGEPILOT_SUMMARY_MODEL"):
        monkeypatch.setenv(variable, "old-test-environment")
    closed = []
    kernel = SimpleNamespace(join=lambda _: None, event=lambda: "stop",
                             api=SimpleNamespace(CloseHandle=closed.append))
    monkeypatch.setattr(launcher, "Kernel", lambda _: kernel)
    seen = []

    def serve(arguments, **kwargs):
        seen.append(arguments)
        assert os.environ["FORGEPILOT_BASE_URL"] == original["base_url"]
        assert all(os.environ[variable] == original["model"] for variable in (
            "FORGEPILOT_CHAT_MODEL", "FORGEPILOT_TASK_MODEL", "FORGEPILOT_SUMMARY_MODEL"))
        return 0

    monkeypatch.setattr("forgepilot.server.main", serve)
    assert launcher.backend(config, "owned-job", tmp_path / "ready", tmp_path / "backend.log") == 0
    assert len(seen) == 1 and closed == ["stop"]
    assert json.loads(config.read_text(encoding="utf-8"))["model"] == "saved-for-next-start"


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop owner")
def test_invalid_optional_embedding_key_disables_only_current_rag_launch(tmp_path, monkeypatch):
    fixture = OwnerFixture(tmp_path, monkeypatch)
    fixture.signals["restart"] = False
    data = json.loads(fixture.config.read_text(encoding="utf-8"))
    data.update(server_args=["--enable-rag"], embedding_credential_file="embedding-key-" + "1" * 32 + ".dpapi")
    fixture.config.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("FORGEPILOT_EMBEDDING_API_KEY", "old-optional-key-must-not-be-reused")

    def unavailable(_):
        raise launcher.desktop_configuration.ConfigurationError("credential_unavailable", "offline corrupt optional key", "embedding")

    monkeypatch.setattr(launcher.desktop_configuration, "load_embedding_key", unavailable)
    popen = launcher.subprocess.Popen
    launched = []

    def capture(command, **kwargs):
        launched.append(kwargs["env"])
        return popen(command, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", capture)

    def status(*_, prepare=False):
        assert not prepare
        fixture.signals["stop"] = True  # A separate user stop after the ordinary startup.
        return {"active_count": 0, "restarting": False}

    monkeypatch.setattr(launcher, "_backend_runtime", status)
    assert launcher.supervise(fixture.config, fixture.result) == 0
    assert len(launched) == 1 and launched[0]["FORGEPILOT_API_KEY"] == "old-offline-key"
    assert launched[0]["FORGEPILOT_EMBEDDING_API_KEY"] == ""
    snapshot = json.loads(Path(launched[0]["FORGEPILOT_LAUNCH_CONFIG"]).read_text(encoding="utf-8"))
    assert "--enable-rag" not in snapshot["server_args"]
    assert "--enable-rag" in json.loads(fixture.config.read_text(encoding="utf-8"))["server_args"]
    state = json.loads(fixture.config.with_name("runtime.json").read_text(encoding="utf-8"))
    assert state["optional_warning"] == "embedding_credential_unavailable"
    assert "start_new_owner" not in fixture.events and "rejected" not in fixture.events
