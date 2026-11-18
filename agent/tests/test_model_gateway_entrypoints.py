"""Entrypoint migration uses fake transports only; no live model credentials."""
import asyncio
import json

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.agents.registry import AgentRegistry
from forgepilot.server import parser
from forgepilot.settings import model_gateway_from_args


def write_config(path, *, routes=("task", "chat", "summary")):
    data = {
        "schema_version": "1.0",
        "profiles": [{
            "profile_id": "configured", "provider": "openai-compatible",
            "base_url": "https://fake.test/v1", "api_key_ref": "env:TEST_GATEWAY_KEY",
            "model": "configured-model", "family": "fixture",
            "supports_streaming": True, "supports_tool_calling": True,
            "timeout_seconds": 2.0, "max_output_tokens": 321, "temperature": 0.25,
        }],
        "routes": [{"route_id": name, "primary": "configured"} for name in routes],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_model_config_argument_and_environment(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_MODEL_CONFIG", "trusted.json")
    assert parser().parse_args([]).model_config == "trusted.json"
    assert parser().parse_args(["--model-config", "explicit.json"]).model_config == "explicit.json"


@pytest.mark.asyncio
async def test_explicit_config_replaces_legacy_flags_and_uses_reference_only(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGEPILOT_API_KEY", raising=False)
    key = "offline-profile-key"
    monkeypatch.setenv("TEST_GATEWAY_KEY", key)
    path = write_config(tmp_path / "models.json")
    requests = []

    def respond(request):
        assert request.headers["Authorization"] == f"Bearer {key}"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    args = parser().parse_args(["--model-config", str(path), "--model", "ignored-model"])
    async with model_gateway_from_args(args, transport=httpx.MockTransport(respond)) as gateway:
        client = gateway.for_route("chat")
        assert client.model == "configured-model"
        assert (await client.chat([{"role": "user", "content": "hello"}])).message.content == "ok"
        assert key not in gateway.redactor.text(key)
    assert len(requests) == 1
    assert requests[0]["model"] == "configured-model"
    assert "max_tokens" not in requests[0] and "max_completion_tokens" not in requests[0]
    assert requests[0]["temperature"] == 0.25
    assert key not in path.read_text(encoding="utf-8")


def test_server_requires_all_use_cases_but_cli_only_requires_task(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "offline-profile-key")
    path = write_config(tmp_path / "models.json", routes=("task",))
    args = parser().parse_args(["--model-config", str(path)])
    with pytest.raises(ForgePilotError, match="llm_config"):
        model_gateway_from_args(args)
    gateway = model_gateway_from_args(args, task_only=True)
    assert gateway.for_route("task").model == "configured-model"
    asyncio.run(gateway.aclose())


def test_server_factories_receive_gateway_handles_with_explicit_configuration(tmp_path, monkeypatch):
    from forgepilot import server
    monkeypatch.delenv("FORGEPILOT_API_KEY", raising=False)
    monkeypatch.setenv("TEST_GATEWAY_KEY", "offline-profile-key")
    path = write_config(tmp_path / "models.json")
    clients = []

    class Service:
        def __init__(self, llm, **kwargs):
            self.llm = llm

    monkeypatch.setattr(server, "TaskService", Service)
    monkeypatch.setattr(server, "executable", lambda configured, fallback: fallback)
    monkeypatch.setattr(server, "build_config_from_args", lambda args: None)

    def run(app, **options):
        clients.extend((app.state.task_host.factory(None).llm,
                        app.state.conversation_host.chat_factory().llm,
                        app.state.conversation_host.summary_factory().llm))

    monkeypatch.setattr(server.uvicorn, "run", run)
    assert server.main(["--model-config", str(path), "--database", str(tmp_path / "history.sqlite3")]) == 0
    assert len(clients) == 3
    assert all(type(client).__module__ == "forgepilot.models.gateway" for client in clients)
    assert {client.model for client in clients} == {"configured-model"}
    assert (tmp_path / "model-accounting.jsonl").is_file()


def test_optional_strict_reviewer_routes_keep_legacy_launch_compatible(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_API_KEY", "offline-profile-key")
    for name in ("FORGEPILOT_REVIEW_MODEL", "FORGEPILOT_TASK_MODEL_FAMILY",
                 "FORGEPILOT_REVIEW_MODEL_FAMILY", "FORGEPILOT_MODEL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    args = parser().parse_args(["--base-url", "https://fake.test/v1", "--model", "task-model"])
    legacy = model_gateway_from_args(args)
    with pytest.raises(ForgePilotError, match="llm_config"):
        AgentRegistry(router=legacy.router)
    asyncio.run(legacy.aclose())

    strict_args = parser().parse_args(["--base-url", "https://fake.test/v1",
        "--task-model", "task-model", "--review-model", "review-model",
        "--task-model-family", "task-family", "--review-model-family", "review-family",
        "--chat-model", "chat-model", "--summary-model", "summary-model"])
    strict = model_gateway_from_args(strict_args)
    try:
        AgentRegistry(router=strict.router)
        assert strict.router.resolve("task").primary.model == "task-model"
        assert strict.router.resolve("implementation", "implementer").primary.model == "task-model"
        assert strict.router.resolve("review", "reviewer").primary.model == "review-model"
        assert strict.router.resolve("review", "reviewer").primary.family == "review-family"
        assert strict.router.resolve("chat").primary.model == "chat-model"
        assert strict.router.resolve("summary").primary.model == "summary-model"
        assert strict.configuration.enforce_review_family_separation is True
    finally:
        asyncio.run(strict.aclose())


@pytest.mark.parametrize("extra", [
    ["--review-model", "review-model"],
    ["--review-model", "task-model", "--task-model-family", "task-family",
     "--review-model-family", "review-family"],
    ["--review-model", "review-model", "--task-model-family", "same-family",
     "--review-model-family", "same-family"],
])
def test_incomplete_or_unfair_reviewer_config_fails_without_provider_call(monkeypatch, extra):
    monkeypatch.setenv("FORGEPILOT_API_KEY", "offline-profile-key")
    for name in ("FORGEPILOT_REVIEW_MODEL", "FORGEPILOT_TASK_MODEL_FAMILY",
                 "FORGEPILOT_REVIEW_MODEL_FAMILY", "FORGEPILOT_MODEL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    args = parser().parse_args(["--base-url", "https://fake.test/v1",
                                "--task-model", "task-model", *extra])
    with pytest.raises(ForgePilotError, match="llm_config"):
        model_gateway_from_args(args)
