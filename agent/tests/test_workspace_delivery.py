"""Real local Git delivery scenarios; no remote repository is configured."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from forgepilot.artifacts.models import DeliveryBundle
from forgepilot.errors import ForgePilotError
from forgepilot.git import SafeGit
from forgepilot.workspace_manager import WorkspaceManager


def git(root, *args, data=None):
    result = subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=True)
    return result.stdout


@pytest.fixture
def delivery_repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "config", "user.name", "User")
    git(source, "config", "user.email", "user@example.invalid")
    git(source, "config", "core.autocrlf", "false")
    (source / "file.txt").write_bytes(b"base\n")
    git(source, "add", "--", "file.txt")
    git(source, "commit", "-qm", "base")
    manager = WorkspaceManager(tmp_path / "data")
    task = str(uuid4())
    manager.create(source, task)
    manager.approve_plan(task, approved=True, expected=manager.inspect_task(task))
    return manager, source, task


def change_and_validate(manager, task, content=b"task\n", *, new_file=False):
    with manager.writer(task, expected=manager.inspect_task(task)) as worktree:
        (worktree / "file.txt").write_bytes(content)
        if new_file:
            (worktree / "nested" / "new").mkdir(parents=True)
            (worktree / "nested" / "new" / "result.txt").write_bytes(b"new directory tree\n")
    snapshot = manager.checkpoint_commit(task, kind="implementation", expected=manager.inspect_task(task))
    manager.mark_validated(task, expected=snapshot)
    return snapshot


def preview(manager, task):
    return manager.delivery_preview(task, "main")["token"]


def artifact_bytes(manager, artifact):
    # Artifact readers use the same guarded path boundary as production. This
    # also works on Windows hosts where ordinary open() is limited to MAX_PATH.
    relative = Path(artifact["path"]).relative_to(manager.root).as_posix()
    return manager.guard.read(relative)


def bundle_for(snapshot):
    return DeliveryBundle(
        change_set_artifact_id=str(uuid4()), validation_report_artifact_id=str(uuid4()),
        review_report_artifact_id=str(uuid4()), commit=snapshot.head_commit,
        build_test_summary="Local tests passed.", reviewer_conclusion="Approved.",
        changed_files=("file.txt",), usage_instructions="Use the updated file.", risks=(),
        rollback="Revert the delivery commit.",
    )


def test_squash_uses_fixed_identity_single_parent_and_keeps_task_branch(delivery_repo):
    manager, source, task = delivery_repo
    before = git(source, "rev-parse", "HEAD").strip()
    snapshot = change_and_validate(manager, task, new_file=True)
    result = manager.deliver(task, "squash", approved=True, token=preview(manager, task))
    assert result["commit"].encode() == git(source, "rev-parse", "HEAD").strip()
    assert git(source, "show", "-s", "--format=%P", "HEAD").strip() == before
    assert git(source, "show", "-s", "--format=%an|%ae|%cn|%ce|%s", "HEAD").strip() == (
        b"ForgePilot|forgepilot@localhost|ForgePilot|forgepilot@localhost|forgepilot(delivery): squash validated result")
    assert not git(source, "status", "--porcelain=v1", "--untracked-files=all")
    assert (source / "file.txt").read_bytes() == b"task\n"
    assert (source / "nested" / "new" / "result.txt").read_bytes() == b"new directory tree\n"
    assert manager.inspect_task(task) == snapshot


def test_conflict_stops_before_changing_main_and_survives_restart(delivery_repo):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    (source / "file.txt").write_bytes(b"target\n")
    git(source, "add", "--", "file.txt")
    git(source, "commit", "-qm", "target change")
    # Built-in union would silently resolve this conflict unless the manager
    # explicitly overrides it, even without any configured custom driver.
    (source / ".git" / "info" / "attributes").write_bytes(b"file.txt merge=union\n")
    before = git(source, "rev-parse", "HEAD")
    token = preview(manager, task)
    with pytest.raises(ForgePilotError, match="delivery_conflict"):
        manager.deliver(task, "squash", approved=True, token=token)
    assert git(source, "rev-parse", "HEAD") == before
    assert (source / "file.txt").read_bytes() == b"target\n"
    assert not git(source, "status", "--porcelain=v1")
    reopened = WorkspaceManager(manager.root)
    assert reopened._load(task)["status"] == "conflict"
    with pytest.raises(ForgePilotError, match="delivery_recovery"):
        reopened.delivery_preview(task, "main")


def test_target_branch_commit_drift_invalidates_approval(delivery_repo):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    token = preview(manager, task)
    (source / "other.txt").write_bytes(b"new target commit\n")
    git(source, "add", "--", "other.txt")
    git(source, "commit", "-qm", "target advanced")
    before = git(source, "rev-parse", "HEAD")
    with pytest.raises(ForgePilotError, match="delivery_changed"):
        manager.deliver(task, "squash", approved=True, token=token)
    assert git(source, "rev-parse", "HEAD") == before
    assert (source / "file.txt").read_bytes() == b"base\n"


def test_target_checked_out_branch_change_invalidates_approval(delivery_repo):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    token = preview(manager, task)
    git(source, "switch", "-qc", "other")
    with pytest.raises(ForgePilotError, match="delivery_changed"):
        manager.deliver(task, "squash", approved=True, token=token)
    assert git(source, "symbolic-ref", "--short", "HEAD").strip() == b"other"


def test_selected_target_must_be_checked_out(delivery_repo):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    git(source, "branch", "other")
    evidence = manager.delivery_preview(task, "other")
    assert evidence["target_checked_out"] is False
    with pytest.raises(ForgePilotError, match="delivery_target"):
        manager.deliver(task, "squash", approved=True, token=evidence["token"])


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked", "ignored", "assume_unchanged", "skip_worktree"])
def test_dirty_main_is_preserved(delivery_repo, dirty_kind):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    if dirty_kind in {"tracked", "assume_unchanged", "skip_worktree"}:
        dirty = source / "file.txt"
    else:
        dirty = source / "user.txt"
    if dirty_kind == "ignored":
        (source / ".git" / "info" / "exclude").write_text("user.txt\n", encoding="utf-8")
    if dirty_kind in {"assume_unchanged", "skip_worktree"}:
        flag = "--assume-unchanged" if dirty_kind == "assume_unchanged" else "--skip-worktree"
        git(source, "update-index", flag, "--", "file.txt")
    dirty.write_bytes(b"user data must survive\n")
    before = git(source, "rev-parse", "HEAD")
    evidence = manager.delivery_preview(task, "main")
    assert evidence["target_clean"] is False
    with pytest.raises(ForgePilotError, match="delivery_dirty"):
        manager.deliver(task, "squash", approved=True, token=evidence["token"])
    assert dirty.read_bytes() == b"user data must survive\n"
    assert git(source, "rev-parse", "HEAD") == before
    assert not git(source, "stash", "list")
    if dirty_kind in {"assume_unchanged", "skip_worktree"}:
        # Even with clean bytes, read-tree may honor skip flags instead of
        # updating a file. It must either deliver exact bytes or stop before CAS.
        dirty.write_bytes(b"base\n")
        evidence = manager.delivery_preview(task, "main")
        assert evidence["target_clean"] is True
        try:
            manager.deliver(task, "squash", approved=True, token=evidence["token"])
        except ForgePilotError as error:
            assert error.code in {"delivery_changed", "git_failed"}
            assert git(source, "rev-parse", "HEAD") == before
            assert manager._load(task)["status"] == "pending_delivery"
        else:
            assert dirty.read_bytes() == b"task\n"
            assert git(source, "show", "HEAD:file.txt") == b"task\n"


def test_changed_task_and_old_validation_are_rejected(delivery_repo):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    token = preview(manager, task)
    with manager.writer(task, expected=manager.inspect_task(task)) as worktree:
        (worktree / "file.txt").write_bytes(b"after validation\n")
    manager.checkpoint_commit(task, kind="repair", repair_round=1, expected=manager.inspect_task(task))
    with pytest.raises(ForgePilotError, match="delivery_changed"):
        manager.deliver(task, "squash", approved=True, token=token)
    evidence = manager.delivery_preview(task, "main")
    assert evidence["validated"] is False
    with pytest.raises(ForgePilotError, match="delivery_validation"):
        manager.deliver(task, "squash", approved=True, token=evidence["token"])
    assert (source / "file.txt").read_bytes() == b"base\n"


def test_binary_patch_exports_hash_and_applies_to_base(delivery_repo):
    manager, source, task = delivery_repo
    snapshot = change_and_validate(manager, task, b"\x00\xffbinary\r\n")
    before = git(source, "rev-parse", "HEAD")
    result = manager.deliver(task, "patch", approved=True, token=preview(manager, task))
    patch = artifact_bytes(manager, result["patch"])
    assert b"GIT binary patch" in patch
    assert sha256(patch).hexdigest() == result["patch"]["sha256"]
    assert len(patch) == result["patch"]["size"]
    git(source, "apply", "--check", "-", data=patch)
    assert git(source, "rev-parse", "HEAD") == before
    assert manager.inspect_task(task) == snapshot


def test_keep_branch_preserves_both_workspaces_and_consumes_token(delivery_repo):
    manager, source, task = delivery_repo
    snapshot = change_and_validate(manager, task)
    before = git(source, "rev-parse", "HEAD")
    token = preview(manager, task)
    result = manager.deliver(task, "keep_branch", approved=True, token=token)
    assert git(source, "rev-parse", result["branch"]).strip().decode() == snapshot.head_commit
    assert git(source, "rev-parse", "HEAD") == before
    assert manager.inspect_task(task) == snapshot
    with pytest.raises(ForgePilotError, match="delivery_approval"):
        manager.deliver(task, "keep_branch", approved=True, token=token)


def test_bundle_is_schema_valid_and_records_unavailable_lineage(delivery_repo):
    manager, source, task = delivery_repo
    snapshot = change_and_validate(manager, task)
    result = manager.deliver(task, "bundle", approved=True, token=preview(manager, task), bundle=bundle_for(snapshot))
    exported = DeliveryBundle.model_validate_json(artifact_bytes(manager, result["bundle"]))
    assert exported.commit == snapshot.head_commit
    assert exported.patch_path == result["patch"]["path"]
    metadata = json.loads(artifact_bytes(manager, result["manifest"]))
    assert metadata["lineage_verified"] is False
    assert metadata["bundle"]["sha256"] == sha256(artifact_bytes(manager, result["bundle"])).hexdigest()
    # A connected artifact store must resolve a same-task protocol lineage and
    # independently passing validation/review before claiming verification.
    from forgepilot.artifacts import (
        ChangeSet, ExecutionPlan, InMemoryArtifactStore, ReviewReport, TaskSpec,
        ValidationReport, create_artifact,
    )
    store = InMemoryArtifactStore()
    def put(content, *parents):
        artifact = create_artifact(task_id=task, producer="system", producer_node="delivery-test",
                                   content=content, parent_artifact_ids=tuple(p.artifact_id for p in parents))
        store.put(artifact)
        return artifact
    spec = put(TaskSpec.model_validate({
        "task_type": "maintenance", "title": "Update file", "objective": "Update file bytes.",
        "repository": {"base_ref": "HEAD", "toolchain_profile": "local"},
        "problem": {"observed_behavior": "Old file.", "expected_behavior": "Updated file.", "reproduction_steps": ["Read file."]},
        "acceptance_criteria": [{"id": "AC-1", "description": "File updated.", "verification": "test"}],
        "allowed_scope": {"path_patterns": ["file.txt"], "max_changed_files": 1}, "risk_level": "low",
    }))
    plan = put(ExecutionPlan.model_validate({
        "task_spec_artifact_id": spec.artifact_id, "summary": "Update file.",
        "steps": [{"step_id": "STEP-1", "owner": "implementer", "expected_files": ["file.txt"],
                   "action": "Update bytes.", "verification": ["test"], "risk": "Low.", "adds_tests": False,
                   "acceptance_criteria_ids": ["AC-1"]}],
    }), spec)
    changes = put(ChangeSet.model_validate({
        "execution_plan_artifact_id": plan.artifact_id, "summary": "Updated file.",
        "files": [{"path": "file.txt", "change_type": "modified", "unified_diff": "-base\n+task\n",
                   "before_sha256": sha256(b"base\n").hexdigest(), "after_sha256": sha256(b"task\n").hexdigest(),
                   "plan_step_ids": ["STEP-1"], "acceptance_criteria_ids": ["AC-1"]}],
    }), plan)
    validation = put(ValidationReport.model_validate({
        "toolchain_profile": "local", "commands": [{"kind": "test", "command_summary": "file byte check",
            "started_at": "2026-10-25T00:00:00Z", "finished_at": "2026-10-25T00:00:01Z", "exit_code": 0}],
        "status": "passed", "tests_passed": 1, "tests_failed": 0, "summary": "Passed.",
    }), changes)
    review = put(ReviewReport.model_validate({
        "verdict": "approve", "acceptance_criteria": [{"criterion_id": "AC-1", "status": "met", "evidence": "Bytes checked."}],
        "test_review": "Passed.", "security_review": "Paths checked.", "recommend_delivery": True, "summary": "Ready.",
    }), spec, changes, validation)
    manager.artifacts = store
    verified_bundle = bundle_for(snapshot).model_copy(update={
        "change_set_artifact_id": changes.artifact_id, "validation_report_artifact_id": validation.artifact_id,
        "review_report_artifact_id": review.artifact_id,
    })
    verified = manager.deliver(task, "bundle", approved=True, token=preview(manager, task), bundle=verified_bundle)
    assert verified["lineage_verified"] is True
    unrelated_validation = put(validation.content)
    unrelated_review = put(review.content, spec)
    unrelated_bundle = verified_bundle.model_copy(update={
        "validation_report_artifact_id": unrelated_validation.artifact_id,
        "review_report_artifact_id": unrelated_review.artifact_id,
    })
    with pytest.raises(ForgePilotError, match="lineage"):
        manager.deliver(task, "bundle", approved=True, token=preview(manager, task), bundle=unrelated_bundle)


def test_bundle_refuses_stale_commit_or_missing_parent_lineage(delivery_repo):
    manager, source, task = delivery_repo
    snapshot = change_and_validate(manager, task)
    invalid = bundle_for(snapshot).model_copy(update={"commit": "a" * 40})
    with pytest.raises(ForgePilotError, match="delivery_bundle"):
        manager.deliver(task, "bundle", approved=True, token=preview(manager, task), bundle=invalid)
    from forgepilot.artifacts.store import InMemoryArtifactStore
    manager.artifacts = InMemoryArtifactStore()
    with pytest.raises(ForgePilotError, match="lineage"):
        manager.deliver(task, "bundle", approved=True, token=preview(manager, task), bundle=bundle_for(snapshot))


def test_abandon_removes_only_registered_clean_worktree(delivery_repo):
    manager, source, task = delivery_repo
    record = manager.record(task)
    before = git(source, "rev-parse", "HEAD")
    result = manager.deliver(task, "abandon", approved=True, token=preview(manager, task))
    assert result["status"] == "abandoned"
    assert not Path(record["worktree_path"]).exists()
    assert source.is_dir()
    assert (source / "file.txt").read_bytes() == b"base\n"
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "show-ref", "--verify", "refs/heads/" + result["branch"])


def test_abandon_refuses_user_files_even_when_approved(delivery_repo):
    manager, source, task = delivery_repo
    token = preview(manager, task)
    user_file = Path(manager.record(task)["worktree_path"]) / "user.txt"
    user_file.write_bytes(b"preserve me")
    with pytest.raises(ForgePilotError, match="workspace_changed"):
        manager.deliver(task, "abandon", approved=True, token=token)
    assert user_file.read_bytes() == b"preserve me"
    assert source.exists()


def test_detached_main_without_branches_can_export_and_abandon(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "config", "user.name", "User")
    git(source, "config", "user.email", "user@example.invalid")
    git(source, "config", "core.autocrlf", "false")
    (source / "file.txt").write_bytes(b"base\n")
    git(source, "add", "--", "file.txt")
    git(source, "commit", "-qm", "base")
    git(source, "switch", "--detach", "HEAD")
    git(source, "branch", "-D", "main")
    assert not git(source, "for-each-ref", "refs/heads")
    before = git(source, "rev-parse", "HEAD")
    manager = WorkspaceManager(tmp_path / "data")
    manager.create(source, "detached-export", base_ref="HEAD")
    manager.approve_plan("detached-export", approved=True, expected=manager.inspect_task("detached-export"))
    change_and_validate(manager, "detached-export")
    evidence = manager.delivery_preview("detached-export")
    assert evidence["target_branch"] is None and not evidence["target_checked_out"]
    with pytest.raises(ForgePilotError, match="delivery_target"):
        manager.deliver("detached-export", "squash", approved=True, token=evidence["token"])
    result = manager.deliver("detached-export", "patch", approved=True, token=evidence["token"])
    git(source, "apply", "--check", "-", data=artifact_bytes(manager, result["patch"]))
    assert git(source, "rev-parse", "HEAD") == before
    assert (source / "file.txt").read_bytes() == b"base\n"
    manager.create(source, "detached-abandon", base_ref="HEAD")
    record = manager.record("detached-abandon")
    evidence = manager.delivery_preview("detached-abandon")
    abandoned = manager.deliver("detached-abandon", "abandon", approved=True, token=evidence["token"])
    assert abandoned["status"] == "abandoned" and abandoned["branch"] is None
    assert not Path(record["worktree_path"]).exists()
    assert source.is_dir() and git(source, "rev-parse", "HEAD") == before


def test_interrupted_squash_never_resumes_automatically(delivery_repo, monkeypatch):
    manager, source, task = delivery_repo
    change_and_validate(manager, task)
    token = preview(manager, task)
    before = git(source, "rev-parse", "HEAD")
    run = SafeGit.run
    class Interrupted(BaseException):
        pass
    def interrupt_publish(self, args, **kwargs):
        if args[:2] == ["update-ref", "refs/heads/main"]:
            raise Interrupted()
        return run(self, args, **kwargs)
    monkeypatch.setattr(SafeGit, "run", interrupt_publish)
    with pytest.raises(Interrupted):
        manager.deliver(task, "squash", approved=True, token=token)
    reopened = WorkspaceManager(manager.root)
    assert reopened._load(task)["status"] == "pending_delivery"
    assert git(source, "rev-parse", "HEAD") == before
    assert (source / "file.txt").read_bytes() == b"task\n"
    with pytest.raises(ForgePilotError, match="delivery_recovery"):
        reopened.delivery_preview(task, "main")
    with pytest.raises(ForgePilotError, match="never auto merge"):
        reopened.recover_task(task, approved=True, expected=reopened.inspect_task(task))
    manual = reopened.delivery_recovery_preview(task)
    assert manual["outcome"] == "manual_inspection"
    with pytest.raises(ForgePilotError, match="manual"):
        reopened.resolve_delivery(task, approved=True, token=manual["token"])
    assert git(source, "rev-parse", "HEAD") == before
    # These commands simulate the user's explicit manual inspection/decision;
    # the recovery API itself performs no Git mutation or automatic rollback.
    git(source, "reset", "--hard", before.decode().strip())
    restored = reopened.delivery_recovery_preview(task)
    assert restored["outcome"] == "restored"
    assert reopened.resolve_delivery(task, approved=True, token=restored["token"])["status"] == "active"
    assert git(source, "rev-parse", "HEAD") == before

    # A second interruption after CAS leaves the exact same durable intent but
    # an already complete clean target, which a fresh human decision may accept.
    monkeypatch.setattr(SafeGit, "run", run)
    save = reopened._save
    def interrupt_recording(state):
        if state["status"] == "delivered":
            raise Interrupted()
        return save(state)
    monkeypatch.setattr(reopened, "_save", interrupt_recording)
    with pytest.raises(Interrupted):
        reopened.deliver(task, "squash", approved=True, token=preview(reopened, task))
    final = WorkspaceManager(manager.root)
    completed = final.delivery_recovery_preview(task)
    assert completed["outcome"] == "completed"
    current_head = git(source, "rev-parse", "HEAD")
    result = final.resolve_delivery(task, approved=True, token=completed["token"])
    assert result["status"] == "delivered"
    assert git(source, "rev-parse", "HEAD") == current_head
    assert not git(source, "status", "--porcelain=v1")
