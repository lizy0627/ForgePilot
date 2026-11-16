"""Explicit server dependencies preserve lazy startup and shared model ownership."""
import asyncio
import json
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from conftest import reply
from forgepilot import server
from forgepilot.errors import ForgePilotError
from forgepilot.mission_factory import build_mission_workflow
from forgepilot.mission_host import MissionHost
from forgepilot.models.gateway import ModelGateway
from forgepilot.presets import BuildConfig
from forgepilot.service_composition import (
    DevelopmentConfig, ServerResources, compose_server_resources,
)
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from test_analysis_workflow import AnalysisProvider
from test_mission_factory import configuration
from test_mission_service import settled
from test_workspace_manager import committed_repo, git


@pytest.fixture(autouse=True)
def isolated_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_ARTIFACT_ROOT", str(tmp_path / "artifacts"))


class CountingProvider:
    def __init__(self):
        self.calls = 0
        self.closes = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        return reply("offline response")

    async def aclose(self):
        self.closes += 1


def gateway_for(provider, *, omit=None):
    return ModelGateway(configuration(omit=omit), secret_resolver=lambda _: "offline-key",
                        provider_factory=lambda *_: provider)


def development(rg_exe, git_exe, **kwargs):
    return DevelopmentConfig(rg_executable=rg_exe, git_executable=git_exe,
        build_config=BuildConfig(cmake=shutil.which("cmake"), ctest=shutil.which("ctest"), **kwargs))


def unavailable(*args, **kwargs):
    pytest.fail("Mission composition and plain chat must not create legacy TaskService")


def test_server_creates_mission_to_approval_without_legacy_task_service(
        tmp_path, monkeypatch, git_exe, rg_exe, committed_repo):
    """Exercise production assembly, workspace admission and the real analysis graph."""
    (committed_repo / "Counter.cpp").write_text(
        "// Offline lifecycle evidence fixture\nint count() { return 1; }\n", encoding="utf-8")
    git(git_exe, committed_repo, "add", "Counter.cpp")
    git(git_exe, committed_repo, "commit", "-q", "-m", "offline analysis fixture")

    class Provider(AnalysisProvider):
        async def chat(self, messages, tools=None):
            result = await super().chat(messages, tools)
            payload = json.loads(next(item["content"] for item in messages if item["role"] == "user"))
            if "request" in payload:
                content = json.loads(result.message.content)
                content.update(repository=payload["repository"], data_policy=payload["data_policy"])
                return reply(json.dumps(content), usage=result.usage)
            return result

    provider = Provider(committed_repo)
    gateway = gateway_for(provider)
    config = development(rg_exe, git_exe)
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    monkeypatch.setattr(server, "compose_server_resources", lambda *args, **kwargs: resources)
    monkeypatch.setattr(server, "TaskService", unavailable)
    created = []

    def run(app, **options):
        async def exercise():
            async with app.router.lifespan_context(app):
                mission = await app.state.mission_host.create(repository_path=str(committed_repo),
                    request="Fix the worker lifecycle", task_type="bug_fix",
                    orchestration_mode="multi_agent", base_ref="HEAD", toolchain_profile="auto")
                final = await settled(app.state.mission_host, mission["task_id"])
                assert final["status"] == "awaiting_human", final
                assert final["review"] is not None
                workflow = app.state.mission_host._services[mission["task_id"]]
                assert workflow.gateway is gateway
                created.append(mission["task_id"])
        asyncio.run(exercise())

    monkeypatch.setattr(server.uvicorn, "run", run)
    assert server.main(["--database", str(tmp_path / "server.sqlite3"),
                        "--mission-allow-cloud-model"]) == 0
    assert len(created) == 1
    assert provider.counts == {"draft": 1, "root": 1, "impact": 1, "plan": 1}
    assert gateway._closed


