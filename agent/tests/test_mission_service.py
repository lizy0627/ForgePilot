"""Mission application boundaries use real Graph transactions and managed Git."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from forgepilot.agents.implementation import ImplementationService
from forgepilot.artifacts import create_artifact
from forgepilot.errors import ForgePilotError
from forgepilot.mission_host import MissionHost
from forgepilot.orchestration.analysis_review import AnalysisReviewStore
from forgepilot.orchestration.models import FailureKind, GraphDefinition, NodeDefinition, NodeResult, NodeStatus, NodeType
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.orchestration.runner import GraphRunner
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.workspace_manager import WorkspaceManager
from test_artifact_protocol import plan, task_spec
from test_workspace_manager import committed_repo


class ControlledWorkflow:
    """Offline deterministic graph with the production approval/checkpoint path."""
    _run_worker = ImplementationService._run_worker
    _check_review = ImplementationService._check_review
    review = ImplementationService.review
    run = ImplementationService.run
    cancel = ImplementationService.cancel

    def __init__(self, events, workspace, *, pause=False):
        self.events, self.workspace = events, workspace
        self.validator = SimpleNamespace(profile=SimpleNamespace(profile_id="cmake-ctest-v1"))
        self.reviews = AnalysisReviewStore(events)
        self.pause = pause
        self.runner = GraphRunner(events, HandlerRegistry({"draft": self.draft, "gate": self.gate,
            "finish": self.finish}), workspace_backend=workspace)
        self._active, self._cancel_requested = set(), set()
        self._analysis_services, self._loop = {}, None

    def _analysis_graph(self, task_id, request):
        return self._stored_graph(task_id)

    def _stored_graph(self, task_id):
        return GraphDefinition("mission-test", 1, (
            NodeDefinition("draft", NodeType.DETERMINISTIC, "draft"),
            NodeDefinition("execution_approval", NodeType.HUMAN_GATE, "gate", dependencies=("draft",)),
            NodeDefinition("finish", NodeType.DETERMINISTIC, "finish", dependencies=("execution_approval",)),
        ))

    def draft(self, context):
        spec = task_spec()
        spec = spec.model_copy(update={"allowed_scope": spec.allowed_scope.model_copy(update={"path_patterns": ("source.txt",)})})
        envelope = create_artifact(task_id=context.task_id, producer="orchestrator.v1", producer_node="draft", content=spec)
        self.events.artifacts.put(envelope)
        content = plan().model_copy(update={"task_spec_artifact_id": envelope.artifact_id,
            "steps": (plan().steps[0].model_copy(update={"expected_files": ("source.txt",),
                "acceptance_criteria_ids": ("AC-1", "AC-2"), "adds_tests": False}),)})
        execution = create_artifact(task_id=context.task_id, producer="orchestrator.v1", producer_node="draft",
            parent_artifact_ids=(envelope.artifact_id,), content=content)
        self.events.artifacts.put(execution)
        return NodeResult(NodeStatus.SUCCEEDED, output_artifact_ids=(envelope.artifact_id, execution.artifact_id))

    def gate(self, context):
        spec, execution = (self.events.artifacts.get(item) for item in context.input_artifact_ids)
        self.reviews.bind(context.task_id, spec, execution)
        return NodeResult(NodeStatus.AWAITING_HUMAN, output_artifact_ids=context.input_artifact_ids)

    def finish(self, context):
        if self.pause:
            self.pause = False
            return NodeResult(NodeStatus.RETRYABLE, failure_kind=FailureKind.BUILD_CONFIGURATION,
                              error_summary="offline infrastructure pause")
        return NodeResult(NodeStatus.SUCCEEDED)


async def setup(tmp_path, git_exe, max_tasks=32, *, pause=False):
    workspace = WorkspaceManager(tmp_path / "workspace", git_executable=git_exe)
    events = SqliteEventStore(tmp_path / "mission.sqlite3", artifact_root=tmp_path / "artifacts", workspace_backend=workspace)
    tasks = TaskHost(lambda trace: None, store=events, max_tasks=max_tasks)
    missions = MissionHost(tasks, lambda events: ControlledWorkflow(events, workspace, pause=pause))
    await tasks.start()
    return tasks, missions


async def create(missions, root, **kwargs):
    fields = dict(repository_path=str(root), request="Fix the worker", task_type="bug_fix",
        orchestration_mode="multi_agent", base_ref="HEAD", toolchain_profile="auto")
    return await missions.create(**(fields | kwargs))


async def settled(missions, task_id):
    for _ in range(300):
        mission = missions.get(task_id)
        if mission["status"] in {"awaiting_human", "failed", "cancelled", "succeeded", "retryable"}:
            await asyncio.sleep(.02)
            return missions.get(task_id)
        await asyncio.sleep(.01)
    pytest.fail("Mission did not settle")


def decision(mission, **updates):
    review = mission["review"]
    return dict(expected_revision=mission["current_revision"], approval_type="execution",
        task_spec_hash=review["task_spec_hash"], plan_hash=review["plan_hash"],
        scope_hash=review["scope_hash"], decision="approved") | updates


@pytest.mark.asyncio
async def test_mission_creation_artifacts_atomic_approval_and_completion(tmp_path, git_exe, committed_repo):
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        made = await create(missions, committed_repo)
        tid = made["task_id"]
        mission = await settled(missions, tid)
        assert mission["status"] == "awaiting_human", missions.graph(tid)
        assert len(missions.artifacts(tid)["items"]) == 2
        item = missions.artifacts(tid)["items"][0]
        assert missions.artifact(tid, item["artifact_id"]) == item
        assert missions.diff(tid)["patch"] == ""
        assert missions.validation(tid)["latest"] is None
        await missions.approve(tid, **decision(mission))
        assert (await settled(missions, tid))["status"] == "succeeded"
        rows = tasks.store._db.execute("SELECT * FROM approvals WHERE task_id=?", (tid,)).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "approved"
        assert tasks.store.missions.is_approval_valid(rows[0]["approval_id"])
        with pytest.raises(ForgePilotError, match="mission_revision_conflict"):
            await missions.approve(tid, **decision(mission))
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["task_spec_hash", "plan_hash", "scope_hash"])
async def test_mission_hash_mismatch_has_no_grant_or_revision_change(tmp_path, git_exe, committed_repo, field):
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        made = await create(missions, committed_repo)
        tid = made["task_id"]
        mission = await settled(missions, tid)
        with pytest.raises(ForgePilotError, match="mission_approval_conflict"):
            await missions.approve(tid, **decision(mission, **{field: "0" * 64}))
        assert missions.get(tid)["current_revision"] == mission["current_revision"]
        assert tasks.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        denial = tasks.store._db.execute("SELECT event_type,attributes_json FROM security_events WHERE task_id=?",
                                        (tid,)).fetchone()
        assert denial["event_type"] == "approval_denied"
        assert "mission_approval_conflict" in denial["attributes_json"]
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_rejection_is_atomic_terminal_and_resume_never_restarts_cancelled(tmp_path, git_exe, committed_repo):
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        tid = (await create(missions, committed_repo))["task_id"]
        mission = await settled(missions, tid)
        rejected = await missions.approve(tid, **decision(mission, decision="rejected"))
        assert rejected["status"] == "cancelled"
        assert tasks.store._db.execute("SELECT status FROM approvals").fetchone()[0] == "rejected"
        resumed = await missions.resume(tid, expected_revision=rejected["current_revision"])
        assert resumed["recovery"]["blocked_reason"] == "terminal_graph"
        assert missions.get(tid)["status"] == "cancelled"
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_waiting_approval_counts_against_shared_capacity(tmp_path, git_exe, committed_repo):
    tasks, missions = await setup(tmp_path, git_exe, max_tasks=1)
    try:
        first, second = await asyncio.gather(create(missions, committed_repo),
            create(missions, committed_repo), return_exceptions=True)
        assert isinstance(first, dict)
        assert isinstance(second, ForgePilotError) and second.code == "task_capacity"
        tid = first["task_id"]
        mission = await settled(missions, tid)
        assert tasks.capacity.active_count() == 1
        with pytest.raises(ForgePilotError, match="task_capacity"):
            await create(missions, committed_repo)
        with pytest.raises(ForgePilotError, match="task_capacity"):
            await tasks.create(str(committed_repo), "another")
        await missions.cancel(tid, expected_revision=mission["current_revision"])
        assert tasks.capacity.active_count() == 0
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["retry", "resume"])
async def test_retry_and_confirmed_recovery_continue_paused_missions(tmp_path, git_exe, committed_repo, control):
    tasks, missions = await setup(tmp_path, git_exe, pause=True)
    try:
        tid = (await create(missions, committed_repo))["task_id"]
        mission = await settled(missions, tid)
        await missions.approve(tid, **decision(mission))
        paused = await settled(missions, tid)
        assert paused["status"] == "retryable", missions.graph(tid)
        if control == "retry":
            await missions.retry(tid, expected_revision=paused["current_revision"], node_id="finish")
        else:
            preview = await missions.resume(tid, expected_revision=paused["current_revision"])
            assert preview["recovery"]["blocked_reason"] is None, preview
            assert preview["mission"]["recovery_required"]
            await missions.resume(tid, expected_revision=preview["mission"]["current_revision"],
                                  recovery_token=preview["recovery"]["token"])
        assert (await settled(missions, tid))["status"] == "succeeded"
        finish = next(n for n in missions.graph(tid)["nodes"] if n["node_id"] == "finish")
        assert finish["attempt"] == 2
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_failed_gate_publication_rolls_back_approval(tmp_path, git_exe, committed_repo):
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        tid = (await create(missions, committed_repo))["task_id"]
        mission = await settled(missions, tid)
        tasks.store._db.execute("""CREATE TEMP TRIGGER deny_gate BEFORE UPDATE ON graph_node_states
            WHEN NEW.node_id='execution_approval' AND NEW.status='succeeded'
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with pytest.raises(ForgePilotError, match="graph_storage"):
            await missions.approve(tid, **decision(mission))
        assert tasks.store._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        assert missions.get(tid)["current_revision"] == mission["current_revision"]
    finally:
        await missions.close()
        await tasks.close()


