"""The configuration helper remains discoverable in source and frozen layouts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runtime_release_smoke", ROOT / "scripts/release_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_source_helper_command_uses_isolated_launcher_without_secret_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "frozen", False, raising=False)
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    config = tmp_path / "local settings" / "config.json"
    assert smoke.configuration_helper_command(config) == [
        sys.executable, "-I", str(tmp_path / "scripts/desktop_launcher.py"),
        "config-helper", "--config", str(config)]


def test_frozen_helper_command_needs_only_the_packaged_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "frozen", True, raising=False)
    monkeypatch.setattr(smoke.sys, "executable", str(tmp_path / "bin/ForgePilotAgent.exe"))
    config = tmp_path / "local settings/config.json"
    command = smoke.configuration_helper_command(config)
    assert command == [str(tmp_path / "bin/ForgePilotAgent.exe"), "config-helper", "--config", str(config)]
    assert not any("python" in item.lower() or item.endswith(".py") for item in command)


def test_real_source_helper_can_read_first_run_without_configuration_or_backend(tmp_path):
    environment = {**os.environ, "LOCALAPPDATA": str(tmp_path / "local data"),
                   "FORGEPILOT_API_KEY": "offline-release-do-not-expose"}
    assert smoke.configuration_helper_smoke(tmp_path, environment)
    assert not (tmp_path / "unconfigured/config.json").exists()
    assert not (tmp_path / "unconfigured/api-key.dpapi").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installation identity")
@pytest.mark.parametrize("entry", ["scripts/desktop_launcher.py", "agent/desktop.py", "bin/ForgePilotAgent.exe"])
def test_config_helper_defaults_to_invoked_installation(tmp_path, monkeypatch, entry):
    import desktop

    # ToLowerInvariant uses simple casing for İ and for the final Greek sigma.
    install = tmp_path / "ForgePilot-İΟΣ"
    local = tmp_path / "local data"
    identity = hashlib.sha256((os.path.normcase(str(tmp_path)) + "\\forgepilot-İοσ").encode()).hexdigest()[:16].upper()
    expected = local / "ForgePilot" / "launcher" / identity / "config.json"
    executable = install / entry
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(desktop, "FROZEN", entry.endswith(".exe"))
    if desktop.FROZEN:
        monkeypatch.setattr(sys, "executable", str(executable))
    invoked = executable.name if desktop.FROZEN else os.path.relpath(executable)
    monkeypatch.setattr(sys, "argv", [invoked, "config-helper"])
    captured = []
    monkeypatch.setattr(desktop, "configuration_helper", captured.append)
    desktop.main()
    assert captured == [expected]


@pytest.mark.parametrize("output,returncode", [(b"not-json", 0), (b"[]", 0), (b"{}", 0), (b"{}", 1)])
def test_release_preflight_cannot_report_unavailable_helper_as_success(monkeypatch, tmp_path, output, returncode):
    sent = []

    def run(command, **kwargs):
        sent.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout=output, stderr=b"private helper failure")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    environment = {"FORGEPILOT_API_KEY": "offline-private-key"}
    assert not smoke.configuration_helper_smoke(tmp_path, environment)
    command, kwargs = sent[0]
    assert "offline-private-key" not in json.dumps(command)
    assert kwargs["input"] == b'{"action":"load"}\n'
    assert "FORGEPILOT_API_KEY" not in kwargs["env"]
    assert environment["FORGEPILOT_API_KEY"] == "offline-private-key"


def test_release_preflight_bounds_a_hung_helper(monkeypatch, tmp_path):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(smoke.subprocess, "run", run)
    assert not smoke.configuration_helper_smoke(tmp_path, {})
