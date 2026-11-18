"""Manager Git primitives run against real local repositories, without remotes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.git import SafeGit, _run_sync
from forgepilot.workspace_safety import Guard, manifest, remove_worktree_files, remove_worktree_registration


def git(git_exe, root, *args):
    return subprocess.run([git_exe, "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def committed_repo(tmp_path, git_exe):
    root = tmp_path / "真实 Git 项目 with spaces"
    root.mkdir()
    git(git_exe, root, "init", "-q")
    git(git_exe, root, "config", "user.name", "User")
    git(git_exe, root, "config", "user.email", "user@example.test")
    (root / "source.txt").write_bytes(b"base\n")
    git(git_exe, root, "add", "source.txt")
    git(git_exe, root, "commit", "-qm", "initial user commit")
    return root


def test_detached_worktree_and_marker_revalidation(committed_repo, tmp_path, git_exe):
    root = committed_repo
    manager = SafeGit(root, git_exe)
    head = manager.run(["rev-parse", "HEAD"]).stdout.strip().decode()
    controlled = Guard(tmp_path / "data", create=True)
    target = controlled.path("task", missing=True)
    with controlled.protect():
        manager.run(["worktree", "add", "--detach", str(target), head])
    workspace = SafeGit(target, git_exe)
    assert workspace.run(["rev-parse", "HEAD"]).stdout.strip().decode() == head
    assert workspace.run(["symbolic-ref", "--quiet", "HEAD"], allowed=(0, 1)).returncode == 1
    pointer = Guard(target).read_marker()
    assert pointer.startswith(b"gitdir: ")
    with (target / ".git").open("r+b") as stream:
        stream.write(pointer + b"\n")
        stream.truncate()
    with pytest.raises(ForgePilotError, match="pointer changed"):
        workspace.run(["status", "--porcelain"])
    with (target / ".git").open("r+b") as stream:
        stream.write(pointer)
        stream.truncate()
    with controlled.protect():
        manager.run(["worktree", "remove", str(target)])
    assert root.is_dir() and not target.exists()


def test_filters_hooks_signing_and_inherited_environment_are_disabled(committed_repo, tmp_path, git_exe, monkeypatch):
    root = committed_repo
    sentinel = root / "SHOULD-NOT-EXIST"
    command = '"' + sys.executable.replace("\\", "/") + '" -c "open(\'SHOULD-NOT-EXIST\',\'w\').write(\'bad\')"'
    git(git_exe, root, "config", "filter.evil.clean", command)
    git(git_exe, root, "config", "filter.evil.smudge", command)
    git(git_exe, root, "config", "filter.evil.required", "true")
    git(git_exe, root, "config", "merge.evil.driver", command)
    git(git_exe, root, "config", "commit.gpgSign", "true")
    git(git_exe, root, "config", "gpg.program", command)
    (root / ".gitattributes").write_bytes(b"source.txt filter=evil merge=evil\n")
    (root / "source.txt").write_bytes(b"updated\n")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(root / ".git/hooks"))
    safe = SafeGit(root, git_exe)
    safe.run(["add", "--", "source.txt", ".gitattributes"])
    tree = safe.run(["write-tree"]).stdout.strip().decode()
    parent = safe.run(["rev-parse", "HEAD"]).stdout.strip().decode()
    identity = {"GIT_AUTHOR_NAME": "ForgePilot", "GIT_AUTHOR_EMAIL": "forgepilot@localhost",
                "GIT_COMMITTER_NAME": "ForgePilot", "GIT_COMMITTER_EMAIL": "forgepilot@localhost"}
    commit = safe.run(["commit-tree", tree, "-p", parent], data=b"forgepilot(checkpoint): implementation\n",
                      env=identity).stdout.strip().decode()
    assert safe.run(["show", "-s", "--format=%an <%ae>", commit]).stdout.strip() == b"ForgePilot <forgepilot@localhost>"
    assert not sentinel.exists()
    assert safe.run(["rev-parse", "HEAD"]).stdout.strip().decode() == parent


def test_git_metadata_locks_and_remote_commands_are_refused(committed_repo, git_exe):
    safe = SafeGit(committed_repo, git_exe)
    lock = committed_repo / ".git/index.lock"
    lock.write_bytes(b"external or stale")
    with pytest.raises(ForgePilotError, match="workspace_git_busy"):
        safe.run(["status", "--porcelain"])
    assert lock.read_bytes() == b"external or stale"
    lock.unlink()
    with pytest.raises(ForgePilotError, match="git_denied"):
        safe.run(["push"])
    with pytest.raises(ForgePilotError, match="git_denied"):
        safe.run(["commit", "-m", "model message"])
    with pytest.raises(ForgePilotError, match="git_denied"):
        safe.run(["status"], env={"GIT_CONFIG_COUNT": "1"})
    with pytest.raises(ForgePilotError, match="git_denied"):
        safe.run(["write-tree"], env={"GIT_INDEX_FILE": str(committed_repo / ".git/index")})


def test_private_index_never_changes_source_index(committed_repo, tmp_path, git_exe):
    safe = SafeGit(committed_repo, git_exe)
    before = (committed_repo / ".git/index").read_bytes()
    (committed_repo / "source.txt").write_bytes(b"private index modification\n")
    index = tmp_path / "private.index"
    env = {"GIT_INDEX_FILE": str(index)}
    safe.run(["read-tree", "HEAD"], env=env)
    safe.run(["add", "--", "source.txt"], env=env)
    assert safe.run(["diff", "--cached", "--binary"], env=env).stdout
    assert (committed_repo / ".git/index").read_bytes() == before
    assert index.is_file()


def test_synchronous_git_runner_timeout_and_output_limit(tmp_path):
    result = _run_sync([sys.executable, "-c", "print('x'*1000000)"], tmp_path, output_limit=100)
    assert result.truncated and len(result.stdout) <= 100
    with pytest.raises(ForgePilotError, match="tool_timeout"):
        _run_sync([sys.executable, "-c", "import time;time.sleep(10)"], tmp_path, timeout=0.1)


def test_git_config_includes_and_external_object_store_are_refused(committed_repo, tmp_path, git_exe):
    config = committed_repo / ".git/config"
    original = config.read_bytes()
    with config.open("ab") as stream:
        stream.write(b'\n[includeIf "gitdir:**"]\n\tpath = //unreachable-server/share/config\n')
    with pytest.raises(ForgePilotError, match="git_config"):
        SafeGit(committed_repo, git_exe)
    config.write_bytes(original)
    (committed_repo / ".git/objects/info/alternates").write_bytes(str(tmp_path).encode())
    with pytest.raises(ForgePilotError, match="git_config"):
        SafeGit(committed_repo, git_exe)


@pytest.mark.skipif(os.name != "nt", reason="Windows exact handle disposition")
def test_exact_worktree_and_admin_cleanup_preserves_recreated_user_directory(committed_repo, tmp_path, git_exe):
    import hashlib
    safe = SafeGit(committed_repo, git_exe)
    target = tmp_path / "controlled-task"
    safe.run(["worktree", "add", "--detach", str(target), "HEAD"])
    task = SafeGit(target, git_exe)
    files = manifest(target)
    marker = hashlib.sha256(Guard(target).read_marker()).hexdigest()
    root_identity = (target.stat().st_dev, target.stat().st_ino)
    admin = task.git_dir
    admin_files = manifest(admin)
    admin_identity = (admin.stat().st_dev, admin.stat().st_ino)
    remove_worktree_files(target, files, marker, expected_root_identity=root_identity)
    # This was unsafe with git worktree remove(old_path): the user may create
    # new content at the old name once the verified directory was removed.
    target.mkdir()
    (target / "new-user-file").write_bytes(b"must survive")
    remove_worktree_registration(admin, task.common_dir, target, admin_files,
                                 expected_root_identity=admin_identity)
    assert not admin.exists()
    assert (target / "new-user-file").read_bytes() == b"must survive"
    assert str(target).replace("\\", "/").encode() not in safe.run(["worktree", "list", "--porcelain"]).stdout
    remove_worktree_registration(admin, task.common_dir, target, admin_files,
                                 expected_root_identity=admin_identity)


def test_worktree_configuration_cannot_bypass_driver_inspection(committed_repo, git_exe):
    root = committed_repo
    config = root / ".git/config"
    original = config.read_bytes()
    git(git_exe, root, "config", "extensions.worktreeConfig", "true")
    worktree_config = root / ".git/config.worktree"
    worktree_config.write_bytes(b'[merge "evil"]\n driver = SHOULD_NOT_RUN\n')
    # This is why a command-line extension=false flag is insufficient: Git has
    # already read this driver when it applies the command-line override.
    assert git(git_exe, root, "-c", "extensions.worktreeConfig=false", "config", "--get", "merge.evil.driver").strip() == b"SHOULD_NOT_RUN"
    with pytest.raises(ForgePilotError, match="git_config"):
        SafeGit(root, git_exe)
    config.write_bytes(original)
    with pytest.raises(ForgePilotError, match="git_config"):
        SafeGit(root, git_exe)
    worktree_config.unlink()
    with config.open("ab") as stream:
        stream.write(b"\n[extensions]\n worktreeConfig = false\n")
    with pytest.raises(ForgePilotError, match="git_config"):
        SafeGit(root, git_exe)


def test_replacement_objects_and_grafts_cannot_redefine_verified_commits(committed_repo, git_exe):
    from forgepilot.process import safe_environment
    root = committed_repo
    original = git(git_exe, root, "rev-parse", "HEAD").strip().decode()
    original_tree = git(git_exe, root, "ls-tree", "-r", original)
    (root / "source.txt").write_bytes(b"replacement bytes\n")
    git(git_exe, root, "add", "source.txt")
    tree = git(git_exe, root, "write-tree").strip().decode()
    replacement = subprocess.run([git_exe, "-C", str(root), "commit-tree", tree],
                                 input=b"replacement object fixture\n", capture_output=True, check=True).stdout.strip().decode()
    git(git_exe, root, "replace", original, replacement)
    assert git(git_exe, root, "ls-tree", "-r", original) != original_tree
    safe = SafeGit(root, git_exe)
    assert safe.run(["ls-tree", "-r", original]).stdout == original_tree
    env = safe_environment()
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_GRAFT_FILE"] == os.devnull
    (root / ".git/info/grafts").write_bytes(original.encode() + b"\n")
    with pytest.raises(ForgePilotError, match="grafts"):
        safe.run(["ls-tree", "-r", original])
