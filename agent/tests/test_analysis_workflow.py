"""Offline provider contracts across the real gateway, graph, artifacts and approval."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3
from threading import Barrier, Event
from uuid import uuid4

import pytest

from forgepilot.agents.analysis import AnalysisRequest, AnalysisService
from forgepilot.agents.contracts import validate_task_spec
from forgepilot.artifacts.models import DataPolicy, RepositorySpec
from forgepilot.artifacts.store import ArtifactStorageError
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import append_mission_event
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.orchestration.graph import validate_graph
from forgepilot.orchestration.models import NodeStatus
from forgepilot.orchestration.workspace import StrictFakeWorkspaceBackend, WorkspaceSnapshot
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore
from conftest import call, reply


def spec_data(kind="bug_fix"):
    return dict(task_type=kind, title="Stop the worker on close", objective="Cancel background work before window destruction",
        repository={"base_ref": "HEAD", "toolchain_profile": "windows-msvc-qt6"},
        problem={"observed_behavior": "Worker continues after close", "expected_behavior": "Worker stops before destruction",
                 "reproduction_steps": ["Start worker", "Close window"]},
        acceptance_criteria=[
            {"id": "AC-1", "description": "Regression: worker stops before window destruction", "verification": "test"},
            {"id": "AC-2", "description": "The configured CMake target builds successfully", "verification": "build"},
            {"id": "AC-3", "description": "All existing CTest cases remain passing", "verification": "ctest"}],
        allowed_scope={"path_patterns": ["*.cpp", "tests/**", "CMakeLists.txt"], "max_changed_files": 4},
        forbidden_scope=[".git/**", "build/**", ".forgepilot/**"],
        test_policy={"existing_tests_read_only": True, "allow_new_tests": True, "modifiable_existing_test_patterns": []},
        data_policy={"cloud_model_allowed": True, "external_mcp_allowed": False},
        assumptions=["The caller selected the repository"], risks=["Cancellation may race with completion"], risk_level="medium")


def plan_data(spec):
    return {"task_spec_artifact_id": spec["artifact_id"], "summary": "Repair lifecycle and verify regressions",
        "steps": [
            {"step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [], "expected_files": ["Counter.cpp", "tests/worker_lifecycle.cpp"],
             "action": "Cancel worker before owner destruction and add focused regression coverage",
             "verification": ["test"], "risk": "Ownership races", "adds_tests": True, "acceptance_criteria_ids": ["AC-1"]},
            {"step_id": "STEP-2", "owner": "validator", "depends_on": ["STEP-1"], "expected_files": [],
             "action": "Build counter target and run existing CTest cases", "verification": ["build", "ctest"],
             "risk": "Toolchain unavailable", "adds_tests": False, "acceptance_criteria_ids": ["AC-2", "AC-3"]}]}


class AnalysisProvider:
    def __init__(self, root, *, invalid_stage=None, always_invalid=False, block=False, tool_loop=False, fail_root=False):
        self.root = root
        self.invalid_stage, self.always_invalid = invalid_stage, always_invalid
        self.block, self.tool_loop, self.fail_root = block, tool_loop, fail_root
        self.started = asyncio.Event()
        self.both = asyncio.Event()
        self.active = self.peak = 0
        self.seen = []
        self.counts = {}

    async def chat(self, messages, tools=None):
        prompt = messages[0]["content"]
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        stage = ("root" if "You are root-cause-analyst.v1" in prompt else
                 "impact" if "You are impact-test-analyst.v1" in prompt else
                 "draft" if "request" in payload else "plan")
        self.seen.append((stage, deepcopy(messages), [t.name for t in tools or []]))
        self.counts[stage] = self.counts.get(stage, 0) + 1
        if stage in {"root", "impact"}:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.both.set()
            self.started.set()
            try:
                await asyncio.wait_for(self.both.wait(), 5)
                if self.block:
                    await asyncio.Event().wait()
                if stage == "root" and self.fail_root:
                    from forgepilot.models.errors import ModelError
                    raise ModelError("llm_network", "fake transient", category="network")
                await asyncio.sleep(0.01)
            finally:
                self.active -= 1
        if self.invalid_stage == stage and (self.always_invalid or self.counts[stage] == 1):
            return reply("not valid json", usage={"prompt_tokens": 10, "completion_tokens": 3})
        if self.tool_loop and stage == "root":
            return reply(None, calls=[call("repository_read_range", '{"path":"Counter.cpp"}')],
                         usage={"prompt_tokens": 10, "completion_tokens": 3})
        if stage == "draft":
            content = spec_data("feature" if "feature" in payload["request"] else "bug_fix")
        elif stage == "plan":
            assert self.active == 0
            assert len(payload["evidence"]["content"]["items"]) == 2
            content = plan_data(payload["task_spec"])
        else:
            content = {"items": [{"evidence_id": "EV-1", "claim": "Counter::increment is a candidate repair location" if stage == "root" else "Counter target requires regression coverage",
                "reason": "The current method increments the stored counter", "confidence": 0.8,
                "path": "Counter.cpp", "start_line": 1, "end_line": 2, "symbol": "Counter::increment",
                "file_hash": sha256((self.root / "Counter.cpp").read_bytes()).hexdigest(), "source": "local_repository"}],
                "summary": "Lifecycle evidence" if stage == "root" else "Impact and tests evidence"}
        return reply(json.dumps(content), usage={"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50})

    async def aclose(self):
        pass


async def service_for(root, tmp_path, *, provider=None, workspace_backend=None):
    repository = await Repository.open(str(root), rg_executable="rg", git_executable="git")
    provider = provider or AnalysisProvider(root)
    configuration = ModelConfiguration(profiles=(ModelProfile(profile_id="fake", base_url="https://offline.test/v1",
        api_key_ref="env:FORGEPILOT_API_KEY", model="fake-model", family="fake", max_output_tokens=1024),),
        routes=(ModelRoute(route_id="orchestration", primary="fake"), ModelRoute(route_id="analysis", primary="fake")),
        enforce_review_family_separation=False)
    gateway = ModelGateway(configuration, secret_resolver=lambda _: "fake-only-key", provider_factory=lambda *_: provider)
    events = SqliteEventStore(tmp_path / "analysis.sqlite3", artifact_root=tmp_path / "tasks",
                              workspace_backend=workspace_backend)
    service = AnalysisService(events, repository, gateway)
    return service, provider, events, gateway


def request(text="Fix the worker lifecycle bug"):
    return AnalysisRequest(request=text, repository=RepositorySpec(base_ref="HEAD", toolchain_profile="windows-msvc-qt6"),
                           data_policy=DataPolicy(cloud_model_allowed=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bug_fix", "feature"])
async def test_natural_request_parallel_join_artifacts_single_approval(cpp_repo, tmp_path, kind):
    service, provider, events, gateway = await service_for(cpp_repo, tmp_path)
    task_id = str(uuid4())
    before = service.repository.audit_manifest()
    try:
        result = await service.run(task_id, request("Add a feature" if kind == "feature" else "Fix the worker"))
        assert result.status == NodeStatus.AWAITING_HUMAN
        assert provider.peak == 2
        assert provider.counts == {"draft": 1, "root": 1, "impact": 1, "plan": 1}
        trace = service.runner.events(task_id)
        started = [e.sequence for e in trace if e.event_type == "node_started" and e.node_id in {"root_cause_analysis", "impact_test_analysis"}]
        finished = [e.sequence for e in trace if e.event_type == "node_finished" and e.node_id in {"root_cause_analysis", "impact_test_analysis"}]
        assert max(started) < min(finished)
        assert next(e.sequence for e in trace if e.event_type == "node_started" and e.node_id == "join_evidence") > max(finished)
        review = service.review(task_id)
        spec = events.artifacts.get(review.task_spec_artifact_id)
        plan = events.artifacts.get(review.plan_artifact_id)
        assert spec.content.task_type == kind
        assert {ac for step in plan.content.steps for ac in step.acceptance_criteria_ids} == {"AC-1", "AC-2", "AC-3"}
        approval = service.approve(review)
        assert approval["valid"] and approval["task_spec_hash"] == review.task_spec_hash
        assert service.runner.store.snapshot(task_id).status == NodeStatus.SUCCEEDED
        assert events._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
        with pytest.raises(ForgePilotError):
            service.approve(review)
        assert service.repository.audit_manifest() == before
        assert all(not any(name.startswith("mcp_") or "patch" in name or name in {"cmake_build", "run_tests"} for name in names)
                   for _, _, names in provider.seen)
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_trusted_locked_task_spec_skips_model_draft_but_preserves_analysis_and_review(cpp_repo, tmp_path):
    service, provider, events, gateway = await service_for(cpp_repo, tmp_path)
    frozen = validate_task_spec(spec_data())
    task_id = str(uuid4())
    try:
        result = await service.run(task_id, request(), locked_task_spec=frozen)
        assert result.status is NodeStatus.AWAITING_HUMAN
        assert provider.counts == {"root": 1, "impact": 1, "plan": 1}
        assert provider.peak == 2
        review = service.review(task_id)
        assert events.artifacts.get(review.task_spec_artifact_id).content == frozen
        assert service.runner.store.snapshot(task_id).graph_id == "task-analysis-locked.v1"
        assert next(node for node in service.graph(request(), locked_task_spec=frozen).nodes
                    if node.node_id == "draft_task_spec").handler == "locked_task_spec"
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_locked_task_spec_rejects_changed_repository_before_model_or_graph(cpp_repo, tmp_path):
    service, provider, events, gateway = await service_for(cpp_repo, tmp_path)
    payload = spec_data()
    payload["repository"]["base_ref"] = "other"
    try:
        with pytest.raises(ForgePilotError, match="policy_denied"):
            await service.run(str(uuid4()), request(), locked_task_spec=payload)
        assert provider.seen == []
        assert not events._db.execute("SELECT 1 FROM task_graph_runs").fetchone()
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_approval_revision_changes_hash_and_rejects_old_review(cpp_repo, tmp_path):
    service, _, events, gateway = await service_for(cpp_repo, tmp_path)
    task_id = str(uuid4())
    try:
        await service.run(task_id, request())
        first = service.review(task_id)
        approval = service.approve(first)
        current = service.review(task_id)
        spec = events.artifacts.get(current.task_spec_artifact_id).content.model_dump(mode="json")
        spec["allowed_scope"]["max_changed_files"] = 3
        plan = events.artifacts.get(current.plan_artifact_id).content
        last_event = service.runner.events(task_id)[-1]
        revised = service.revise(current, task_spec=spec, plan=plan)
        assert [(e.revision, e.event_type) for e in service.runner.events(task_id, after_sequence=last_event.sequence)] == [
            (current.revision + 1, "execution_review_revised")]
        assert revised.scope_hash != first.scope_hash and revised.task_spec_hash != first.task_spec_hash
        assert not events.missions.is_approval_valid(approval["approval_id"])
        with pytest.raises(ForgePilotError, match="approval_stale"):
            service.approve(first)
        assert service.approve(revised)["valid"]
        attempts = events._db.execute("SELECT attempt FROM task_node_attempts WHERE task_id=? AND node_id='execution_approval' ORDER BY attempt", (task_id,)).fetchall()
        assert [r[0] for r in attempts] == [1, 2]
    finally:
        await gateway.aclose()
        events.close()


async def run_admitted_analysis(service, task_id, root):
    analysis_request = request()
    graph = validate_graph(service.graph(analysis_request))
    service.runner.store.create_run(task_id, graph, policy_hash=service.runner.policy.definition_hash)
    workspace = WorkspaceSnapshot(str(uuid4()), task_id, "a" * 40, {})
    record = {"task_id": task_id, "workspace_id": workspace.workspace_id, "repository_path": str(root),
              "worktree_path": str(root), "base_ref": "HEAD", "head_commit": workspace.head_commit, "status": "active"}
    service.events.workspace_backend.register(workspace, record)
    service.events.missions.admit_mission(task_id, record, {"repository_path": str(root), "request": analysis_request.request})
    assert (await service.run(task_id, analysis_request)).status is NodeStatus.AWAITING_HUMAN


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "BEFORE INSERT ON artifacts WHEN NEW.artifact_type='ExecutionPlan' AND NEW.producer_node='review_revision'",
    "BEFORE UPDATE OF scope_hash ON task_graph_runs",
], ids=["second-artifact", "gate-update"])
async def test_failed_revision_preserves_approved_package_across_restart(cpp_repo, tmp_path, monkeypatch, failure):
    workspace = StrictFakeWorkspaceBackend()
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, workspace_backend=workspace)
    task_id = str(uuid4())
    try:
        await run_admitted_analysis(service, task_id, cpp_repo)
        approval = service.approve(service.review(task_id))
        old = service.review(task_id)
        spec = events.artifacts.get(old.task_spec_artifact_id).content
        plan = events.artifacts.get(old.plan_artifact_id).content
        trace = service.runner.events(task_id)
        graph = service.runner.store.snapshot(task_id)
        artifacts = events.artifacts.list_for_task(task_id)
        # Exercise rollback of both external payloads, including the first registration.
        monkeypatch.setattr("forgepilot.artifacts.sqlite_store.INLINE_LIMIT_BYTES", 1)
        events._db.execute(f"""CREATE TEMP TRIGGER reject_revision {failure}
            BEGIN SELECT RAISE(ABORT, 'injected revision failure'); END""")
        with pytest.raises(ArtifactStorageError):
            service.revise(old, task_spec=spec, plan=plan)

        def assert_original_package():
            assert service.review(task_id) == old
            assert service.runner.store.snapshot(task_id) == graph
            assert service.runner.events(task_id) == trace
            assert events.artifacts.list_for_task(task_id) == artifacts
            assert events.missions.is_approval_valid(approval["approval_id"])
            assert not list((events.artifacts.root / task_id / "artifacts").glob("*.json"))

        assert_original_package()
        events.close()  # The connection-scoped injection disappears on restart.
        events = SqliteEventStore(tmp_path / "analysis.sqlite3", artifact_root=tmp_path / "tasks", workspace_backend=workspace)
        service = AnalysisService(events, service.repository, gateway)
        assert_original_package()
        revised = service.revise(old, task_spec=spec, plan=plan)
        assert service.approve(revised)["valid"]
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_revision_uses_actual_mission_revision_when_an_event_intervenes(cpp_repo, tmp_path):
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, workspace_backend=StrictFakeWorkspaceBackend())
    task_id = str(uuid4())
    try:
        await run_admitted_analysis(service, task_id, cpp_repo)
        old = service.review(task_id)
        last_sequence = service.runner.events(task_id)[-1].sequence
        events._db.create_function("append_audit_event", 0,
            lambda: append_mission_event(events, task_id, "task.audit", {}))
        events._db.execute("""CREATE TEMP TRIGGER extra_revision_event BEFORE INSERT ON artifacts
            WHEN NEW.artifact_type='ExecutionPlan' AND NEW.producer_node='review_revision'
            BEGIN SELECT append_audit_event(); END""")
        revised = service.revise(old, task_spec=events.artifacts.get(old.task_spec_artifact_id).content,
                                 plan=events.artifacts.get(old.plan_artifact_id).content)
        trace = service.runner.events(task_id, after_sequence=last_sequence)
        assert [e.event_type for e in trace] == ["artifact.created", "task.audit", "artifact.created", "execution_review_revised"]
        assert [e.revision for e in trace] == list(range(old.revision + 1, revised.revision + 1))
        assert service.approve(revised)["valid"]
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_simultaneous_mission_revisions_publish_only_one_complete_package(cpp_repo, tmp_path):
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, workspace_backend=StrictFakeWorkspaceBackend())
    task_id = str(uuid4())
    try:
        await run_admitted_analysis(service, task_id, cpp_repo)
        old = service.review(task_id)
        spec = events.artifacts.get(old.task_spec_artifact_id).content
        plan = events.artifacts.get(old.plan_artifact_id).content
        start, at_gate, release = Barrier(2), Event(), Event()
        last_sequence = service.runner.events(task_id)[-1].sequence

        def pause_gate_update():
            at_gate.set()
            if not release.wait(10):
                raise RuntimeError("Gate update was not released")
            return 0

        def revise(summary):
            start.wait(timeout=10)
            return service.revise(old, task_spec=spec, plan=plan.model_copy(update={"summary": summary}))

        events._db.create_function("pause_gate_update", 0, pause_gate_update)
        events._db.execute("""CREATE TEMP TRIGGER pause_revision BEFORE UPDATE OF scope_hash ON task_graph_runs
            BEGIN SELECT pause_gate_update(); END""")
        with ThreadPoolExecutor(max_workers=2) as workers:
            writers = [workers.submit(revise, summary) for summary in ("First revision", "Other revision")]
            try:
                assert at_gate.wait(10)
                # A WAL reader bypasses the process-local lock while the writer is inside the gate update.
                with closing(sqlite3.connect(events.path)) as reader:
                    assert reader.execute("SELECT task_spec_artifact_id,plan_artifact_id,current_revision "
                        "FROM task_graph_runs WHERE task_id=?", (task_id,)).fetchone() == (
                        old.task_spec_artifact_id, old.plan_artifact_id, old.revision)
                    assert not reader.execute("SELECT 1 FROM artifacts WHERE producer_node='review_revision'").fetchone()
                    assert not reader.execute("SELECT 1 FROM graph_events WHERE task_id=? AND sequence>?",
                                              (task_id, last_sequence)).fetchone()
            finally:
                release.set()
        winner, = [writer.result() for writer in writers if writer.exception() is None]
        error, = [writer.exception() for writer in writers if writer.exception() is not None]
        assert isinstance(error, ForgePilotError) and error.code == "approval_stale"
        assert service.review(task_id) == winner
        new_spec = events.artifacts.get(winner.task_spec_artifact_id)
        new_plan = events.artifacts.get(winner.plan_artifact_id)
        assert {a.artifact_id for a in events.artifacts.list_for_task(task_id) if a.producer_node == "review_revision"} == {
            new_spec.artifact_id, new_plan.artifact_id}
        assert (new_spec.content_sha256, new_plan.content_sha256) == (winner.task_spec_hash, winner.plan_hash)
        assert new_plan.content.task_spec_artifact_id == new_spec.artifact_id
        assert {r.parent_artifact_id for r in events.artifacts.parents_of(new_spec.artifact_id)} == {old.task_spec_artifact_id}
        assert {r.parent_artifact_id for r in events.artifacts.parents_of(new_plan.artifact_id)} == set(new_plan.parent_artifact_ids)
        created_spec, created_plan, gate = service.runner.events(task_id, after_sequence=last_sequence)
        for offset, (event, artifact) in enumerate(((created_spec, new_spec), (created_plan, new_plan)), start=1):
            assert (event.revision, event.event_type) == (old.revision + offset, "artifact.created")
            assert event.payload["artifact_id"] == artifact.artifact_id
            assert event.payload["content_sha256"] == artifact.content_sha256
        assert (gate.revision, gate.event_type) == (winner.revision, "execution_review_revised")
        assert gate.revision == created_plan.revision + 1
        assert gate.payload == {"task_spec_artifact_id": new_spec.artifact_id, "plan_artifact_id": new_plan.artifact_id}
        assert service.approve(winner)["valid"]
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["draft", "root", "impact", "plan"])
async def test_schema_repair_uses_real_gateway_bounded_contract(cpp_repo, tmp_path, stage):
    provider = AnalysisProvider(cpp_repo, invalid_stage=stage)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    try:
        assert (await service.run(str(uuid4()), request())).status == NodeStatus.AWAITING_HUMAN
        assert provider.counts[stage] == 2
        assert sum(r.attempt == "schema_repair" for r in gateway.accounting.snapshot()) == 1
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["contract", "budget"])
async def test_analyst_failure_never_fabricates_join_or_plan(cpp_repo, tmp_path, mode):
    provider = AnalysisProvider(cpp_repo, invalid_stage="root" if mode == "contract" else None,
                                always_invalid=True, tool_loop=mode == "budget")
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    try:
        result = await service.run(str(uuid4()), request())
        assert result.status == NodeStatus.FAILED
        assert "plan" not in provider.counts
        assert provider.counts["root"] == (2 if mode == "contract" else 6)
        root = next(n for n in result.nodes if n.node_id == "root_cause_analysis")
        assert not root.result.output_artifact_ids
        assert next(n for n in result.nodes if n.node_id == "join_evidence").attempt == 0
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_stale_hash_blocks_approval(cpp_repo, tmp_path):
    service, _, events, gateway = await service_for(cpp_repo, tmp_path)
    try:
        task_id = str(uuid4())
        await service.run(task_id, request())
        review = service.review(task_id)
        with (cpp_repo / "Counter.cpp").open("a") as stream:
            stream.write("\n// independently changed\n")
        with pytest.raises(ForgePilotError, match="evidence_stale"):
            service.approve(review)
        assert events._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_cancel_drains_model_requests_and_prevents_join(cpp_repo, tmp_path):
    provider = AnalysisProvider(cpp_repo, block=True)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    task_id = str(uuid4())
    worker = asyncio.create_task(service.run(task_id, request()))
    try:
        await asyncio.wait_for(provider.both.wait(), 5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 5)
        await asyncio.sleep(0)
        assert provider.active == 0
        snapshot = service.runner.store.snapshot(task_id)
        assert snapshot.status == NodeStatus.CANCELLED
        assert next(n for n in snapshot.nodes if n.node_id == "join_evidence").attempt == 0
        assert "plan" not in provider.counts
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_asynchronous_provider_cleanup(cpp_repo, tmp_path):
    class CleanupProvider(AnalysisProvider):
        cleaned = 0
        async def chat(self, messages, tools=None):
            try:
                return await super().chat(messages, tools)
            except asyncio.CancelledError:
                await asyncio.sleep(0.1)
                self.cleaned += 1
                raise
    provider = CleanupProvider(cpp_repo, block=True)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    worker = asyncio.create_task(service.run(str(uuid4()), request()))
    try:
        await asyncio.wait_for(provider.both.wait(), 5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 5)
        assert provider.cleaned == 2
        assert not any(service._pending.values())
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bind", "approve", "revise"])
async def test_review_storage_failure_is_atomic_and_never_retryable(cpp_repo, tmp_path, operation):
    service, _, events, gateway = await service_for(cpp_repo, tmp_path)
    task_id = str(uuid4())
    try:
        if operation != "bind":
            await service.run(task_id, request())
            review = service.review(task_id)
        target = "BEFORE INSERT ON approvals" if operation == "approve" else "BEFORE UPDATE OF scope_hash ON task_graph_runs"
        events._db.execute(f"CREATE TRIGGER injected_storage_failure {target} BEGIN SELECT RAISE(ABORT, 'private failure details'); END")
        if operation == "bind":
            with pytest.raises(ForgePilotError, match="graph_storage"):
                await service.run(task_id, request())
            assert service.runner.store.snapshot(task_id).status != NodeStatus.RETRYABLE
        elif operation == "approve":
            with pytest.raises(ArtifactStorageError, match="persistence failed"):
                service.approve(review)
            assert service.review(task_id) == review
        else:
            with pytest.raises(ArtifactStorageError, match="persistence failed"):
                service.revise(review, task_spec=events.artifacts.get(review.task_spec_artifact_id).content,
                               plan=events.artifacts.get(review.plan_artifact_id).content)
            assert service.review(task_id) == review
        assert events._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_restart_cannot_replenish_failed_node_budget(cpp_repo, tmp_path):
    provider = AnalysisProvider(cpp_repo, fail_root=True)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    task_id = str(uuid4())
    try:
        failed = await service.run(task_id, request())
        assert failed.status == NodeStatus.RETRYABLE
        root = next(n for n in failed.nodes if n.node_id == "root_cause_analysis")
        assert root.result.metrics.model_calls == 2
        replacement = AnalysisService(events, service.repository, gateway)
        retried = await replacement.retry(task_id, request(), "root_cause_analysis", expected_revision=failed.revision)
        assert retried.status == NodeStatus.FAILED
        assert provider.counts["root"] == 2
        assert next(n for n in retried.nodes if n.node_id == "root_cause_analysis").result.error_summary == "llm_budget"
    finally:
        await gateway.aclose()
        events.close()


@pytest.mark.asyncio
async def test_tool_budget_is_charged_before_dispatch(cpp_repo, tmp_path):
    from forgepilot.models.accounting import ModelBudget
    provider = AnalysisProvider(cpp_repo, tool_loop=True)
    service, _, events, gateway = await service_for(cpp_repo, tmp_path, provider=provider)
    task_id = str(uuid4())
    service._budgets[(task_id, "root_cause_analysis")] = ModelBudget(max_model_calls=4,
        max_tool_calls=0, max_input_tokens=40000, max_output_tokens=12000)
    try:
        result = await service.run(task_id, request())
        assert result.status == NodeStatus.FAILED
        assert provider.counts["root"] == 1
        root = next(n for n in result.nodes if n.node_id == "root_cause_analysis")
        assert root.result.metrics.model_calls == 1 and root.result.metrics.tool_calls == 0
        assert root.result.error_summary == "llm_budget"
    finally:
        await gateway.aclose()
        events.close()
