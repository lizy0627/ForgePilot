"""Capability gaps must be distinguishable from setup failures."""
import errno
from pathlib import Path

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from offline_symlink_probe import probe_symlinks

    return probe_symlinks


def test_records_permission_for_each_kind_separately(probe, tmp_path, monkeypatch):
    attempts = []

    def denied(link, target, target_is_directory=False):
        attempts.append((link.parent, target_is_directory))
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "symlink_to", denied)
    result = probe(tmp_path)
    assert [directory for _, directory in attempts] == [False, True]
    assert all(folder.parent == tmp_path for folder, _ in attempts)
    assert result["file"]["status"] == result["directory"]["status"] == "permission_denied"
    assert result["file"]["errno"] == result["directory"]["errno"] == errno.EACCES
    assert not list(tmp_path.iterdir())


def test_unrelated_oserror_is_a_setup_failure(probe, tmp_path, monkeypatch):
    def out_of_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(Path, "symlink_to", out_of_space)
    with pytest.raises(OSError) as error:
        probe(tmp_path)
    assert error.value.errno == errno.ENOSPC
    assert not list(tmp_path.iterdir())


def test_ordinary_entry_cannot_prove_symlink_support(probe, tmp_path, monkeypatch):
    def make_regular_file(link, *args, **kwargs):
        link.touch()

    monkeypatch.setattr(Path, "symlink_to", make_regular_file)
    with pytest.raises(RuntimeError, match="symbolic link"):
        probe(tmp_path)
    assert not list(tmp_path.iterdir())
