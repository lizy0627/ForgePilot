"""Previous frozen delivery graphs remain inspectable but cannot resume execution."""
from dataclasses import replace
from types import MethodType

import pytest

from forgepilot.agents.delivery import DeliveryWorkflow, LEGACY_DELIVERY_GRAPH_IDS
from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from test_artifact_protocol import all_contents
from test_mission_service import ControlledWorkflow, create, decision, settled, setup
from test_workspace_manager import committed_repo


def freeze_previous_graph(monkeypatch, graph_id):
    original = ControlledWorkflow._stored_graph
    monkeypatch.setattr(ControlledWorkflow, "_stored_graph", lambda self, task_id:
        replace(original(self, task_id), graph_id=graph_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_id", sorted(LEGACY_DELIVERY_GRAPH_IDS))
async def test_unfinished_previous_graph_requires_new_mission_and_approval(
        tmp_path, git_exe, committed_repo, monkeypatch, graph_id):
    freeze_previous_graph(monkeypatch, graph_id)
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        task_id = (await create(missions, committed_repo))["task_id"]
        waiting = await settled(missions, task_id)
        assert waiting["status"] == "awaiting_human"
        workflow = missions._services[task_id]
        # Model an application upgrade without rewriting the persisted definition.
        workflow._stored_graph = MethodType(DeliveryWorkflow._stored_graph, workflow)
        frozen = missions.graph(task_id)
        assert all(node["pending_action"] is None for node in frozen["nodes"])
        for control in (
            lambda: missions.approve(task_id, **decision(waiting)),
            lambda: missions.approve(task_id, **decision(waiting, decision="rejected")),
            lambda: missions.retry(task_id, expected_revision=waiting["current_revision"]),
            lambda: missions.resume(task_id, expected_revision=waiting["current_revision"]),
        ):
            with pytest.raises(ForgePilotError) as caught:
                await control()
            assert caught.value.code == "graph_definition"
            assert "recreate the mission and approve" in caught.value.message
        assert missions.get(task_id) == waiting
        assert missions.graph(task_id) == frozen
        assert tasks.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_completed_previous_graph_keeps_history_diff_and_reports_readable(
        tmp_path, git_exe, committed_repo, monkeypatch):
    graph_id = "task-analysis-delivery.v1"
    freeze_previous_graph(monkeypatch, graph_id)
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        task_id = (await create(missions, committed_repo))["task_id"]
        waiting = await settled(missions, task_id)
        await missions.approve(task_id, **decision(waiting))
        completed = await settled(missions, task_id)
        assert completed["status"] == "succeeded"
        changes, report = all_contents()[3:5]
        changes = changes.model_copy(update={
            "execution_plan_artifact_id": completed["review"]["plan_artifact_id"],
            "files": (changes.files[0].model_copy(update={"path": "source.txt"}),),
        })
        change_artifact = create_artifact(task_id=task_id, producer="workspace.v1",
            producer_node="finish", content=changes,
            parent_artifact_ids=(completed["review"]["plan_artifact_id"],))
        tasks.store.artifacts.put(change_artifact)
        report_artifact = create_artifact(task_id=task_id, producer="validator.v1",
            producer_node="finish", content=report,
            parent_artifact_ids=(change_artifact.artifact_id,))
        tasks.store.artifacts.put(report_artifact)
        historical = missions.get(task_id)
        monkeypatch.setattr(missions, "_workflow", lambda *_args, **_kwargs:
            pytest.fail("Historical inspection must not rebuild an execution workflow"))

        assert missions.get(task_id) == historical
        graph = missions.graph(task_id)
        assert graph["graph_id"] == graph_id and graph["graph_version"] == 1
        assert graph["status"] == "succeeded"
        artifacts = missions.artifacts(task_id)["items"]
        assert len(artifacts) == 4
        assert missions.artifact(task_id, report_artifact.artifact_id) == artifacts[-1]
        diff = missions.diff(task_id)
        assert diff["patch"] == changes.files[0].unified_diff
        assert diff["change_sets"][0]["artifact_id"] == change_artifact.artifact_id
        validation = missions.validation(task_id)
        assert validation["latest"]["artifact_id"] == report_artifact.artifact_id
        assert validation["latest"]["content"]["summary"] == report.summary
        assert validation["latest"]["content_sha256"] == report_artifact.content_sha256
    finally:
        await missions.close()
        await tasks.close()
