"""Real local Git acceptance tests for the WorkspaceManager trust boundary."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.orchestration.workspace import WorkspaceBackend
from forgepilot.workspace_manager import WorkspaceManager


def git(git_exe, root, *args, allowed=(0,)):
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0")
    result = subprocess.run([git_exe, "-C", str(root), *args], capture_output=True,
                            env=environment, check=False)
    assert result.returncode in allowed, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.fixture
def committed_repo(tmp_path, git_exe):
    root = tmp_path / "用户 source with spaces"
    root.mkdir()
    git(git_exe, root, "init", "-q", "-b", "main")
    git(git_exe, root, "config", "user.name", "Repository User")
    git(git_exe, root, "config", "user.email", "user@example.invalid")
    git(git_exe, root, "config", "core.autocrlf", "false")
    (root / "source.txt").write_bytes(b"original source\n")
    (root / "other.txt").write_bytes(b"unselected original\n")
    (root / "data.bin").write_bytes(b"\x00original\xff")
    git(git_exe, root, "add", "--all")
    git(git_exe, root, "commit", "-q", "-m", "user baseline")
    return root


@pytest.fixture
def manager(tmp_path, git_exe):
    return WorkspaceManager(tmp_path / "controlled data", git_executable=git_exe)


def create(manager, root, task_id="task-1", **kwargs):
    return manager.create(root, task_id, **kwargs)


def approve(manager, task_id="task-1", **kwargs):
    return manager.approve_plan(task_id, approved=True,
                                expected=manager.inspect_task(task_id), **kwargs)


def edit(manager, data=b"agent edit\n", task_id="task-1", path="source.txt"):
    with manager.writer(task_id, expected=manager.inspect_task(task_id)) as root:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return manager.inspect_task(task_id)


def checkpoint(manager, task_id="task-1", **kwargs):
    return manager.checkpoint_commit(task_id, kind="implementation",
                                      expected=manager.inspect_task(task_id), **kwargs)


def user_state(git_exe, root):
    return {
        "head": git(git_exe, root, "rev-parse", "HEAD"),
        "status": git(git_exe, root, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        "diff": git(git_exe, root, "diff", "--binary"),
        "cached": git(git_exe, root, "diff", "--cached", "--binary"),
        "index": (root / ".git" / "index").read_bytes(),
        "stash": git(git_exe, root, "show-ref", "--verify", "refs/stash", allowed=(0, 128)),
        "files": {str(path.relative_to(root)): path.read_bytes()
                  for path in root.rglob("*") if path.is_file() and ".git" not in path.parts},
    }


def test_creates_selected_committed_base_detached_before_plan_approval(manager, committed_repo, git_exe):
    selected = git(git_exe, committed_repo, "rev-parse", "HEAD").decode().strip()
    (committed_repo / "source.txt").write_bytes(b"later committed version\n")
    git(git_exe, committed_repo, "commit", "-q", "-am", "later user commit")
    state = create(manager, committed_repo, base_ref=selected)
    worktree = Path(state["worktree_path"])
    assert worktree.is_relative_to(manager.root / "worktrees" / state["repository_id"])
    assert worktree.name == "task-1"
    assert state["base_ref"] == selected and state["branch"] is None
    assert manager.inspect_task("task-1").head_commit == selected
    assert git(git_exe, worktree, "symbolic-ref", "-q", "HEAD", allowed=(1,)) == b""
    assert (worktree / "source.txt").read_bytes() == b"original source\n"
    assert not git(git_exe, committed_repo, "branch", "--list", "forgepilot/*")
    with pytest.raises(ForgePilotError):
        manager.approve_plan("task-1", approved=False, expected=manager.inspect_task("task-1"))
    approve(manager)
    branch = git(git_exe, worktree, "branch", "--show-current").decode().strip()
    assert branch.startswith("forgepilot/task-")
    assert git(git_exe, committed_repo, "branch", "--show-current").strip() == b"main"


def test_cancel_does_not_create_task_or_touch_user_repo(manager, committed_repo, git_exe):
    before = user_state(git_exe, committed_repo)
    assert create(manager, committed_repo, dirty_strategy="cancel") is None
    assert not (manager.root / "tasks" / "task-1.json").exists()
    assert not list((manager.root / "worktrees").rglob(".git"))
    assert user_state(git_exe, committed_repo) == before


@pytest.mark.parametrize("base", ["does-not-exist", "--upload-pack=evil", "HEAD~1", "HEAD^{tree}", "../main"])
def test_invalid_or_noncommit_base_never_creates_worktree(manager, committed_repo, base):
    with pytest.raises(ForgePilotError):
        create(manager, committed_repo, base_ref=base)
    assert not list((manager.root / "worktrees").rglob(".git"))


def test_unborn_repository_has_no_accepted_committed_base(manager, tmp_path, git_exe):
    root = tmp_path / "unborn"
    root.mkdir()
    git(git_exe, root, "init", "-q")
    with pytest.raises(ForgePilotError):
        create(manager, root)
    assert not list((manager.root / "worktrees").rglob(".git"))


def test_dirty_head_strategy_preserves_staged_unstaged_untracked_and_stash(manager, committed_repo, git_exe):
    (committed_repo / "source.txt").write_bytes(b"staged user bytes\n")
    git(git_exe, committed_repo, "add", "source.txt")
    (committed_repo / "source.txt").write_bytes(b"unstaged user bytes\n")
    (committed_repo / "notes.txt").write_bytes(b"private untracked user data\n")
    before = user_state(git_exe, committed_repo)
    state = create(manager, committed_repo)
    assert user_state(git_exe, committed_repo) == before
    assert (Path(state["worktree_path"]) / "source.txt").read_bytes() == b"original source\n"
    assert not (Path(state["worktree_path"]) / "notes.txt").exists()


def test_baseline_artifact_hash_selected_final_bytes_and_binary_copy(manager, committed_repo, git_exe):
    (committed_repo / "source.txt").write_bytes(b"selected staged bytes\n")
    git(git_exe, committed_repo, "add", "source.txt")
    (committed_repo / "source.txt").write_bytes(b"selected final working bytes\n")
    (committed_repo / "other.txt").write_bytes(b"must remain outside baseline\n")
    (committed_repo / "data.bin").write_bytes(bytes(range(256)))
    (committed_repo / "nested").mkdir()
    (committed_repo / "nested/new.txt").write_bytes(b"selected untracked\n")
    before = user_state(git_exe, committed_repo)
    state = create(manager, committed_repo, dirty_strategy="copy_diff")
    snapshot = manager.inspect_task("task-1")
    artifact = manager.prepare_baseline("task-1", ["source.txt", "data.bin", "nested/new.txt"])
    patch = Path(artifact["path"]).read_bytes()
    assert artifact["sha256"] == sha256(patch).hexdigest()
    assert artifact["size"] == len(patch)
    assert b"GIT binary patch" in patch and b"other.txt" not in patch
    with pytest.raises(ForgePilotError):
        manager.apply_baseline("task-1", artifact["sha256"], approved=False, expected=snapshot)
    assert manager.inspect_task("task-1") == snapshot
    result = manager.apply_baseline("task-1", artifact["sha256"], approved=True, expected=snapshot)
    root = Path(state["worktree_path"])
    for name in ("source.txt", "data.bin", "nested/new.txt"):
        assert (root / name).read_bytes() == (committed_repo / name).read_bytes()
    assert (root / "other.txt").read_bytes() == b"unselected original\n"
    assert result.head_commit == snapshot.head_commit
    assert user_state(git_exe, committed_repo) == before


def test_baseline_patch_tampering_is_detected_before_any_git_apply(manager, committed_repo):
    (committed_repo / "source.txt").write_bytes(b"user diff\n")
    state = create(manager, committed_repo)
    before = manager.inspect_task("task-1")
    artifact = manager.prepare_baseline("task-1", ["source.txt"])
    Path(artifact["path"]).write_bytes(b"forged patch contents")
    with pytest.raises(ForgePilotError):
        manager.apply_baseline("task-1", artifact["sha256"], approved=True, expected=before)
    assert manager.inspect_task("task-1") == before
    assert (Path(state["worktree_path"]) / "source.txt").read_bytes() == b"original source\n"


def test_baseline_conflict_stops_without_modifying_either_working_tree(manager, committed_repo, git_exe):
    old_base = git(git_exe, committed_repo, "rev-parse", "HEAD").decode().strip()
    (committed_repo / "source.txt").write_bytes(b"new committed baseline\n")
    git(git_exe, committed_repo, "commit", "-q", "-am", "different source baseline")
    (committed_repo / "source.txt").write_bytes(b"user change on different baseline\n")
    main_before = user_state(git_exe, committed_repo)
    state = create(manager, committed_repo, base_ref=old_base)
    observed = manager.inspect_task("task-1")
    artifact = manager.prepare_baseline("task-1", ["source.txt"])
    with pytest.raises(ForgePilotError, match="conflict"):
        manager.apply_baseline("task-1", artifact["sha256"], approved=True, expected=observed)
    assert (Path(state["worktree_path"]) / "source.txt").read_bytes() == b"original source\n"
    assert user_state(git_exe, committed_repo) == main_before
    assert json.loads((manager.root / "tasks" / "task-1.json").read_bytes())["status"] == "conflict"


@pytest.mark.parametrize("path", ["../source.txt", ".git/config", "source.txt:stream", r"\\server\share\file", r"C:\\outside.txt"])
def test_baseline_refuses_windows_escape_and_git_paths(manager, committed_repo, path):
    create(manager, committed_repo)
    with pytest.raises(ForgePilotError):
        manager.prepare_baseline("task-1", [path])


def test_writer_requires_plan_approval(manager, committed_repo):
    create(manager, committed_repo)
    snapshot = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        with manager.writer("task-1", expected=snapshot):
            pytest.fail("Writer must not be entered before approval")


def test_one_repository_lease_excludes_other_tasks_and_manager_instances(manager, committed_repo, git_exe):
    create(manager, committed_repo)
    create(manager, committed_repo, task_id="task-2")
    approve(manager)
    approve(manager, "task-2")
    second = WorkspaceManager(manager.root, git_executable=git_exe)
    with manager.writer("task-1", expected=manager.inspect_task("task-1")) as root:
        (root / "source.txt").write_bytes(b"owned write\n")
        with pytest.raises(ForgePilotError):
            with second.writer("task-2", expected=second.inspect_task("task-2")):
                pytest.fail("A second writer entered the repository")
        with pytest.raises(ForgePilotError):
            second.checkpoint_commit("task-1", kind="implementation", expected=manager.inspect_task("task-1"))
    assert checkpoint(manager).head_commit
    with second.writer("task-2", expected=second.inspect_task("task-2")) as root:
        (root / "source.txt").write_bytes(b"next writer after release\n")


@pytest.mark.parametrize("kind,round_number,message", [
    ("implementation", None, "forgepilot(checkpoint): implementation"),
    ("plan_approved", None, "forgepilot(checkpoint): approved execution plan"),
    ("validated", None, "forgepilot(checkpoint): validated result"),
    ("repair", 2, "forgepilot(checkpoint): repair round 2"),
])
def test_checkpoint_has_fixed_identity_and_fixed_message(manager, committed_repo, git_exe, kind, round_number, message):
    state = create(manager, committed_repo)
    approve(manager)
    before = edit(manager)
    after = manager.checkpoint_commit("task-1", kind=kind, repair_round=round_number, expected=before)
    assert after.head_commit != before.head_commit
    assert after.file_hashes == before.file_hashes
    raw = git(git_exe, Path(state["worktree_path"]), "show", "--no-patch",
              "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", after.head_commit).decode().rstrip("\n")
    assert raw == "\0".join(("ForgePilot", "forgepilot@localhost", "ForgePilot", "forgepilot@localhost", message))
    assert git(git_exe, committed_repo, "show", "HEAD:source.txt") == b"original source\n"


@pytest.mark.parametrize("kind,round_number", [("custom commit text", None), ("repair", 0), ("repair", 4), ("repair", True)])
def test_checkpoint_policy_rejects_custom_messages_and_invalid_repair_rounds(manager, committed_repo, kind, round_number):
    create(manager, committed_repo)
    approve(manager)
    before = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        manager.checkpoint_commit("task-1", kind=kind, repair_round=round_number, expected=before)
    assert manager.inspect_task("task-1") == before


def test_plan_checkpoint_cannot_create_branch_without_user_approval(manager, committed_repo, git_exe):
    create(manager, committed_repo)
    before = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        manager.checkpoint_commit("task-1", kind="plan_approved", expected=before)
    assert not git(git_exe, committed_repo, "branch", "--list", "forgepilot/*")


def test_external_file_edits_are_not_adopted_even_with_fresh_snapshot(manager, committed_repo):
    state = create(manager, committed_repo)
    approve(manager)
    (Path(state["worktree_path"]) / "source.txt").write_bytes(b"external process edit\n")
    observed = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        manager.checkpoint_commit("task-1", kind="implementation", expected=observed)
    with pytest.raises(ForgePilotError):
        manager.recover_task("task-1", approved=True, expected=observed)
    assert manager.inspect_task("task-1") == observed


def test_external_index_only_drift_is_rejected_without_overwriting_staged_bytes(manager, committed_repo, git_exe):
    from forgepilot.workspace_safety import manifest

    state = create(manager, committed_repo)
    approve(manager)
    worktree = Path(state["worktree_path"])
    source = worktree / "source.txt"
    original = source.read_bytes()
    before = manager.inspect_task("task-1")
    staged = b"external staged bytes invisible in the working file\n"
    source.write_bytes(staged)
    git(git_exe, worktree, "add", "--", "source.txt")
    source.write_bytes(original)
    cached_diff = git(git_exe, worktree, "diff", "--cached", "--binary")
    assert cached_diff
    assert git(git_exe, worktree, "show", ":source.txt") == staged
    assert git(git_exe, worktree, "rev-parse", "HEAD").decode().strip() == before.head_commit
    assert manifest(worktree) == dict(before.file_hashes)

    with pytest.raises(ForgePilotError):
        manager.checkpoint_commit("task-1", kind="implementation", expected=before)

    assert source.read_bytes() == original
    assert git(git_exe, worktree, "rev-parse", "HEAD").decode().strip() == before.head_commit
    assert git(git_exe, worktree, "show", ":source.txt") == staged
    assert git(git_exe, worktree, "diff", "--cached", "--binary") == cached_diff


def test_external_head_drift_is_rejected_even_with_fresh_snapshot(manager, committed_repo, git_exe):
    state = create(manager, committed_repo)
    approve(manager)
    root = Path(state["worktree_path"])
    git(git_exe, root, "commit", "--allow-empty", "-q", "-m", "external commit")
    observed = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        manager.checkpoint_commit("task-1", kind="implementation", expected=observed)
    assert manager.inspect_task("task-1") == observed


def test_writer_cannot_change_head_or_commit(manager, committed_repo, git_exe):
    create(manager, committed_repo)
    approve(manager)
    with pytest.raises(ForgePilotError):
        with manager.writer("task-1", expected=manager.inspect_task("task-1")) as root:
            git(git_exe, root, "commit", "--allow-empty", "-q", "-m", "unauthorized writer commit")


def test_writer_enforces_approved_file_scope(manager, committed_repo):
    state = create(manager, committed_repo)
    approve(manager, approved_paths=["source.txt"])
    with pytest.raises(ForgePilotError):
        edit(manager, path="other.txt")
    assert json.loads((manager.root / "tasks" / "task-1.json").read_bytes())["status"] == "interrupted_writer"
    assert (Path(state["worktree_path"]) / "other.txt").read_bytes() == b"agent edit\n"


def test_git_attribute_normalization_cannot_publish_different_checkpoint_bytes(manager, committed_repo, git_exe):
    (committed_repo / ".gitattributes").write_bytes(b"source.txt text eol=lf\n")
    git(git_exe, committed_repo, "add", ".gitattributes")
    git(git_exe, committed_repo, "commit", "-q", "-m", "explicit EOL policy")
    create(manager, committed_repo)
    approve(manager)
    observed = edit(manager, b"writer CRLF output\r\n")
    with pytest.raises(ForgePilotError, match="checkpoint_integrity"):
        checkpoint(manager)
    assert manager.inspect_task("task-1") == observed


def test_real_workspace_backend_checkpoint_restart_and_retry_is_idempotent(manager, committed_repo, git_exe):
    create(manager, committed_repo)
    approve(manager)
    before = edit(manager)
    record = manager.record("task-1")
    assert isinstance(manager, WorkspaceBackend)
    after = manager.checkpoint(record, "durable-checkpoint", before)
    reopened = WorkspaceManager(manager.root, git_executable=git_exe)
    assert reopened.inspect(record) == after
    assert reopened.checkpoint(record, "durable-checkpoint", before) == after
    assert reopened.checkpoint(record, "durable-checkpoint", after) == after
    reopened.verify_checkpoint(record, "durable-checkpoint", after.head_commit, after.file_hashes)
    assert reopened.record("task-1")["worktree_path"] == record["worktree_path"]
    for bad in ({**record, "worktree_path": str(committed_repo)}, {**record, "base_ref": "b" * 40}):
        with pytest.raises(ForgePilotError):
            reopened.inspect(bad)


def test_reused_checkpoint_id_cannot_capture_later_changes(manager, committed_repo):
    create(manager, committed_repo)
    approve(manager)
    edit(manager)
    checkpoint(manager, checkpoint_id="same-checkpoint")
    later = edit(manager, b"later changes\n")
    with pytest.raises(ForgePilotError):
        checkpoint(manager, checkpoint_id="same-checkpoint")
    assert manager.inspect_task("task-1") == later


def test_repeated_checkpoint_still_checks_expected_workspace_snapshot(manager, committed_repo):
    create(manager, committed_repo)
    approve(manager)
    before = edit(manager)
    after = checkpoint(manager, checkpoint_id="bound-checkpoint")
    for wrong in (replace(before, task_id="another-task"),
                  replace(before, workspace_id="another-workspace"),
                  replace(before, file_hashes={"source.txt": sha256(b"unrelated").hexdigest()})):
        with pytest.raises(ForgePilotError):
            manager.checkpoint_commit("task-1", kind="implementation", expected=wrong,
                                      checkpoint_id="bound-checkpoint")
    assert manager.inspect_task("task-1") == after


def test_interrupted_checkpoint_requires_explicit_recovery_and_never_duplicates_commit(manager, committed_repo, git_exe, monkeypatch):
    create(manager, committed_repo)
    approve(manager)
    before = edit(manager)

    class ProcessDeath(BaseException):
        pass

    def crash_before_publish(state):
        raise ProcessDeath("HEAD updated; state not yet published")

    monkeypatch.setattr(manager, "_publish_checkpoint", crash_before_publish)
    with pytest.raises(ProcessDeath):
        manager.checkpoint_commit("task-1", kind="implementation", expected=before,
                                  checkpoint_id="interrupted-checkpoint")
    reopened = WorkspaceManager(manager.root, git_executable=git_exe)
    observed = reopened.inspect_task("task-1")
    assert observed.head_commit != before.head_commit
    assert observed.file_hashes == before.file_hashes
    with pytest.raises(ForgePilotError):
        reopened.recover_task("task-1", approved=False, expected=observed)
    recovered = reopened.recover_task("task-1", approved=True, expected=observed)
    assert recovered == observed
    assert reopened.checkpoint_commit("task-1", kind="implementation", expected=before,
                                      checkpoint_id="interrupted-checkpoint") == recovered
    record = reopened.record("task-1")
    reopened.verify_checkpoint(record, "interrupted-checkpoint", recovered.head_commit, recovered.file_hashes)


def test_interrupted_owned_writer_restores_verified_checkpoint_after_restart(manager, committed_repo, git_exe):
    create(manager, committed_repo)
    approve(manager)
    saved = checkpoint(manager, checkpoint_id="safe-checkpoint")
    with pytest.raises(RuntimeError, match="simulated interruption"):
        with manager.writer("task-1", expected=saved) as root:
            (root / "source.txt").write_bytes(b"partial output\n")
            (root / "partial.txt").write_bytes(b"new partial output\n")
            raise RuntimeError("simulated interruption")
    reopened = WorkspaceManager(manager.root, git_executable=git_exe)
    observed = reopened.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        reopened.recover_task("task-1", approved=True, expected=observed)
    restored = reopened.restore(reopened.record("task-1"), "safe-checkpoint", expected=observed)
    assert restored == saved
    assert not (Path(reopened.record("task-1")["worktree_path"]) / "partial.txt").exists()


def test_restore_refuses_user_file_added_after_interrupted_writer(manager, committed_repo):
    state = create(manager, committed_repo)
    approve(manager)
    saved = checkpoint(manager, checkpoint_id="safe-checkpoint")
    with pytest.raises(RuntimeError):
        with manager.writer("task-1", expected=saved) as root:
            (root / "source.txt").write_bytes(b"partial writer output\n")
            raise RuntimeError("interrupted writer")
    user_file = Path(state["worktree_path"]) / "user-added-after-interruption.txt"
    user_file.write_bytes(b"unowned user data\n")
    observed = manager.inspect_task("task-1")
    with pytest.raises(ForgePilotError):
        manager.restore(manager.record("task-1"), "safe-checkpoint", expected=observed)
    assert user_file.read_bytes() == b"unowned user data\n"
    assert (Path(state["worktree_path"]) / "source.txt").read_bytes() == b"partial writer output\n"


def test_repeated_restore_finishes_only_reviewed_additions_after_git_reset(manager, committed_repo, monkeypatch):
    from forgepilot.workspace_safety import Guard
    state = create(manager, committed_repo)
    approve(manager)
    saved = checkpoint(manager, checkpoint_id="safe-checkpoint")
    with pytest.raises(RuntimeError):
        with manager.writer("task-1", expected=saved) as root:
            (root / "source.txt").write_bytes(b"partial tracked edit")
            (root / "partial.txt").write_bytes(b"partial added file")
            raise RuntimeError("writer paused")
    observed = manager.inspect_task("task-1")
    record = manager.record("task-1")
    def interrupt_cleanup(*_):
        raise RuntimeError("interrupted after reset")
    with monkeypatch.context() as crash:
        crash.setattr(Guard, "unlink", interrupt_cleanup)
        with pytest.raises(RuntimeError, match="interrupted after reset"):
            manager.restore(record, "safe-checkpoint", expected=observed)
    root = Path(state["worktree_path"])
    assert (root / "source.txt").read_bytes() == b"original source\n"
    (root / "unreviewed.txt").write_bytes(b"user work")
    with pytest.raises(ForgePilotError, match="workspace_changed"):
        manager.restore(record, "safe-checkpoint", expected=observed)
    assert (root / "partial.txt").exists()
    assert (root / "unreviewed.txt").read_bytes() == b"user work"
    (root / "unreviewed.txt").unlink()
    assert manager.restore(record, "safe-checkpoint", expected=observed) == saved
    assert not (root / "partial.txt").exists()


def test_cleanup_removes_only_registered_clean_worktree_and_keeps_branch(manager, committed_repo, git_exe):
    state = create(manager, committed_repo)
    approve(manager)
    edit(manager)
    saved = checkpoint(manager)
    main_before = user_state(git_exe, committed_repo)
    sibling = manager.root / "worktrees" / state["repository_id"] / "user-owned-sibling"
    sibling.mkdir()
    (sibling / "notes.txt").write_bytes(b"must survive\n")
    result = manager.cleanup("task-1", approved=True, expected=saved)
    assert result["status"] == "abandoned"
    assert not Path(state["worktree_path"]).exists()
    assert (sibling / "notes.txt").read_bytes() == b"must survive\n"
    assert user_state(git_exe, committed_repo) == main_before
    assert git(git_exe, committed_repo, "branch", "--list", "forgepilot/task-*")


def test_cleanup_removes_known_empty_directory_after_owned_file_deletion(manager, committed_repo, git_exe):
    (committed_repo / "nested").mkdir()
    (committed_repo / "nested/owned.txt").write_bytes(b"owned baseline\n")
    git(git_exe, committed_repo, "add", "nested/owned.txt")
    git(git_exe, committed_repo, "commit", "-q", "-m", "nested baseline")
    state = create(manager, committed_repo)
    approve(manager)
    with manager.writer("task-1", expected=manager.inspect_task("task-1")) as root:
        (root / "nested/owned.txt").unlink()
    saved = checkpoint(manager)
    assert (Path(state["worktree_path"]) / "nested").is_dir()
    manager.cleanup("task-1", approved=True, expected=saved)
    assert not Path(state["worktree_path"]).exists()
    assert (committed_repo / "nested/owned.txt").read_bytes() == b"owned baseline\n"


def test_plan_approval_does_not_adopt_concurrently_added_user_file(manager, committed_repo, monkeypatch):
    state = create(manager, committed_repo)
    expected = manager.inspect_task("task-1")
    branch = manager._branch
    user_file = Path(state["worktree_path"]) / "user-file"
    def changed(current):
        branch(current)
        user_file.write_bytes(b"concurrent user data\n")
    monkeypatch.setattr(manager, "_branch", changed)
    with pytest.raises(ForgePilotError, match="workspace_changed"):
        manager.approve_plan("task-1", approved=True, expected=expected)
    with pytest.raises(ForgePilotError, match="workspace_changed"):
        manager.cleanup("task-1", approved=True, expected=manager.inspect_task("task-1"))
    assert user_file.read_bytes() == b"concurrent user data\n"


@pytest.mark.parametrize("ignored", [False, True])
def test_cleanup_refuses_unknown_user_files_including_ignored(manager, committed_repo, ignored):
    state = create(manager, committed_repo)
    root = Path(state["worktree_path"])
    if ignored:
        (committed_repo / ".git" / "info" / "exclude").write_text("user.tmp\n", encoding="utf-8")
    (root / "user.tmp").write_bytes(b"unknown user data\n")
    with pytest.raises(ForgePilotError):
        manager.cleanup("task-1", approved=True, expected=manager.inspect_task("task-1"))
    assert (root / "user.tmp").read_bytes() == b"unknown user data\n"
    assert (committed_repo / "source.txt").read_bytes() == b"original source\n"


def test_cleanup_refuses_replaced_worktree_root(manager, committed_repo):
    state = create(manager, committed_repo)
    expected = manager.inspect_task("task-1")
    root = Path(state["worktree_path"])
    moved = root.with_name("moved-original-worktree")
    root.rename(moved)
    root.mkdir()
    (root / "user.txt").write_bytes(b"replacement directory user data\n")
    with pytest.raises(ForgePilotError):
        manager.cleanup("task-1", approved=True, expected=expected)
    assert (root / "user.txt").read_bytes() == b"replacement directory user data\n"
    assert (moved / "source.txt").exists()
    assert (committed_repo / ".git").is_dir()


@pytest.mark.parametrize("replace_root", [False, True])
def test_cleanup_never_follows_windows_junction_or_symlink(manager, committed_repo, tmp_path, replace_root):
    state = create(manager, committed_repo)
    expected = manager.inspect_task("task-1")
    root = Path(state["worktree_path"])
    outside = tmp_path / "unrelated user directory"
    outside.mkdir()
    (outside / "user.txt").write_bytes(b"untouched outside data\n")
    if replace_root:
        root.rename(root.with_name("original-owned-worktree"))
        link = root
    else:
        link = root / "junction"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ForgePilotError):
        manager.cleanup("task-1", approved=True, expected=expected)
    assert link.exists()
    assert (outside / "user.txt").read_bytes() == b"untouched outside data\n"
    assert sorted(path.name for path in outside.iterdir()) == ["user.txt"]
    assert (committed_repo / ".git").is_dir()
