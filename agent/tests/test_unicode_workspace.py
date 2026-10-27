"""Real Windows Unicode names must not merge approval or Git administration identities."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.git import SafeGit
from forgepilot.path_identity import path_key, path_within, same_path
from forgepilot.workspace_manager import WorkspaceManager
from forgepilot.workspace_safety import Guard, _same_path, manifest
from test_workspace_manager import git


_PAIRS = [("İ", "i\u0307"), ("ß", "ss")]


def _file_id(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def _committed(root, git_exe, *, files=None):
    root.mkdir()
    git(git_exe, root, "init", "-q", "-b", "main")
    git(git_exe, root, "config", "user.name", "Unicode Fixture")
    git(git_exe, root, "config", "user.email", "unicode@example.invalid")
    git(git_exe, root, "config", "core.autocrlf", "false")
    for name, data in (files or {"source.txt": b"baseline source\n"}).items():
        (root / name).write_bytes(data)
    git(git_exe, root, "add", "--all")
    git(git_exe, root, "commit", "-q", "-m", "Unicode baseline")
    return root


@pytest.mark.parametrize("first,second", _PAIRS)
def test_real_unicode_roots_and_open_handles_have_distinct_identity(tmp_path, first, second):
    left, right = tmp_path / first, tmp_path / second
    left.mkdir()
    right.mkdir()
    (left / "content.txt").write_bytes(b"left")
    (right / "content.txt").write_bytes(b"right")
    assert _file_id(left) != _file_id(right) and not os.path.samefile(left, right)
    assert path_key(str(left)) != path_key(str(right))
    assert not same_path(left, right) and not _same_path(left, right)
    assert not path_within(right / "content.txt", left)
    assert Guard(left).read("content.txt") == b"left"
    assert Guard(right).read("content.txt") == b"right"


@pytest.mark.parametrize("first,second", _PAIRS)
def test_manifest_preserves_distinct_unicode_source_files(tmp_path, first, second):
    one, two = first + ".cpp", second + ".cpp"
    (tmp_path / one).write_bytes(b"first file\n")
    (tmp_path / two).write_bytes(b"second file\n")
    assert _file_id(tmp_path / one) != _file_id(tmp_path / two)
    inventory = manifest(tmp_path)
    assert inventory == {one: sha256(b"first file\n").hexdigest(),
                         two: sha256(b"second file\n").hexdigest()}


@pytest.mark.parametrize("first,second", _PAIRS)
def test_tree_pins_preserve_each_distinct_unicode_directory(tmp_path, first, second):
    for name in (first, second):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "source.txt").write_bytes(name.encode("utf-8"))
    assert _file_id(tmp_path / first) != _file_id(tmp_path / second)
    guard = Guard(tmp_path)
    with guard.protect_tree():
        assert guard.read(first + "/source.txt") == first.encode("utf-8")
        assert guard.read(second + "/source.txt") == second.encode("utf-8")


@pytest.mark.parametrize("first,second", _PAIRS)
def test_safe_git_rejects_copied_marker_with_unicode_alias_backpointer(tmp_path, git_exe, first, second):
    main = _committed(tmp_path / "main", git_exe)
    genuine = tmp_path / first
    alias = tmp_path / second
    git(git_exe, main, "worktree", "add", "--detach", str(genuine), "HEAD")
    alias.mkdir()
    marker = (genuine / ".git").read_bytes()
    (alias / ".git").write_bytes(marker)
    (alias / "source.txt").write_bytes(b"unregistered sibling content\n")
    assert _file_id(genuine) != _file_id(alias)
    registered = SafeGit(genuine, git_exe)
    backpointer = (registered.git_dir / "gitdir").read_bytes()
    before_index = (registered.git_dir / "index").read_bytes()
    assert registered.run(["rev-parse", "HEAD"]).stdout == git(git_exe, main, "rev-parse", "HEAD")
    with pytest.raises(ForgePilotError, match="workspace_boundary"):
        SafeGit(alias, git_exe)
    assert (registered.git_dir / "gitdir").read_bytes() == backpointer
    assert (registered.git_dir / "index").read_bytes() == before_index
    assert (genuine / ".git").read_bytes() == marker and (alias / ".git").read_bytes() == marker


@pytest.mark.parametrize("first,second", _PAIRS)
def test_safe_git_pins_each_distinct_unicode_metadata_directory(tmp_path, git_exe, first, second):
    root = _committed(tmp_path / "source", git_exe)
    refs = root / ".git/refs/heads"
    (refs / first).mkdir()
    (refs / second).mkdir()
    assert _file_id(refs / first) != _file_id(refs / second)
    assert SafeGit(root, git_exe).run(["rev-parse", "HEAD"]).stdout == git(git_exe, root, "rev-parse", "HEAD")


@pytest.mark.parametrize("first,second", _PAIRS)
def test_manager_repository_ids_do_not_merge_distinct_unicode_repositories(tmp_path, git_exe, first, second):
    left = _committed(tmp_path / first, git_exe)
    right = _committed(tmp_path / second, git_exe)
    assert _file_id(left / ".git") != _file_id(right / ".git")
    manager = WorkspaceManager(tmp_path / "controlled", git_executable=git_exe)
    a = manager.create(left, "left-task")
    b = manager.create(right, "right-task")
    assert a["repository_id"] != b["repository_id"]
    assert not same_path(a["worktree_path"], b["worktree_path"])
    assert manager._git(a, main=True).root.samefile(left)
    assert manager._git(b, main=True).root.samefile(right)


@pytest.mark.parametrize("first,second", _PAIRS)
def test_manager_exact_scope_does_not_grant_distinct_unicode_file(tmp_path, git_exe, first, second):
    allowed, denied = first + ".cpp", second + ".cpp"
    source = _committed(tmp_path / "source", git_exe,
                        files={allowed: b"approved original\n", denied: b"unapproved original\n"})
    manager = WorkspaceManager(tmp_path / "controlled", git_executable=git_exe)
    state = manager.create(source, "scope-task")
    snapshot = manager.approve_plan("scope-task", approved=True,
                                    expected=manager.inspect_task("scope-task"), approved_paths=[allowed])
    with pytest.raises(ForgePilotError, match="workspace_scope"):
        with manager.writer("scope-task", expected=snapshot) as root:
            (root / denied).write_bytes(b"unauthorized edit\n")
    worktree = Path(state["worktree_path"])
    assert (worktree / allowed).read_bytes() == b"approved original\n"
    assert (worktree / denied).read_bytes() == b"unauthorized edit\n"
    assert (source / denied).read_bytes() == b"unapproved original\n"
    interrupted = manager._load("scope-task")
    assert interrupted["status"] == "interrupted_writer"
    assert interrupted["writer_observed"]["files"][denied] == sha256(b"unauthorized edit\n").hexdigest()


def test_manager_durable_worktree_path_cannot_alias_distinct_unicode_data_root(tmp_path, git_exe):
    main = _committed(tmp_path / "source", git_exe)
    legitimate, alias = tmp_path / "İ", tmp_path / "i\u0307"
    alias.mkdir()
    manager = WorkspaceManager(legitimate, git_executable=git_exe)
    manager.create(main, "identity-task")
    state = manager._load("identity-task")
    relative = Path(state["worktree_path"]).relative_to(legitimate)
    state["worktree_path"] = str(alias / relative)
    manager.guard.write("tasks/identity-task.json", json.dumps(state, ensure_ascii=False).encode("utf-8"))
    assert _file_id(legitimate) != _file_id(alias)
    with pytest.raises(ForgePilotError, match="workspace_corrupted"):
        manager.record("identity-task")
