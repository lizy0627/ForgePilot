"""Per-Mission analysis tools on loopback HTTP admission and cold recovery."""
from pathlib import Path

import httpx
import pytest

from forgepilot.agents.analysis import AnalysisService
from forgepilot.agents.analysis_tools import build_analysis_registry
from mission_tools_support import MissionToolsCase
from test_api_network import serving_app
from test_mission_tools import settled


def analyst_tools(analysis):
    registry = build_analysis_registry(analysis.repository, retrieval=analysis.retrieval,
        intelligence=analysis.intelligence, approved_mcp=analysis.approved_mcp,
        approved_server_ids=analysis.approved_server_ids,
        external_mcp_allowed=bool(analysis.approved_server_ids))
    return frozenset(spec.name for spec in registry.get_specs())


@pytest.mark.asyncio
async def test_first_graph_and_cold_resume_use_the_same_task_tools(tmp_path, git_exe, rg_exe, monkeypatch):
    graph = AnalysisService.graph
    first_graph = {}

    def inspect_graph(service, *args, **kwargs):
        root = service.repository.root
        if root not in first_graph:
            first_graph[root] = (service, analyst_tools(service))
        return graph(service, *args, **kwargs)

    monkeypatch.setattr(AnalysisService, "graph", inspect_graph)
    tasks = {}

    def inspect_cached(case, task_id, use_rag):
        workflow = case.app.state.mission_host._services[task_id]
        root = Path(workflow.workspace.record(task_id)["worktree_path"])
        initial, names = first_graph[root]
        analysis = workflow._analysis_for(task_id)
        assert analysis is initial
        assert workflow._analysis_for(task_id) is analysis
        assert analysis.repository.root == analysis.intelligence.repository.root == root
        assert root != case.source
        assert analyst_tools(analysis) == names
        assert "repository_get_symbol" in names
        assert ("rag_retrieve" in names) == use_rag
        assert any(name.startswith("mcp_") for name in names) == use_rag
        if use_rag:
            assert analysis.retrieval.repository.root == root
            assert analysis.approved_server_ids == (sid,)
        return names

    case = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe)
    try:
        sid = await case.seed_mcp()
        with serving_app(case.app) as url:
            async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=45) as client:
                for use_rag in (True, False):
                    created = await client.post("/api/v2/missions", json={
                        "repository_path": str(case.source), "request": "Inspect the current value function",
                        "use_rag": use_rag, "mcp_server_ids": [sid] if use_rag else []})
                    assert created.status_code == 202, created.text
                    task_id = created.json()["task_id"]
                    result = await settled(client, f"/api/v2/missions/{task_id}")
                    assert result["status"] == "awaiting_human", result
                    frozen = await client.get(f"/api/v2/missions/{task_id}/graph")
                    tasks[task_id] = (use_rag, frozen.json()["definition_hash"])
                expected = {task_id: inspect_cached(case, task_id, use_rag)
                            for task_id, (use_rag, _) in tasks.items()}
                assert len(first_graph) == 2
    finally:
        case.store.close()

    first_graph.clear()
    reopened = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe)
    try:
        with serving_app(reopened.app) as url:
            async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=45) as client:
                for task_id, (use_rag, frozen_hash) in tasks.items():
                    path = f"/api/v2/missions/{task_id}"
                    current = (await client.get(path)).json()
                    resumed = await client.post(path + "/resume", json={
                        "expected_revision": current["current_revision"]})
                    assert resumed.status_code == 202, resumed.text
                    assert resumed.json()["recovery"]["evidence"]["definition"] == frozen_hash
                    assert inspect_cached(reopened, task_id, use_rag) == expected[task_id]
                assert not reopened.provider.seen
    finally:
        reopened.store.close()