@pytest.mark.asyncio
async def test_delivery_recovers_committed_outcome_after_event_failure(tmp_path, git_exe, committed_repo, monkeypatch):
    import sqlite3
    import forgepilot.mission_host as module
    tasks, missions = await setup(tmp_path, git_exe)
    try:
        tid = (await create(missions, committed_repo))["task_id"]
        mission = await settled(missions, tid)
        cancelled = await missions.cancel(tid, expected_revision=mission["current_revision"])
        preview = await missions.delivery(tid, expected_revision=cancelled["current_revision"], mode="abandon")
        original = module.append_mission_event
        def fail(store, task_id, event_type, data, **kwargs):
            if event_type == "workspace.delivered":
                raise sqlite3.OperationalError("injected outcome publication failure")
            return original(store, task_id, event_type, data, **kwargs)
        monkeypatch.setattr(module, "append_mission_event", fail)
        with pytest.raises(sqlite3.OperationalError):
            await missions.delivery(tid, expected_revision=preview["mission"]["current_revision"],
                mode="abandon", token=preview["delivery"]["token"])
        monkeypatch.setattr(module, "append_mission_event", original)
        mission = missions.get(tid)
        rows = tasks.store._db.execute("SELECT payload_json FROM graph_events WHERE task_id=? AND event_type='workspace.delivered'", (tid,)).fetchall()
        assert len(rows) == 1 and '"recovered":true' in rows[0][0]
        assert mission["status"] == "cancelled"
        missions.get(tid)
        assert tasks.store._db.execute("SELECT count(*) FROM graph_events WHERE task_id=? AND event_type='workspace.delivered'", (tid,)).fetchone()[0] == 1
    finally:
        await missions.close()
        await tasks.close()
