"""Independent adversarial checks of approval and asynchronous writer boundaries."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import TaskSpec
from forgepilot.agents.implementation import ImplementationService
from forgepilot.errors import ForgePilotError
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.orchestration.models import NodeStatus
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.workspace_manager import WorkspaceManager
from test_analysis_workflow import AnalysisProvider, request
from test_implementation_workflow import (
    SOURCE, ImplementationProvider, assert_user_files_unchanged, close_case,
    make_case, patch, create, completed,
)
from test_workspace_manager import committed_repo, git
from conftest import reply


@pytest.mark.asyncio
async def test_scope_approval_retries_restore_after_outbox_failure(tmp_path, committed_repo, git_exe):
    ask = reply(json.dumps({"status": "awaiting_human", "summary": "Need header", "scope_expansion": {
        "files": [{"path": "worker.hpp", "reason": "Declare the worker API", "plan_step_id": "STEP-1",
                   "acceptance_criteria_ids": ["AC-1"]}]}}))
    case = await make_case(tmp_path, committed_repo, git_exe, [patch(), ask])
    try:
        assert (await case.service.approve(case.review)).status is NodeStatus.AWAITING_HUMAN
        expansions = case.service.scope_expansions
        preview = expansions.preview(case.task_id)
        decide = dict(approved=True, expected_revision=preview["revision"],
                      before_approve=case.service.restore_scope_checkpoint,
                      **{key: preview[key] for key in ("scope_hash", "task_spec_hash", "plan_hash")})
        case.events._db.execute("""CREATE TEMP TRIGGER fail_scope_approval BEFORE INSERT ON graph_events
            WHEN NEW.event_type='approval.approved' BEGIN SELECT RAISE(ABORT,'outbox failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="outbox failure"):
            expansions.decide(case.task_id, **decide)
        assert expansions.preview(case.task_id) == preview
        assert expansions.approved_package(case.task_id) is None
        assert (case.worktree / "source.txt").read_bytes() == SOURCE
        case.events._db.execute("DROP TRIGGER fail_scope_approval")
        expansions.decide(case.task_id, **decide)
        assert expansions.approved_package(case.task_id) == (preview["task_spec_artifact_id"], preview["plan_artifact_id"])
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_graph_gate_alone_cannot_grant_implementer_write_authority(tmp_path, committed_repo, git_exe):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    try:
        result = await case.service._run_worker(case.task_id, case.service.runner.approve_human,
            case.task_id, case.service.graph(), "execution_approval", expected_revision=case.review.revision)
        assert result.status is NodeStatus.AWAITING_HUMAN
        assert case.provider.seen == []
        assert (case.worktree / "source.txt").read_bytes() == SOURCE
        assert not case.changes()
        assert [cp.checkpoint_type for cp in case.checkpoints()] == ["plan_approved"]
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["revision", "task_spec_hash", "plan_hash", "scope_hash", "review_hash"])
async def test_forged_review_cannot_create_approval_or_branch(tmp_path, committed_repo, git_exe, field):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    forged = replace(case.review, **{field: case.review.revision + 1 if field == "revision" else "0" * 64})
    try:
        with pytest.raises(ForgePilotError, match="approval_stale"):
            await case.service.approve(forged)
        assert case.events._db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert case.manager._load(case.task_id)["branch"] is None
        assert case.provider.seen == []
        assert not case.checkpoints()
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_task_cannot_import_another_tasks_execution_package(tmp_path, committed_repo, git_exe):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    try:
        other_task = str(uuid4())
        with pytest.raises(ForgePilotError, match="artifact_scope"):
            await case.service.prepare(other_task, case.spec.artifact_id, case.plan.artifact_id)
        with pytest.raises(ForgePilotError, match="unknown_graph"):
            case.service.runner.store.snapshot(other_task)
        assert case.provider.seen == []
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_denial", [False, True])
async def test_absent_or_false_cloud_policy_blocks_model_and_rag(tmp_path, committed_repo, git_exe, explicit_denial):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    calls = []
    case.service.retrieval_factory = lambda repository: calls.append(repository)
    task_id = str(uuid4())
    case.manager.create(committed_repo, task_id)
    data = case.spec.content.model_dump(mode="json")
    if explicit_denial:
        data["data_policy"] = {"cloud_model_allowed": False, "external_mcp_allowed": False}
    else:
        data.pop("data_policy")
    spec = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="spec",
                           content=TaskSpec.model_validate(data))
    plan = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="plan",
        parent_artifact_ids=(spec.artifact_id,), content=case.plan.content.model_copy(update={
            "task_spec_artifact_id": spec.artifact_id}))
    try:
        case.events.artifacts.put(spec)
        case.events.artifacts.put(plan)
        await case.service.prepare(task_id, spec.artifact_id, plan.artifact_id)
        result = await case.service.approve(case.service.review(task_id))
        assert result.status is NodeStatus.FAILED
        assert case.provider.seen == [] and calls == []
        assert case.gateway.accounting.snapshot() == ()
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_revoked_approval_during_model_response_blocks_next_write(tmp_path, committed_repo, git_exe):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    original_chat = case.provider.chat

    async def revoke_before_reply(messages, tools=None):
        response = await original_chat(messages, tools)
        with case.events._transaction():
            case.events._db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=?",
                                    (case.task_id,))
        return response

    case.provider.chat = revoke_before_reply
    try:
        result = await case.service.approve(case.review)
        assert result.status is NodeStatus.AWAITING_HUMAN
        assert len(case.provider.seen) == 1
        assert (case.worktree / "source.txt").read_bytes() == SOURCE
        assert not case.changes()
        assert [cp.checkpoint_type for cp in case.checkpoints()] == ["plan_approved"]
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_deadline_expires_before_returned_patch_and_blocks_mutation(tmp_path, committed_repo, git_exe):
    case = await make_case(tmp_path, committed_repo, git_exe, [patch()])
    original_chat = case.provider.chat

    async def expire_before_reply(messages, tools=None):
        response = await original_chat(messages, tools)
        case.service._budgets[(case.task_id, "implementer")].deadline = 0
        return response

    case.provider.chat = expire_before_reply
    try:
        result = await case.service.approve(case.review)
        assert result.status is NodeStatus.FAILED
        assert len(case.provider.seen) == 1
        assert (case.worktree / "source.txt").read_bytes() == SOURCE
        assert not case.changes()
        assert [cp.checkpoint_type for cp in case.checkpoints()] == ["plan_approved"]
        assert_user_files_unchanged(case)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_late_provider_patch_after_cancellation_cannot_mutate(tmp_path, committed_repo, git_exe):
    class LateProvider(ImplementationProvider):
        async def chat(self, messages, tools=None):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                return patch()

    provider = LateProvider()
    case = await make_case(tmp_path, committed_repo, git_exe, provider=provider)
    work = asyncio.create_task(case.service.approve(case.review))
    try:
        # Approval creates a real Git checkpoint before the provider starts.
        await asyncio.wait_for(provider.started.wait(), 30)
        current = case.service.runner.store.snapshot(case.task_id)
        case.service.cancel(case.task_id, expected_revision=current.revision)
        result = await asyncio.wait_for(work, 30)
        assert result.status is NodeStatus.CANCELLED
        assert provider.cancelled.is_set()
        assert (case.worktree / "source.txt").read_bytes() == SOURCE
        assert not case.changes()
        assert [cp.checkpoint_type for cp in case.checkpoints()] == ["plan_approved"]
        assert_user_files_unchanged(case)
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await close_case(case)