@pytest.mark.asyncio
async def test_repeated_mission_assembly_borrows_one_gateway_and_never_owns_its_pool(
        tmp_path, git_exe, rg_exe):
    provider = CountingProvider()
    gateway = gateway_for(provider)
    config = development(rg_exe, git_exe)
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    store = SqliteEventStore(tmp_path / "shared.sqlite3")
    try:
        first = build_mission_workflow(resources.mission_dependencies(store, mcp_connection_factory=unavailable))
        second = build_mission_workflow(resources.mission_dependencies(store, mcp_connection_factory=unavailable))
        assert first.gateway is second.gateway is gateway
        assert first.events is second.events is store
        assert not gateway._providers and provider.calls == 0
        legacy_handle = gateway.for_route("task")
        await legacy_handle.aclose()
        await legacy_handle.chat([{"role": "user", "content": "v1"}])
        await first.gateway.for_route("task").chat([{"role": "user", "content": "v2"}])
        assert provider.calls == 2 and provider.closes == 0
        assert len(gateway._providers) == 1
        await gateway.aclose()
        await gateway.aclose()
        assert provider.closes == 1
    finally:
        await gateway.aclose()
        store.close()


def test_server_openapi_and_plain_chat_do_not_resolve_development_or_mission_routes(
        tmp_path, monkeypatch):
    provider = CountingProvider()
    gateway = gateway_for(provider, omit="analysis")
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=unavailable)
    monkeypatch.setattr(server, "compose_server_resources", lambda *args, **kwargs: resources)
    monkeypatch.setattr(server, "TaskService", unavailable)
    monkeypatch.setattr(server, "executable", unavailable)
    monkeypatch.setattr(server, "build_config_from_args", unavailable)

    def run(app, **options):
        schema = app.openapi()
        assert "/api/v1/tasks" in schema["paths"] and "/api/v2/missions" in schema["paths"]
        chat = app.state.conversation_host.chat_factory()
        summary = app.state.conversation_host.summary_factory()
        assert chat.llm.gateway is summary.llm.gateway is gateway

        async def idle():
            async with app.router.lifespan_context(app):
                assert provider.calls == 0 and not gateway._providers
        asyncio.run(idle())

    monkeypatch.setattr(server.uvicorn, "run", run)
    assert server.main(["--database", str(tmp_path / "chat.sqlite3")]) == 0
    assert provider.calls == 0 and gateway._closed


def test_resource_composition_builds_one_gateway_and_redacts_launch_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSION_FACTORY_TEST_KEY", "offline-profile-key")
    monkeypatch.setenv("FORGEPILOT_API_KEY", "offline-legacy-key")
    path = tmp_path / "models.json"
    path.write_text(json.dumps(configuration(omit="analysis").model_dump(mode="json")), encoding="utf-8")
    args = server.parser().parse_args(["--model-config", str(path)])
    resources = compose_server_resources(args, api_token="offline-local-token", development=unavailable)
    try:
        assert not resources.gateway._providers
        assert resources.gateway.for_route("chat").gateway is resources.gateway
        assert resources.redactor.text("offline-profile-key offline-legacy-key offline-local-token") == (
            "[REDACTED] [REDACTED] [REDACTED]")
    finally:
        asyncio.run(resources.gateway.aclose())


@pytest.mark.parametrize("missing_role,expected", [("analysis", "llm_config"), (None, None)])
def test_role_validation_precedes_workspace_allocation_and_preset_resolution(
        tmp_path, monkeypatch, git_exe, rg_exe, missing_role, expected):
    def before_preview(*args, **kwargs):
        pytest.fail("Mission composition must defer preset resolution and commands until source preview")

    monkeypatch.setattr("forgepilot.presets.Presets.resolve_mission", before_preview)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", before_preview)
    gateway = gateway_for(CountingProvider(), omit=missing_role)
    config = development(rg_exe, git_exe, configure_preset="trusted-preset")
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    store = SqliteEventStore(tmp_path / "invalid.sqlite3")
    try:
        deps = resources.mission_dependencies(store, mcp_connection_factory=unavailable)
        if expected is not None:
            with pytest.raises(ForgePilotError) as caught:
                build_mission_workflow(deps)
            assert caught.value.code == expected
            assert not (store.artifacts.root / "mission-workspaces").exists()
        else:
            workflow = build_mission_workflow(deps)
            assert workflow.mission_build_config is config.build_config
            assert not list((workflow.workspace.root / "worktrees").iterdir())
            assert not list((workflow.workspace.root / "tasks").iterdir())
        assert not gateway._providers
    finally:
        asyncio.run(gateway.aclose())
        store.close()


