"""Real filesystem and OS lease tests, including Windows junction boundaries."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.workspace_safety import Guard, WriterLease, manifest, remove_worktree_files, validate_relative


@pytest.mark.parametrize("value", ["../other", "x/../other", "/absolute", "\\\\server\\share", "C:stream",
                                  "a:stream", "a/.git/config", ".GIT", "a\0b", "a//b", "a/./b",
                                  "NUL.txt", "com1/x", "a.", "a ", "x?y", "\\\\?\\D:\\data"])
def test_relative_paths_reject_windows_aliases(value):
    with pytest.raises(ForgePilotError, match="workspace_boundary"):
        validate_relative(value)


def test_guard_atomic_bytes_and_complete_manifest(tmp_path):
    guard = Guard(tmp_path / "controlled", create=True)
    guard.mkdir("src/deep")
    guard.write("src/deep/file.bin", b"\0\xffdata")
    guard.write(".hidden", b"included")
    (guard.root / ".git").mkdir()
    (guard.root / ".git" / "metadata").write_bytes(b"excluded")
    assert guard.read("src\\deep\\file.bin") == b"\0\xffdata"
    assert manifest(guard.root) == {
        ".hidden": hashlib.sha256(b"included").hexdigest(),
        "src/deep/file.bin": hashlib.sha256(b"\0\xffdata").hexdigest(),
    }
    with pytest.raises(ForgePilotError):
        guard.write("src/deep/file.bin", b"overwrite", overwrite=False)
    assert guard.read("src/deep/file.bin") == b"\0\xffdata"
    assert not list(guard.root.rglob(".forgepilot-*.tmp"))
    with pytest.raises(ForgePilotError):
        guard.read("src/deep/file.bin", max_bytes=2)


def test_guard_rejects_replaced_root_and_opened_handle(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    guard = Guard(selected)
    selected.rename(tmp_path / "old")
    selected.mkdir()
    with pytest.raises(ForgePilotError, match="replaced"):
        guard.check()
    guard = Guard(selected)
    (selected / "file").write_bytes(b"safe")
    monkeypatch.setattr("forgepilot.workspace_safety.opened_path", lambda fd: tmp_path / "outside")
    with pytest.raises(ForgePilotError, match="location changed"):
        guard.read("file")
    with pytest.raises(ForgePilotError, match="escaped"):
        guard.write("other", b"safe")
    assert not (selected / "other").exists()


def _directory_link(source, target):
    if os.name == "nt":
        # mklink /J does not require Developer Mode or symlink privilege.
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(source), str(target)],
                                capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    else:
        source.symlink_to(target, target_is_directory=True)


def test_junction_or_symlink_never_entered(tmp_path):
    root = tmp_path / "controlled"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "user.txt").write_bytes(b"user")
    _directory_link(root / "link", outside)
    guard = Guard(root)
    for operation in (lambda: guard.read("link/user.txt"), lambda: guard.write("link/new.txt", b"x"),
                      lambda: guard.mkdir("link/child"), lambda: manifest(root), lambda: Guard(root / "link")):
        with pytest.raises(ForgePilotError, match="workspace_boundary"):
            operation()
    assert (outside / "user.txt").read_bytes() == b"user"
    assert sorted(path.name for path in outside.iterdir()) == ["user.txt"]


@pytest.mark.skipif(os.name != "nt", reason="Windows share-delete directory handle semantics")
def test_windows_protect_blocks_parent_rename(tmp_path):
    guard = Guard(tmp_path / "controlled", create=True)
    guard.mkdir("src")
    with guard.protect("src"):
        with pytest.raises(OSError):
            (guard.root / "src").rename(guard.root / "renamed")
    (guard.root / "src").rename(guard.root / "renamed")


def test_manifest_rejects_nested_git_and_excess_bytes(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".git").write_bytes(b"not metadata")
    with pytest.raises(ForgePilotError):
        manifest(tmp_path)
    (tmp_path / "src" / ".git").unlink()
    (tmp_path / "large").write_bytes(b"123")
    monkeypatch.setattr("forgepilot.workspace_safety.MAX_TOTAL_BYTES", 2)
    with pytest.raises(ForgePilotError, match="workspace_limit"):
        manifest(tmp_path)


def test_writer_lease_preserves_inode_and_excludes_second_process(tmp_path):
    repository_id = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    different_id = hashlib.sha256((str(tmp_path) + "/different").encode()).hexdigest()
    with WriterLease(tmp_path, repository_id, "task") as lease:
        inode = lease.guard.path(lease.relative).stat().st_ino
        with pytest.raises(ForgePilotError, match="workspace_lease_busy"):
            WriterLease(tmp_path, repository_id, "other", recover=True).acquire()
        script = ("from forgepilot.workspace_safety import WriterLease\n"
                  "from forgepilot.errors import ForgePilotError\n"
                  "import sys\n"
                  "try: WriterLease(sys.argv[1], sys.argv[2], 'child', recover=True).acquire()\n"
                  "except ForgePilotError as e: sys.exit(0 if e.code == 'workspace_lease_busy' else 2)\n"
                  "sys.exit(3)\n")
        process = subprocess.run([sys.executable, "-c", script, str(tmp_path), repository_id], capture_output=True)
        assert process.returncode == 0, process.stderr
        with WriterLease(tmp_path, different_id, "parallel"):
            lease.check()
    with WriterLease(tmp_path, repository_id, "other") as next_lease:
        assert next_lease.guard.path(next_lease.relative).stat().st_ino == inode
        assert next_lease.previous["active"] is False


def test_writer_crash_requires_explicit_recovery(tmp_path):
    repository_id = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    script = ("from forgepilot.workspace_safety import WriterLease\nimport os,sys\n"
              "lease=WriterLease(sys.argv[1], sys.argv[2], 'crashed').acquire()\nos._exit(0)\n")
    subprocess.run([sys.executable, "-c", script, str(tmp_path), repository_id], check=True, capture_output=True)
    with pytest.raises(ForgePilotError, match="workspace_lease_stale"):
        WriterLease(tmp_path, repository_id, "new").acquire()
    with WriterLease(tmp_path, repository_id, "new", recover=True) as lease:
        assert lease.previous["active"] and lease.previous["owner"] == "crashed"
    payload = json.loads((tmp_path / "leases" / (repository_id + ".lock")).read_bytes())
    assert payload["owner"] == "new" and payload["active"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows exact handle disposition")
def test_handle_cleanup_deletes_only_verified_objects_and_retries(tmp_path):
    root = tmp_path / "controlled"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/file").write_bytes(b"approved")
    marker = b"gitdir: D:/controlled/.git/worktrees/task\n"
    (root / ".git").write_bytes(marker)
    expected = manifest(root)
    identity = (root.stat().st_dev, root.stat().st_ino)
    (root / "user-file").write_bytes(b"preserve")
    with pytest.raises(ForgePilotError, match="unknown file"):
        remove_worktree_files(root, expected, hashlib.sha256(marker).hexdigest(), expected_root_identity=identity)
    assert (root / "src/file").read_bytes() == b"approved"
    (root / "user-file").unlink()
    with (root / "src/file").open("r+b"):
        with pytest.raises(ForgePilotError, match="workspace_cleanup_locked"):
            remove_worktree_files(root, expected, hashlib.sha256(marker).hexdigest(), expected_root_identity=identity)
    assert root.exists()
    remove_worktree_files(root, expected, hashlib.sha256(marker).hexdigest(), expected_root_identity=identity)
    assert not root.exists()
    remove_worktree_files(root, expected, hashlib.sha256(marker).hexdigest(), expected_root_identity=identity)


@pytest.mark.skipif(os.name != "nt", reason="Windows exact handle disposition")
def test_handle_cleanup_refuses_replaced_root_and_hardlinks(tmp_path):
    root = tmp_path / "controlled"
    root.mkdir()
    (root / "file").write_bytes(b"approved")
    (root / ".git").write_bytes(b"marker")
    expected = manifest(root)
    identity = (root.stat().st_dev, root.stat().st_ino)
    root.rename(tmp_path / "old")
    root.mkdir()
    (root / "file").write_bytes(b"approved")
    (root / ".git").write_bytes(b"marker")
    with pytest.raises(ForgePilotError, match="persisted"):
        remove_worktree_files(root, expected, hashlib.sha256(b"marker").hexdigest(), expected_root_identity=identity)
    os.link(root / "file", tmp_path / "user-hardlink")
    with pytest.raises(ForgePilotError, match="hardlinked"):
        remove_worktree_files(root, expected, hashlib.sha256(b"marker").hexdigest())
    assert (tmp_path / "user-hardlink").read_bytes() == b"approved"


@pytest.mark.skipif(os.name != "nt", reason="Windows exact handle deletion and long paths")
def test_unlink_hash_and_long_artifact_paths(tmp_path):
    guard = Guard(tmp_path)
    relative = "/".join(["long-component-" + "x" * 40] * 4)
    guard.mkdir(relative)
    name = relative + "/" + "a" * 64 + "-DeliveryBundle.manifest.json"
    assert len(str(guard.root / name)) > 260
    guard.write(name, b"verified artifact", overwrite=False)
    digest = hashlib.sha256(b"verified artifact").hexdigest()
    assert guard.read(name) == b"verified artifact"
    with pytest.raises(ForgePilotError, match="bytes changed"):
        guard.unlink(name, hashlib.sha256(b"other").hexdigest())
    assert guard.read(name) == b"verified artifact"
    guard.unlink(name, digest)
    assert not (guard.root / name).exists()
    guard.unlink(name, digest)


@pytest.mark.skipif(os.name != "nt", reason="Windows global repository OS mutex")
def test_writer_lease_coordinates_different_data_roots_and_releases_after_failure(tmp_path):
    repository_id = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    first, second = tmp_path / "first-data", tmp_path / "second-data"
    with WriterLease(first, repository_id, "first"):
        # Mutex ownership is recursive on the same OS thread; our local set
        # must reject this second distinct manager rather than acquire it twice.
        with pytest.raises(ForgePilotError, match="workspace_lease_busy"):
            WriterLease(second, repository_id, "second", recover=True).acquire()
        script = ("from forgepilot.workspace_safety import WriterLease\n"
                  "from forgepilot.errors import ForgePilotError\nimport sys\n"
                  "try: WriterLease(sys.argv[1], sys.argv[2], 'child', recover=True).acquire()\n"
                  "except ForgePilotError as e: sys.exit(0 if e.code == 'workspace_lease_busy' else 2)\n"
                  "sys.exit(3)\n")
        child = subprocess.run([sys.executable, "-c", script, str(second), repository_id], capture_output=True)
        assert child.returncode == 0, child.stderr
    # Both the process reservation and named mutex have been released.
    with WriterLease(second, repository_id, "second"):
        pass
    script = ("from forgepilot.workspace_safety import WriterLease\nimport sys\n"
              "with WriterLease(sys.argv[1], sys.argv[2], 'child'): pass\n")
    child = subprocess.run([sys.executable, "-c", script, str(first), repository_id], capture_output=True)
    assert child.returncode == 0, child.stderr
