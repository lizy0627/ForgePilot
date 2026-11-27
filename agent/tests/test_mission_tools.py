"""Real production Mission admission, tool calls, diagnostics and frozen restoration."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.mission_tools import MissionToolSelection, prepare_mission_tools
from mission_tools_support import MissionToolsCase, SOURCE


async def settled(client, path):
    async with asyncio.timeout(45):
        while True:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            if response.json()["status"] in {"awaiting_human", "failed", "retryable", "cancelled"}:
                await asyncio.sleep(0.03)
                return (await client.get(path)).json()
            await asyncio.sleep(0.02)


def test_shared_workflow_freezes_analysis_dependencies_per_task(tmp_path, git_exe, rg_exe):
    case = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe)
    try:
        workflow = case.app.state.mission_host._new_workflow(())
        def prepare(use_rag):
            task_id = str(uuid4())
            workflow.workspace.create(case.source, task_id)
            return prepare_mission_tools(workflow, task_id,
                MissionToolSelection(use_rag, workflow.mission_data_policy))

        with_rag = prepare(True)
        workflow.configure_mission_tools(with_rag)
        first_graph = workflow.graph()
        without_rag = prepare(False)
        workflow.configure_mission_tools(without_rag)

        for tools in (without_rag, with_rag):
            analysis = workflow._analysis_for(tools.task_id)
            assert analysis.repository is tools.repository
            assert analysis.intelligence is tools.intelligence
            assert analysis.retrieval is tools.retrieval
            workflow.configure_mission_tools(tools)
            assert workflow._analysis_for(tools.task_id) is analysis
        assert with_rag.repository.root != without_rag.repository.root
        assert with_rag.retrieval is not None and without_rag.retrieval is None
        assert workflow.graph(with_rag.task_id) == first_graph
        assert workflow.graph(without_rag.task_id).definition_hash != first_graph.definition_hash
        with pytest.raises(ForgePilotError, match="graph_definition"):
            workflow.graph()
        with pytest.raises(ForgePilotError, match="graph_definition"):
            workflow.configure_mission_tools(prepare_mission_tools(workflow, with_rag.task_id, without_rag.selection))
        assert workflow._analysis_for(with_rag.task_id).retrieval is with_rag.retrieval
    finally:
        case.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "embedding", "vector", "disabled"])
async def test_production_http_model_tools_fallback_and_frozen_restore(tmp_path, git_exe, rg_exe, failure):
    case = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe,
        embedding_failure=failure == "embedding", vector_failure=failure == "vector",
        rag_enabled=failure != "disabled")
    try:
        async with case.app.router.lifespan_context(case.app):
            sid = await case.seed_mcp()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v2/missions", json={"repository_path": str(case.source),
                    "request": "Inspect selected local tools and current source", "task_type": "bug_fix",
                    "mcp_server_ids": [sid], "use_rag": True})
                assert created.status_code == 202, created.text
                path = "/api/v2/missions/" + created.json()["task_id"]
                result = await settled(client, path)
                assert result["status"] == "awaiting_human", (await client.get(path + "/graph")).text
                assert result["use_rag"] is True and result["mcp_server_ids"] == [sid]
                context = result["tool_context"]
                assert context["rag"]["requested"] and context["rag"]["used"]
                assert context["mcp"]["used"] and context["mcp"]["server_ids"] == [sid]
                assert context["clangd"]["fallback_reasons"]
                expected = {"embedding": "embedding_failed", "vector": "qdrant_failed", "disabled": "rag_disabled"}
                if failure is not None:
                    assert expected[failure] in context["rag"]["fallback_reasons"]
                observations = case.evidence()
                analyst_tools = [set(names) for names in observations["model_tools"] if any(name.startswith("mcp_") for name in names)]
                assert analyst_tools and all({"rag_retrieve", "repository_get_symbol"} <= names for names in analyst_tools)
                schemas = {item["name"]: item["input_schema"] for batch in observations["model_schemas"] for item in batch}
                assert "query" in schemas["rag_retrieve"]["properties"]
                assert {"path", "line", "column"} <= set(schemas["repository_get_symbol"]["required"])
                assert all(schema["required"] == ["topic"] for name, schema in schemas.items() if name.startswith("mcp_"))
                responses = [item for batch in observations["model_tool_responses"] for item in batch]
                assert any(item.get("content_is_untrusted") and "evidence_location" in item for item in responses)
                assert any(item.get("results") for item in responses)
                assert any(item.get("semantic") is False and item.get("fallback_reason") for item in responses)
                assert (case.source / "src/value.cpp").read_text(encoding="utf-8") == SOURCE
                graph = (await client.get(path + "/graph")).json()
                assert graph["graph_id"] == "task-analysis-delivery.v4"
                task_id = result["task_id"]
                workflow = case.app.state.mission_host._services[task_id]
                assert workflow.validator._monitors == {}
                prepared = workflow.mission_tools_for(task_id)
                analysis = workflow._analysis_for(task_id)
                definition = workflow._stored_graph(task_id)
                assert workflow._analysis_for(task_id) is analysis
                assert prepared.repository is analysis.repository
                assert prepared.intelligence is analysis.intelligence
                assert prepared.retrieval is analysis.retrieval
                writer_repository = workflow._repository(task_id)
                writer_retrieval = prepared.retrieval_factory(writer_repository)
                assert writer_retrieval.repository is writer_repository
                assert writer_retrieval.data_policy == prepared.selection.data_policy
                # Reconstruct from persisted selection, without model/tool calls.
                model_count = len(case.provider.seen)
                case.app.state.mission_host._services.clear()
                restored = case.app.state.mission_host._workflow(task_id)
                assert restored._stored_graph(task_id) == definition
                assert restored.mission_tools_for(task_id).selection == prepared.selection
                assert len(case.provider.seen) == model_count
                reread = (await client.get(path)).json()
                assert reread["tool_context"] == context
                if failure is not None:
                    return
                service = case.app.state.task_host.mcp_service
                saved = service.get(sid)
                candidate = {key: saved[key] for key in (
                    "name", "transport", "modes", "selected_tools", "effect", "enabled", "connection")}
                candidate["name"] = "Changed after task admission"
                await service.save(sid, candidate, base_revision=saved["revision"],
                                   catalog_hash=saved["catalog_hash"])
    finally:
        case.store.close()

    reopened = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe)
    try:
        async with reopened.app.router.lifespan_context(reopened.app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened.app), base_url="http://127.0.0.1") as client:
            current = (await client.get(path)).json()
            assert current["status"] == "awaiting_human"
            revision = current["current_revision"]
            blocked = await client.post(path + "/resume", json={"expected_revision": revision})
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["error"]["code"] == "mcp_catalog_changed"
            stale = await client.post(path + "/cancel", json={"expected_revision": revision - 1})
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "mission_revision_conflict"
            assert (await client.get("/api/v2/runtime/status")).json()["active_count"] == 1
            cancelled = await client.post(path + "/cancel", json={"expected_revision": revision})
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
            assert (await client.get("/api/v2/runtime/status")).json()["active_count"] == 0
            events = await client.get(path + "/events", headers={"Last-Event-ID": current["last_event_id"]})
            assert events.text.count("event: task.cancelled\n") == 1
            preview = await client.post(path + "/delivery", json={
                "mode": "abandon", "expected_revision": cancelled.json()["current_revision"]})
            assert preview.status_code == 200, preview.text
            abandoned = await client.post(path + "/delivery", json={
                "mode": "abandon", "expected_revision": preview.json()["mission"]["current_revision"],
                "token": preview.json()["delivery"]["token"]})
            assert abandoned.status_code == 200, abandoned.text
            delivery = abandoned.json()["delivery"]
            assert delivery["status"] == "abandoned" and not Path(delivery["worktree_path"]).exists()
            assert (reopened.source / "src/value.cpp").read_text(encoding="utf-8") == SOURCE
            assert not reopened.provider.seen
    finally:
        reopened.store.close()


@pytest.mark.asyncio
async def test_unselected_tools_remain_absent_after_rag_configuration_changes(tmp_path, git_exe, rg_exe):
    case = MissionToolsCase(tmp_path, git_exe=git_exe, rg_exe=rg_exe)
    try:
        async with case.app.router.lifespan_context(case.app):
            await case.seed_mcp()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v2/missions", json={"repository_path": str(case.source),
                    "request": "Inspect only current repository source", "use_rag": False})
                assert created.status_code == 202, created.text
                path = "/api/v2/missions/" + created.json()["task_id"]
                result = await settled(client, path)
                assert result["status"] == "awaiting_human", (await client.get(path + "/graph")).text
                assert not result["use_rag"] and result["mcp_server_ids"] == []
                assert not result["tool_context"]["rag"]["requested"]
                assert all("rag_retrieve" not in entry["names"] and not any(
                    name.startswith("mcp_") for name in entry["names"]) for entry in case.provider.seen)
                assert not case.embeddings.calls and not case.vectors.calls
                before = json.loads(json.dumps(result["tool_context"]))
                case.rag.config = case.rag.config.model_copy(update={"enabled": False})
                case.app.state.mission_host._services.clear()
                restored = case.app.state.mission_host._workflow(result["task_id"])
                prepared = restored.mission_tools_for(result["task_id"])
                assert prepared.selection.use_rag is False and prepared.selection.mcp_servers == ()
                assert prepared.retrieval_factory is None
                assert (await client.get(path)).json()["tool_context"] == before
    finally:
        case.store.close()
