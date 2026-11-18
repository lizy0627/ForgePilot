"""Purpose-specific model selection without external provider requests."""

import asyncio
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.server import parser
from forgepilot.settings import models_from_args, resolve_models, task_model_from_args


def test_legacy_model_remains_the_complete_fallback(monkeypatch):
    for name in ('FORGEPILOT_CHAT_MODEL','FORGEPILOT_TASK_MODEL','FORGEPILOT_SUMMARY_MODEL'):
        monkeypatch.delenv(name,raising=False)
    args = parser().parse_args(["--model", "legacy-model"])
    assert models_from_args(args) == resolve_models(model="legacy-model")
    assert models_from_args(args).chat == "legacy-model"
    assert models_from_args(args).task == "legacy-model"
    assert models_from_args(args).summary == "legacy-model"


def test_partial_and_complete_model_configuration_follow_documented_fallbacks(monkeypatch):
    for name in ('FORGEPILOT_MODEL','FORGEPILOT_CHAT_MODEL','FORGEPILOT_TASK_MODEL','FORGEPILOT_SUMMARY_MODEL'):
        monkeypatch.delenv(name,raising=False)
    partial = parser().parse_args(["--model", "fallback", "--chat-model", "fast-chat"])
    assert models_from_args(partial) == resolve_models(
        model="fallback", chat_model="fast-chat", task_model=None, summary_model=None)
    assert models_from_args(partial).chat == "fast-chat"
    assert models_from_args(partial).task == "fallback"
    assert models_from_args(partial).summary == "fast-chat"

    complete = parser().parse_args(["--chat-model", "chat", "--task-model", "task",
                                    "--summary-model", "summary"])
    assert models_from_args(complete).chat == "chat"
    assert models_from_args(complete).task == "task"
    assert models_from_args(complete).summary == "summary"


def test_environment_models_and_task_cli_selection(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_MODEL", "fallback-env")
    monkeypatch.setenv("FORGEPILOT_CHAT_MODEL", "chat-env")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL", "task-env")
    monkeypatch.delenv("FORGEPILOT_SUMMARY_MODEL", raising=False)
    args = parser().parse_args([])
    assert models_from_args(args).chat == "chat-env"
    assert models_from_args(args).task == "task-env"
    assert models_from_args(args).summary == "chat-env"
    assert task_model_from_args(args) == "task-env"


def test_chat_tool_concurrency_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_CHAT_TOOL_CONCURRENCY", "2")
    assert parser().parse_args([]).chat_tool_concurrency == 2
    assert parser().parse_args(["--chat-tool-concurrency", "8"]).chat_tool_concurrency == 8
    with pytest.raises(SystemExit):
        parser().parse_args(["--chat-tool-concurrency", "0"])


@pytest.mark.parametrize("values, message", [
    ({"chat_model": "chat-only"}, "task"),
    ({"model": "fallback", "task_model": "bad\nmodel"}, "task"),
    ({"model": "fallback", "summary_model": "x" * 257}, "summary"),
])
def test_invalid_or_incomplete_models_name_the_failed_purpose(values, message):
    with pytest.raises(ForgePilotError, match=message):
        resolve_models(**values)


def test_server_constructs_distinct_clients_for_each_purpose(tmp_path, monkeypatch):
    from forgepilot import server

    observed = {}
    created = []

    class Service:
        def __init__(self, llm, **kwargs):
            self.llm = llm

    monkeypatch.setenv("FORGEPILOT_API_KEY", "offline-model-config-key")
    monkeypatch.setattr(server, "TaskService", Service)
    monkeypatch.setattr(server, "executable", lambda configured, fallback: fallback)
    monkeypatch.setattr(server, "build_config_from_args", lambda args: None)

    def run(app, **options):
        task = app.state.task_host.factory(None)
        chat = app.state.conversation_host.chat_factory()
        summary = app.state.conversation_host.summary_factory()
        created.extend((task.llm, chat.llm, summary.llm))
        observed.update(task=task.llm.model, chat=chat.llm.model, summary=summary.llm.model)

    monkeypatch.setattr(server.uvicorn, "run", run)
    result = server.main([
        "--base-url", "http://127.0.0.1:9/v1",
        "--chat-model", "low-latency-chat",
        "--task-model", "high-quality-task",
        "--summary-model", "economical-summary",
        "--database", str(tmp_path / "models.sqlite3"),
        "--chat-metrics-file", str(tmp_path / "metrics.jsonl"),
    ])
    try:
        assert result == 0
        assert observed == {"chat": "low-latency-chat", "task": "high-quality-task",
                            "summary": "economical-summary"}
    finally:
        for client in created:
            asyncio.run(client.aclose())
