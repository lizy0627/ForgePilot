"""Trusted frozen TaskSpec admission stays separate from the public HTTP schema."""
import asyncio
import json

import pytest

from forgepilot.agents.contracts import validate_task_spec
from forgepilot.agents.analysis_tools import AnalysisToolRegistry
from forgepilot.agents.registry import AgentRegistry
from forgepilot.errors import ForgePilotError
from forgepilot.mission_host import MissionHost
from forgepilot.orchestration.models import GraphDefinition, _plain_json

from test_analysis_contracts import task_payload
from test_mission_factory import build_dependencies, build_host
from test_workspace_manager import committed_repo


def frozen_payload():
    payload = task_payload()
    payload["repository"] = {"base_ref": "HEAD", "toolchain_profile": "cmake-ctest-v1"}
    payload["data_policy"] = {"cloud_model_allowed": False, "external_mcp_allowed": False}
    return payload


def fields(root, spec):
    return dict(repository_path=str(root), request=spec.objective, task_type=spec.task_type,
                orchestration_mode="multi_agent", base_ref="HEAD", toolchain_profile="auto",
                language="zh-CN", mcp_server_ids=[])


@pytest.mark.asyncio
async def test_locked_task_spec_is_bound_to_durable_graph_without_model_draft(
        tmp_path, git_exe, rg_exe, committed_repo):
    host, gateway, _ = build_host(tmp_path, git_exe, rg_exe)
    missions = MissionHost(host, dependencies=build_dependencies(host, gateway, git_exe, rg_exe))
    spec = validate_task_spec(frozen_payload())
    await host.start()
    try:
        made = await missions.create_locked(spec, **fields(committed_repo, spec))
        task_id = made["task_id"]
        run = host.store.missions.get_graph_run(task_id)
        graph = GraphDefinition.from_dict(json.loads(run["definition_json"]))
        assert graph.graph_id == "task-analysis-delivery-locked.v2"
        draft = next(node for node in graph.nodes if node.node_id == "draft_task_spec")
        assert draft.handler == "locked_task_spec"
        assert _plain_json(draft.settings["task_spec"]) == spec.model_dump(mode="json")
        assert missions._workflow(task_id)._stored_graph(task_id).definition_hash == graph.definition_hash
        assert "task_spec" not in missions._metadata(task_id)
        for _ in range(100):
            published = [item for item in host.store.artifacts.list_for_task(task_id)
                         if item.artifact_type == "TaskSpec"]
            if published:
                break
            await asyncio.sleep(.02)
        assert len(published) == 1 and published[0].content == spec
    finally:
        await missions.close()
        await host.close()
        await gateway.aclose()


@pytest.mark.asyncio
async def test_locked_task_spec_rejects_request_mismatch_before_workspace(
        tmp_path, git_exe, rg_exe, committed_repo):
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe)
    missions = MissionHost(host, dependencies=build_dependencies(host, gateway, git_exe, rg_exe))
    spec = validate_task_spec(frozen_payload())
    await host.start()
    try:
        with pytest.raises(ForgePilotError, match="policy_denied"):
            await missions.create_locked(spec, **(fields(committed_repo, spec) | {"request": "Different request"}))
        assert not (host.store.artifacts.root / "mission-workspaces").exists()
        assert provider.calls == 0
    finally:
        await missions.close()
        await host.close()
        await gateway.aclose()


@pytest.mark.asyncio
async def test_locked_single_agent_uses_one_identity_and_stage_scoped_tools(
        tmp_path, git_exe, rg_exe, committed_repo):
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe)
    missions = MissionHost(host, dependencies=build_dependencies(host, gateway, git_exe, rg_exe))
    spec = validate_task_spec(frozen_payload())
    await host.start()
    try:
        made = await missions.create_locked(spec, **(fields(committed_repo, spec) | {
            "orchestration_mode": "single_agent"}))
        task_id = made["task_id"]
        graph = GraphDefinition.from_dict(json.loads(
            host.store.missions.get_graph_run(task_id)["definition_json"]))
        assert graph.graph_id == "task-analysis-delivery-single-locked.v2"
        model_nodes = [node for node in graph.nodes if node.agent_id]
        assert model_nodes and {node.agent_id for node in model_nodes} == {"generalist.v1"}
        assert {node.node_id for node in model_nodes} >= {
            "generalist_analysis", "generalist_plan", "implementer", "reviewer_0", "repair_1"}
        assert not any(node.node_id == "analyst_fanout" for node in graph.nodes)
        definition = AgentRegistry(include_generalist=True).get("generalist.v1")
        assert gateway.router.validate_agent(definition).primary.profile_id == "writer"
        assert missions._workflow(task_id)._stored_graph(task_id).definition_hash == graph.definition_hash
        with pytest.raises(ForgePilotError, match="policy_denied"):
            AnalysisToolRegistry(object(), role="generalist").register(
                type("WriteTool", (), {"name": "repository_apply_patch", "read_only": False})())
        assert host.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        await missions.close()
        await host.close()
        await gateway.aclose()


@pytest.mark.asyncio
async def test_locked_editable_test_scope_requires_manifest_binding_and_separate_grant(
        tmp_path, git_exe, rg_exe, committed_repo):
    host, gateway, provider = build_host(tmp_path, git_exe, rg_exe)
    missions = MissionHost(host, dependencies=build_dependencies(host, gateway, git_exe, rg_exe))
    payload = frozen_payload()
    payload["test_policy"] = {"existing_tests_read_only": False, "allow_new_tests": True,
                              "modifiable_existing_test_patterns": ["test/existing.cpp"]}
    payload["allowed_scope"] = {"path_patterns": ["source.txt", "test/existing.cpp"],
                                "max_changed_files": 2}
    spec = validate_task_spec(payload)
    await host.start()
    try:
        with pytest.raises(ForgePilotError, match="policy_denied"):
            await missions.create_locked(spec, allow_locked_test_scope=True,
                                         **fields(committed_repo, spec))
        assert provider.calls == 0
        made = await missions.create_locked(spec, allow_locked_test_scope=True,
            locked_authorization_ref="a" * 64, **fields(committed_repo, spec))
        task_id = made["task_id"]
        graph = GraphDefinition.from_dict(json.loads(
            host.store.missions.get_graph_run(task_id)["definition_json"]))
        draft = next(node for node in graph.nodes if node.node_id == "draft_task_spec")
        assert draft.settings["allow_locked_test_scope"] is True
        assert missions._workflow(task_id)._stored_graph(task_id).definition_hash == graph.definition_hash
        assert missions._metadata(task_id)["locked_authorization_ref"] == "a" * 64
        assert host.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        await missions.close()
        await host.close()
        await gateway.aclose()
