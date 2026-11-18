"""Execution approval precedes all deterministic baseline process work."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import ExecutionPlan
from forgepilot.build_process import CommandResult
from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.models import NodeStatus
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.validation import ValidationService
from forgepilot.workspace_manager import WorkspaceManager
from test_implementation_workflow import ImplementationProvider, completed
from test_validation_service import validator_case


@pytest_asyncio.fixture
async def baseline_case(tmp_path, validator_case, monkeypatch):
    manager, previous_task, _, validator, spec, _, _ = validator_case
    task = str(uuid4())
    root = Path(manager.create(manager.record(previous_task)["repository_path"], task)["worktree_path"])
    events = SqliteEventStore(tmp_path / "baseline.sqlite3", workspace_backend=manager,
                             artifact_root=tmp_path / "baseline artifacts")
    frozen = create_artifact(task_id=task, producer="orchestrator.v1", producer_node="draft_task_spec",
                             content=spec)
    plan = create_artifact(task_id=task, producer="orchestrator.v1", producer_node="generate_plan",
        parent_artifact_ids=(frozen.artifact_id,), content=ExecutionPlan.model_validate({
            "task_spec_artifact_id": frozen.artifact_id, "summary": "Update the approved source",
            "steps": [{"step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [],
                "expected_files": ["source.cpp"], "action": "Update the source and validate",
                "verification": ["ctest"], "risk": "Preserve the tests", "adds_tests": False,
                "acceptance_criteria_ids": ["AC-1"]}]}))
    events.artifacts.put(frozen)
    events.artifacts.put(plan)
    provider = ImplementationProvider([completed("awaiting_human")])
    configuration = ModelConfiguration(profiles=(ModelProfile(profile_id="fake",
        base_url="http://127.0.0.1:1/v1", api_key_ref="env:FORGEPILOT_API_KEY",
        model="fake-baseline", family="fake", max_output_tokens=1024),),
        routes=tuple(ModelRoute(route_id=role, primary="fake")
                     for role in ("implementation", "repair", "review")),
        enforce_review_family_separation=False)
    gateway = ModelGateway(configuration, secret_resolver=lambda _: "offline-only",
                           provider_factory=lambda *_: provider)
    calls = []

    async def process(argv, cwd, **kwargs):
        phase = "inventory" if "--show-only=json-v1" in argv else "configure"
        calls.append(phase)
        output = json.dumps({"tests": [{"name": "public"}]}) if phase == "inventory" else ""
        return CommandResult(argv, str(cwd), 0, output, "", 1, "succeeded")

    monkeypatch.setattr("forgepilot.validation.run_build_process", process)
    service = DeliveryWorkflow(events, manager, gateway, validator=validator)
    case = SimpleNamespace(task_id=task, root=root, manager=manager, events=events,
        validator=validator, service=service, provider=provider, gateway=gateway,
        spec=frozen, plan=plan, calls=calls, process=process, monkeypatch=monkeypatch)
    try:
        paused = await service.prepare(task, frozen.artifact_id, plan.artifact_id)
        assert paused.status is NodeStatus.AWAITING_HUMAN, paused
        case.review = service.review(task)
        yield case
    finally:
        await gateway.aclose()
        case.events.close()


def node(case, identifier):
    return next(item for item in case.service.runner.store.snapshot(case.task_id).nodes
                if item.node_id == identifier)


def assert_no_writer(case):
    assert case.provider.seen == []
    assert node(case, "implementer").attempt == 0
    assert all(node(case, f"repair_{index}").attempt == 0 for index in (1, 2, 3))


@pytest.mark.asyncio
async def test_preapproval_and_rejection_never_run_a_process(baseline_case):
    case = baseline_case
    assert case.calls == []
    assert not case.validator.guard.path(case.task_id + "/integrity.json", missing=True).exists()
    assert node(case, "baseline_capture").attempt == 0
    assert_no_writer(case)

    rejected = case.service.runner.store.cancel(case.task_id, expected_revision=case.review.revision,
        rejected_approval={**asdict(case.review), "approval_id": str(uuid4())})
    assert rejected.status is NodeStatus.CANCELLED
    assert (await case.service.run(case.task_id)).status is NodeStatus.CANCELLED
    assert case.events._db.execute("SELECT status FROM approvals").fetchone()[0] == "rejected"
    assert case.calls == []
    assert_no_writer(case)


@pytest.mark.asyncio
async def test_approval_captures_baseline_before_starting_implementer(baseline_case):
    case = baseline_case
    chat = case.provider.chat

    async def checked_chat(*args, **kwargs):
        assert case.calls == ["configure", "inventory"]
        assert node(case, "baseline_capture").status is NodeStatus.SUCCEEDED
        checkpoint = case.service.runner.checkpoints.latest(case.task_id)
        assert checkpoint.checkpoint_type == "plan_approved"
        raw = json.loads(case.validator.guard.read(case.task_id + "/integrity.json"))
        assert raw["approval_checkpoint_hash"] == checkpoint.data_sha256
        assert raw["file_hashes"] == dict(checkpoint.file_hashes)
        assert case.events._db.execute("SELECT count(*) FROM approvals WHERE status='approved'").fetchone()[0] == 1
        return await chat(*args, **kwargs)

    case.provider.chat = checked_chat
    result = await case.service.approve(case.review)
    assert result.status is NodeStatus.AWAITING_HUMAN
    assert len(case.provider.seen) == 1
    assert node(case, "baseline_capture").attempt == 1
    assert case.service.graph().version == 2


@pytest.mark.asyncio
async def test_configure_failure_pauses_only_baseline_and_retry_precedes_writer(baseline_case):
    case = baseline_case

    async def unavailable(argv, cwd, **kwargs):
        case.calls.append("failed configure")
        return CommandResult(argv, str(cwd), 1, "Could not find compiler", "", 1, "failed")

    case.monkeypatch.setattr("forgepilot.validation.run_build_process", unavailable)
    paused = await case.service.approve(case.review)
    assert paused.status is NodeStatus.RETRYABLE
    assert node(case, "baseline_capture").status is NodeStatus.RETRYABLE
    assert case.calls == ["failed configure"]
    assert_no_writer(case)
    assert case.events._db.execute("SELECT count(*) FROM validation_runs").fetchone()[0] == 0
    assert (await case.service.run(case.task_id)).status is NodeStatus.RETRYABLE
    assert case.calls == ["failed configure"]

    case.monkeypatch.setattr("forgepilot.validation.run_build_process", case.process)
    resumed = await case.service.resume_infrastructure(case.task_id)
    assert resumed.status is NodeStatus.AWAITING_HUMAN
    assert case.calls == ["failed configure", "configure", "inventory"]
    assert node(case, "baseline_capture").attempt == 2
    assert len(case.provider.seen) == 1
    assert all(node(case, f"repair_{index}").attempt == 0 for index in (1, 2, 3))


@pytest.mark.asyncio
@pytest.mark.parametrize("configure_status", ["succeeded", "failed"])
async def test_configure_source_write_is_policy_failure_even_if_configure_fails(baseline_case, configure_status):
    case = baseline_case

    async def mutating_configure(argv, cwd, **kwargs):
        if "--show-only=json-v1" in argv:
            return await case.process(argv, cwd, **kwargs)
        case.calls.append("configure")
        (case.root / "source.cpp").write_text("int value = 42;\n")
        return CommandResult(argv, str(cwd), 0 if configure_status == "succeeded" else 1,
                             "", "", 1, configure_status)

    case.monkeypatch.setattr("forgepilot.validation.run_build_process", mutating_configure)
    result = await case.service.approve(case.review)
    assert result.status is NodeStatus.FAILED
    assert node(case, "baseline_capture").result.failure_kind.value == "tool_policy_violation"
    assert_no_writer(case)
    assert not case.validator.guard.path(case.task_id + "/integrity.json", missing=True).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["approval_checkpoint_hash", "file_hashes"])
async def test_saved_baseline_cannot_be_reused_with_different_approval_or_source_inventory(
        baseline_case, changed_field):
    case = baseline_case
    await case.service.approve(case.review)
    checkpoint = case.service.runner.checkpoints.latest(case.task_id)
    relative = case.task_id + "/integrity.json"
    raw = json.loads(case.validator.guard.read(relative))
    if changed_field == "approval_checkpoint_hash":
        raw[changed_field] = "0" * 64
    else:
        raw[changed_field]["source.cpp"] = "0" * 64
    case.validator.guard.write(relative, json.dumps(raw).encode())
    with pytest.raises(ForgePilotError, match="validation_policy"):
        await case.validator.capture(case.task_id, case.spec.content, approval_checkpoint=checkpoint)
    assert case.calls == ["configure", "inventory"]


@pytest.mark.asyncio
async def test_cancel_during_configure_cannot_resume_past_baseline(baseline_case):
    case = baseline_case
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked_process(argv, cwd, **kwargs):
        case.calls.append("configure")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    case.monkeypatch.setattr("forgepilot.validation.run_build_process", blocked_process)
    running = asyncio.create_task(case.service.approve(case.review))
    try:
        # Managed Git approval and its checkpoint precede the fake process.
        await asyncio.wait_for(started.wait(), 60)
        snapshot = case.service.runner.store.snapshot(case.task_id)
        case.service.cancel(case.task_id, expected_revision=snapshot.revision)
        result = await asyncio.wait_for(running, 30)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert result.status is NodeStatus.CANCELLED
    assert stopped.is_set()
    assert_no_writer(case)
    recovery = case.service.runner.prepare_recovery(case.task_id, case.service._stored_graph(case.task_id))
    assert recovery.blocked_reason == "terminal_graph"
    assert (await case.service.run(case.task_id)).status is NodeStatus.CANCELLED
    assert case.calls == ["configure"]
    assert_no_writer(case)


class BaselineProcessCrash(BaseException):
    """A lost process leaves a claimed deterministic node for explicit recovery."""


@pytest.mark.asyncio
@pytest.mark.parametrize("after_capture", [False, True])
async def test_restart_recovery_reexecutes_baseline_before_implementer(baseline_case, git_exe, after_capture):
    case = baseline_case
    capture = case.validator.capture

    async def interrupted_capture(*args, **kwargs):
        if after_capture:
            await capture(*args, **kwargs)
        raise BaselineProcessCrash()

    case.validator.capture = interrupted_capture
    with pytest.raises(BaselineProcessCrash):
        await case.service.approve(case.review)
    assert node(case, "baseline_capture").status is NodeStatus.RUNNING
    assert_no_writer(case)
    database, artifact_root = case.events.path, case.events.artifacts.root
    case.events.close()
    case.manager = WorkspaceManager(case.manager.root, git_executable=git_exe)
    case.events = SqliteEventStore(database, workspace_backend=case.manager, artifact_root=artifact_root)
    old = case.validator
    case.validator = ValidationService(case.manager, old.profile, build_root=old.guard.root,
        oracle=old.oracle, generator=old.generator, make_program=old.make_program)
    case.service = DeliveryWorkflow(case.events, case.manager, case.gateway, validator=case.validator)
    graph = case.service._stored_graph(case.task_id)
    assert (await case.service.run(case.task_id)).status is NodeStatus.INTERRUPTED
    assert_no_writer(case)
    recovery = case.service.runner.prepare_recovery(case.task_id, graph)
    assert recovery.blocked_reason is None, recovery
    action = next(item for item in recovery.actions if item["node_id"] == "baseline_capture")
    assert action["action"] == "retry_frozen"
    case.service.runner.confirm_recovery(case.task_id, graph, token=recovery.token,
                                        expected_revision=recovery.revision)
    assert_no_writer(case)
    result = await case.service.run(case.task_id)
    assert result.status is NodeStatus.AWAITING_HUMAN
    assert node(case, "baseline_capture").status is NodeStatus.SUCCEEDED
    assert node(case, "baseline_capture").attempt == 2
    assert len(case.provider.seen) == 1
    # A captured baseline survives a crash before node publication without
    # running configure again or replacing its original source inventory.
    assert case.calls == ["configure", "inventory"]
