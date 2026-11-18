"""Owned worker commands preserve Python development and frozen release paths."""

import sys

import pytest

from forgepilot import frozen_runtime


def test_source_worker_commands_use_isolated_python(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = frozen_runtime.owned_worker_command("build", "job-name")
    assert command[:2] == [sys.executable, "-I"]
    assert command[2].endswith("_build_worker.py")
    assert command[3:] == ["job-name"]
    command = frozen_runtime.owned_worker_command("stdio", "job-name", "clangd.exe", "--background-index")
    assert command[2].endswith("_lsp_worker.py")
    assert command[3:] == ["job-name", "clangd.exe", "--background-index"]


def test_frozen_worker_commands_use_the_bundle_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\ForgePilot\bin\ForgePilotAgent.exe")
    assert frozen_runtime.owned_worker_command("build", "job-name") == [
        sys.executable, "worker", "--job", "job-name"]
    assert frozen_runtime.owned_worker_command("stdio", "job-name", "clangd.exe") == [
        sys.executable, "stdio-worker", "job-name", "clangd.exe"]


def test_unknown_owned_worker_is_rejected():
    with pytest.raises(ValueError, match="unknown owned worker"):
        frozen_runtime.owned_worker_command("unknown", "job-name")