@pytest.mark.asyncio
async def test_composed_analysis_approval_and_implementation_finish_at_checkpoint(tmp_path, committed_repo, git_exe):
    original = b"void Counter::increment() {\n    ++value;\n}\n"
    changed = "void Counter::increment() {\n    value += 2;\n}\n"
    (committed_repo / "Counter.cpp").write_bytes(original)
    git(git_exe, committed_repo, "add", "Counter.cpp")
    git(git_exe, committed_repo, "commit", "-q", "-m", "counter fixture")
    task_id = str(uuid4())
    manager = WorkspaceManager(tmp_path / "composed data", git_executable=git_exe)
    record = manager.create(committed_repo, task_id)
    worktree = Path(record["worktree_path"])
    analysis = AnalysisProvider(worktree)
    implementation = ImplementationProvider([
        patch("Counter.cpp", original, changed),
        create("tests/worker_lifecycle.cpp", "#include <cassert>\nvoid regression() { assert(2 == 2); }\n", step="STEP-1"),
        completed(),
    ])

    class CombinedProvider:
        async def chat(self, messages, tools=None):
            provider = implementation if "You are implementer.v1" in messages[0]["content"] else analysis
            return await provider.chat(messages, tools)

        async def aclose(self):
            pass

    configuration = ModelConfiguration(
        profiles=(ModelProfile(profile_id="fake", base_url="https://offline.test/v1",
            api_key_ref="env:FORGEPILOT_API_KEY", model="fake-combined", family="fake", max_output_tokens=1024),),
        routes=tuple(ModelRoute(route_id=route, primary="fake") for route in ("orchestration", "analysis", "implementation")),
        enforce_review_family_separation=False)
    gateway = ModelGateway(configuration, secret_resolver=lambda _: "fake-only-key", provider_factory=lambda *_: CombinedProvider())
    events = SqliteEventStore(tmp_path / "composed.sqlite3", workspace_backend=manager)
    service = ImplementationService(events, manager, gateway)
    try:
        paused = await service.prepare_analysis(task_id, request())
        assert paused.status is NodeStatus.AWAITING_HUMAN
        assert analysis.counts == {"draft": 1, "root": 1, "impact": 1, "plan": 1}
        assert implementation.seen == []
        assert manager._load(task_id)["branch"] is None
        finished = await service.approve(service.review(task_id))
        assert finished.status is NodeStatus.SUCCEEDED
        assert finished.nodes[-1].node_id == "implementer"
        assert (worktree / "Counter.cpp").read_text() == changed
        assert (committed_repo / "Counter.cpp").read_bytes() == original
        assert [cp.checkpoint_type for cp in service.runner.checkpoints.list_for_task(task_id)][-2:] == ["plan_approved", "implementation"]
        payload = json.loads(next(m["content"] for m in implementation.seen[0][0] if m["role"] == "user"))
        assert len(payload["evidence"]) == 1 and len(payload["evidence"][0]["items"]) == 2
        assert (await ImplementationService(events, manager, gateway).run(task_id)).status is NodeStatus.SUCCEEDED
        assert len(implementation.seen) == 3
        assert analysis.counts == {"draft": 1, "root": 1, "impact": 1, "plan": 1}
    finally:
        await gateway.aclose()
        events.close()
