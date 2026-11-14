"""The production composition shares the gateway and preserves legacy startup."""
import asyncio
import json
import shutil

import pytest

from conftest import reply
from forgepilot.api import create_app
from forgepilot.errors import ForgePilotError
from forgepilot.mission_factory import MissionDependencies, build_mission_workflow
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.presets import BuildConfig
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.task_service import TaskService


MISSION_ROUTES = ("orchestration", "analysis", "implementation", "review", "repair")


def configuration(*, omit=None):
    return ModelConfiguration(profiles=tuple(ModelProfile(
        profile_id=name, base_url="http://127.0.0.1:1/v1", model=name,
        family=name, api_key_ref="env:MISSION_FACTORY_TEST_KEY") for name in ("writer", "reviewer")),
        routes=tuple(ModelRoute(route_id=name, primary="reviewer" if name == "review" else "writer")
                     for name in ("task", "chat", "summary", *MISSION_ROUTES) if name != omit))


class Provider:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def chat(self, messages, tools=None):
        self.calls += 1
        return reply("offline response")

    async def aclose(self):
        self.closed = True


def build_host(tmp_path, git_exe, rg_exe, *, omit=None, build_config=None):
    provider = Provider()
    gateway = ModelGateway(configuration(omit=omit), secret_resolver=lambda _: "offline-key",
                           provider_factory=lambda *_: provider)
    store = SqliteEventStore(tmp_path / "factory.sqlite3", artifact_root=tmp_path / "artifacts")
    def factory(trace):
        return TaskService(gateway.for_route("task"), trace=trace, rg_executable=rg_exe,
                           git_executable=git_exe, build_config=build_config)
    return TaskHost(factory, store=store), gateway, provider


def build_dependencies(host, gateway, git_exe, rg_exe, *, build_config=None):
    return MissionDependencies(gateway=gateway, events=host.store,
        rg_executable=rg_exe, git_executable=git_exe, build_config=build_config,
        redactor=gateway.redactor, mcp_connection_factory=host.mcp_service.connection_factory)


@pytest.mark.asyncio
async def test_factory_composes_shared_gateway_trusted_validator_and_data_policy(tmp_path, git_exe, rg_exe):
    config = BuildConfig(cmake=shutil.which("cmake"), ctest=shutil.which("ctest"),
        generator="Visual Studio 17 2022", architecture="x64", configuration="Release",
        configure_timeout=101, build_timeout=202, test_timeout=303)
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe, build_config=config)
    oracle = tmp_path / "trusted oracle"
    oracle.mkdir()
    try:
        dependencies = build_dependencies(host, gateway, git_exe, rg_exe, build_config=config)
        workflow = build_mission_workflow(dependencies, oracle_source=oracle, cloud_model_allowed=True)
        assert workflow.gateway is gateway
        assert workflow.events is host.store
        assert workflow.workspace.artifacts is host.store.artifacts
        assert workflow.validator.workspace is workflow.workspace
        assert workflow.validator.oracle.source_dir == oracle
        assert workflow.validator.profile.cmake == config.cmake
        assert workflow.validator.profile.ctest == config.ctest
        assert workflow.validator.generator == config.generator
        assert workflow.validator.architecture == config.architecture
        assert workflow.validator.configuration == "Release"
        assert workflow.validator.timeout == 303
        assert workflow.mission_data_policy.cloud_model_allowed is True
        assert workflow.mission_data_policy.external_mcp_allowed is False
        assert not gateway._providers and provider.calls == 0
        assert build_mission_workflow(dependencies).mission_data_policy.cloud_model_allowed is False
        # The compatibility handle remains non-owning: composition cannot close v1's gateway.
        legacy_handle = host.factory(None).llm
        await legacy_handle.aclose()
        assert (await legacy_handle.chat([{"role": "user", "content": "hello"}])).message.content == "offline response"
        assert provider.calls == 1 and not provider.closed
    finally:
        await gateway.aclose()
        host.store.close()
    assert provider.closed


@pytest.mark.parametrize("missing_route", MISSION_ROUTES)
def test_missing_graph_route_fails_before_workspace_allocation(tmp_path, git_exe, rg_exe, missing_route):
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe, omit=missing_route)
    try:
        with pytest.raises(ForgePilotError) as caught:
            build_mission_workflow(build_dependencies(host, gateway, git_exe, rg_exe))
        assert caught.value.code == "llm_config"
        assert "orchestration, analysis, implementation, review and repair" in caught.value.message
        assert not (host.store.artifacts.root / "mission-workspaces").exists()
        assert not gateway._providers and provider.calls == 0
        assert gateway.for_route("task").model == "writer"
    finally:
        asyncio.run(gateway.aclose())
        host.store.close()


@pytest.mark.parametrize("field", ["configure_preset", "build_preset", "test_preset"])
def test_v2_rejects_unsupported_presets_before_workspace_allocation(tmp_path, git_exe, rg_exe, field):
    config = BuildConfig(cmake=shutil.which("cmake"), ctest=shutil.which("ctest"), **{field: "trusted-preset"})
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe, build_config=config)
    try:
        with pytest.raises(ForgePilotError) as caught:
            build_mission_workflow(build_dependencies(host, gateway, git_exe, rg_exe, build_config=config))
        assert caught.value.code == "build_config"
        assert "does not support CMake presets" in caught.value.message
        assert not (host.store.artifacts.root / "mission-workspaces").exists()
        assert not gateway._providers and provider.calls == 0
        # The existing v1 service still receives the exact original preset config.
        assert host.factory(None).build_config is config
    finally:
        asyncio.run(gateway.aclose())
        host.store.close()


def test_openapi_generation_never_invokes_mission_or_legacy_factory():
    def unavailable(*args):
        pytest.fail("Schema construction must not instantiate runtime dependencies")
    app = create_app(unavailable, mission_factory=unavailable)
    try:
        schema = app.openapi()
        assert "/api/v1/tasks" in schema["paths"]
        assert "/api/v2/missions" in schema["paths"]
    finally:
        app.state.task_host.store.close()


def test_server_wires_trusted_mission_options_without_provider_io(tmp_path, monkeypatch, git_exe, rg_exe):
    from forgepilot import server
    monkeypatch.setenv("MISSION_FACTORY_TEST_KEY", "offline-key")
    model_config = tmp_path / "models.json"
    model_config.write_text(json.dumps(configuration().model_dump(mode="json")), encoding="utf-8")
    oracle = tmp_path / "oracle"
    oracle.mkdir()
    seen = []
    def run(app, **options):
        workflow = app.state.mission_host.factory(app.state.task_host.store)
        gateway = app.state.task_host.factory(None).llm.gateway
        assert workflow.gateway is gateway
        assert workflow.mission_data_policy.cloud_model_allowed is True
        assert workflow.validator.oracle.source_dir == oracle
        assert not gateway._providers
        seen.append(workflow)
        asyncio.run(gateway.aclose())
    monkeypatch.setattr(server.uvicorn, "run", run)
    result = server.main(["--model-config", str(model_config), "--database", str(tmp_path / "server.sqlite3"),
        "--git", git_exe, "--rg", rg_exe, "--mission-oracle", str(oracle), "--mission-allow-cloud-model"])
    assert result == 0 and len(seen) == 1
