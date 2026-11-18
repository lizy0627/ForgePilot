"""Host recovery derives exact observed data; it never adopts or marks success."""
from hashlib import sha256
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import TaskSpec, ExecutionPlan
from forgepilot.errors import ForgePilotError
from forgepilot.workspace_manager import WorkspaceManager


OLD = b"int value(int x) { return x; }\r\n// user comment\r\n"
NEW = b"int value(int x) { return x < 0 ? 0 : x; }\r\n// user comment\r\n"


@pytest.fixture
def observed_case(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/value.cpp").write_bytes(OLD)
    (source / "README.txt").write_bytes(b"Original documentation\n")
    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True).stdout
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Recovery Fixture")
    git("config", "user.email", "recovery@localhost")
    git("config", "core.autocrlf", "false")
    git("add", "--all")
    git("commit", "-qm", "baseline")
    manager = WorkspaceManager(tmp_path / "owned")
    task = str(uuid4())
    record = manager.create(source, task)
    manager.approve_plan(task, approved=True, expected=manager.inspect_task(task), approved_paths=["src/value.cpp"])
    baseline = manager.inspect_task(task)
    spec = create_artifact(task_id=task, producer="orchestrator.v1", producer_node="draft_task_spec",
        content=TaskSpec.model_validate({
            "task_type": "bug_fix", "title": "Clamp negative values", "objective": "Clamp negative values to zero",
            "repository": {"base_ref": "HEAD", "toolchain_profile": "cmake-ctest-v1"},
            "problem": {"observed_behavior": "Returns negative input", "expected_behavior": "Returns zero for negatives",
                        "reproduction_steps": ["Call value(-1)"]},
            "acceptance_criteria": [{"id": "AC-1", "description": "Negative input returns zero", "verification": "protected_test"}],
            "allowed_scope": {"path_patterns": ["src/**"], "max_changed_files": 1},
            "forbidden_scope": [".git/**", "tests/**"], "test_policy": {"existing_tests_read_only": True, "allow_new_tests": False},
            "data_policy": {"cloud_model_allowed": True, "external_mcp_allowed": False},
            "assumptions": [], "risks": ["Behavior change"], "risk_level": "low"}))
    plan = create_artifact(task_id=task, producer="orchestrator.v1", producer_node="generate_plan",
        parent_artifact_ids=(spec.artifact_id,), content=ExecutionPlan.model_validate({
            "task_spec_artifact_id": spec.artifact_id, "summary": "Clamp the value",
            "steps": [{"step_id": "STEP-1", "owner": "implementer", "depends_on": [],
                       "expected_files": ["src/value.cpp"], "action": "Clamp negative values",
                       "verification": ["protected_test"], "risk": "Behavior change", "adds_tests": False,
                       "acceptance_criteria_ids": ["AC-1"]}]}))
    with pytest.raises(RuntimeError, match="storage interruption"):
        with manager.writer(task, expected=baseline) as worktree:
            (worktree / "src/value.cpp").write_bytes(NEW)
            raise RuntimeError("storage interruption")
    return manager, source, task, Path(record["worktree_path"]), baseline, spec, plan


def test_observed_recovery_keeps_crlf_and_returns_data_without_adopting(observed_case):
    manager, source, task, worktree, baseline, spec, plan = observed_case
    journal = manager.guard.read(manager._state_path(task))
    before = manager.inspect_task(task)
    changes = manager.recover_observed_change_set(task, spec, plan, baseline)
    changed, = changes.files
    assert changed.path == "src/value.cpp" and changed.test_change == "none"
    assert changed.before_sha256 == sha256(OLD).hexdigest() and changed.after_sha256 == sha256(NEW).hexdigest()
    assert changed.plan_step_ids == ("STEP-1",) and changed.acceptance_criteria_ids == ("AC-1",)
    assert changed.unified_diff.count("\r\n") == 3
    assert "Host recovered observed" in changes.summary
    assert manager.inspect_task(task) == before
    assert manager.guard.read(manager._state_path(task)) == journal
    assert (source / "src/value.cpp").read_bytes() == OLD and (worktree / "src/value.cpp").read_bytes() == NEW


def test_observed_recovery_rejects_external_file_after_interruption(observed_case):
    manager, _, task, worktree, baseline, spec, plan = observed_case
    (worktree / "unknown.txt").write_bytes(b"Unobserved user data\n")
    with pytest.raises(ForgePilotError, match="workspace_changed"):
        manager.recover_observed_change_set(task, spec, plan, baseline)
    assert (worktree / "unknown.txt").read_bytes() == b"Unobserved user data\n"


def test_observed_recovery_rejects_ambiguous_plan_mapping(observed_case):
    manager, _, task, _, baseline, spec, plan = observed_case
    extra = plan.content.steps[0].model_copy(update={"step_id": "STEP-2"})
    ambiguous = create_artifact(task_id=task, producer="orchestrator.v1", producer_node="generate_plan",
        parent_artifact_ids=(spec.artifact_id,), content=plan.content.model_copy(update={"steps": (*plan.content.steps, extra)}))
    with pytest.raises(ForgePilotError, match="unique approved plan"):
        manager.recover_observed_change_set(task, spec, ambiguous, baseline)