def test_development_failure_precedes_task_route_resolution(tmp_path):
    gateway = gateway_for(CountingProvider(), omit="task")
    def fail_development():
        raise ForgePilotError("build_config", "offline invalid development tools")
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=fail_development)
    store = SqliteEventStore(tmp_path / "order.sqlite3")
    try:
        with pytest.raises(ForgePilotError) as caught:
            resources.mission_dependencies(store, mcp_connection_factory=unavailable)
        assert caught.value.code == "build_config"
        assert not gateway._providers
    finally:
        asyncio.run(gateway.aclose())
        store.close()


def test_selected_mcp_dependencies_and_cloud_permission_are_explicit(tmp_path, git_exe, rg_exe):
    gateway = gateway_for(CountingProvider())
    config = development(rg_exe, git_exe)
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    store = SqliteEventStore(tmp_path / "mcp.sqlite3")
    server_id = str(uuid4())
    selected = SimpleNamespace(server_id=server_id, revision=3,
        request_redactor=lambda redactor: redactor,
        config=SimpleNamespace(selected_tools=("lookup",), connection=SimpleNamespace(preset_id=None)),
        catalog=[{"name": "lookup", "input_schema": {"type": "object", "properties": {}},
            "output_schema": None, "description": "Offline lookup", "annotations": {"readOnlyHint": True},
            "execution": {}}])
    try:
        deps = resources.mission_dependencies(store, mcp_connection_factory=unavailable)
        baseline = build_mission_workflow(deps)
        selected_workflow = build_mission_workflow(deps, mcp_snapshots=(selected,), cloud_model_allowed=True)
        assert not baseline.mission_data_policy.cloud_model_allowed
        assert not baseline.mission_data_policy.external_mcp_allowed
        assert selected_workflow.mission_data_policy.cloud_model_allowed
        assert selected_workflow.mission_data_policy.external_mcp_allowed
        manager = selected_workflow.mission_mcp_manager
        assert manager.snapshots == (selected,)
        assert manager.sessions[server_id].factory is unavailable
        assert manager.sessions[server_id].connection is None
        assert selected_workflow.analysis_kwargs["approved_server_ids"] == (server_id,)
        assert len(selected_workflow.analysis_kwargs["approved_mcp"]) == 1
        assert not gateway._providers
    finally:
        asyncio.run(gateway.aclose())
        store.close()


def test_mission_host_without_dependencies_never_uses_legacy_factory(tmp_path):
    store = SqliteEventStore(tmp_path / "unconfigured.sqlite3")
    try:
        missions = MissionHost(TaskHost(unavailable, store=store))
        with pytest.raises(ForgePilotError) as caught:
            missions._workflow("unconfigured", mcp_server_ids=())
        assert caught.value.code == "mission_unavailable"
        assert caught.value.message == "Missions require the configured shared ModelGateway"
        assert not missions._services
    finally:
        store.close()


def test_mission_host_rejects_dependencies_from_another_event_store(tmp_path, git_exe, rg_exe):
    gateway = gateway_for(CountingProvider())
    config = development(rg_exe, git_exe)
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    store = SqliteEventStore(tmp_path / "host.sqlite3")
    foreign = SqliteEventStore(tmp_path / "foreign.sqlite3")
    try:
        deps = resources.mission_dependencies(foreign, mcp_connection_factory=unavailable)
        with pytest.raises(ValueError, match="host event store"):
            MissionHost(TaskHost(unavailable, store=store), dependencies=deps)
        assert not gateway._providers
    finally:
        asyncio.run(gateway.aclose())
        foreign.close()
        store.close()


def test_custom_workflow_factory_takes_precedence_over_default_dependencies(
        tmp_path, monkeypatch, git_exe, rg_exe):
    gateway = gateway_for(CountingProvider())
    config = development(rg_exe, git_exe)
    resources = ServerResources(gateway=gateway, redactor=gateway.redactor, development=lambda: config)
    store = SqliteEventStore(tmp_path / "custom.sqlite3")
    calls = []
    workflow = SimpleNamespace()
    def factory(events):
        calls.append(events)
        return workflow
    monkeypatch.setattr("forgepilot.mission_factory.build_mission_workflow", unavailable)
    try:
        deps = resources.mission_dependencies(store, mcp_connection_factory=unavailable)
        missions = MissionHost(TaskHost(unavailable, store=store), factory, dependencies=deps)
        assert missions._workflow("custom", mcp_server_ids=()) is workflow
        assert missions._workflow("custom") is workflow
        assert calls == [store]
        assert not gateway._providers
    finally:
        asyncio.run(gateway.aclose())
        store.close()
