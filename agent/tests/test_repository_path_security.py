"""Repository roots and leaf paths reject Windows alias and link escapes."""

import os
import subprocess

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.repository import Repository


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ("/../other", ":alternate-stream", "/trailing. "))
async def test_repository_root_rejects_alias_paths(cpp_repo, rg_exe, git_exe, suffix):
    with pytest.raises(ForgePilotError, match="repository_invalid"):
        await Repository.open(str(cpp_repo) + suffix, rg_executable=rg_exe,
                              git_executable=git_exe)


@pytest.mark.asyncio
async def test_repository_root_rejects_unc_and_selected_reparse(cpp_repo, tmp_path, rg_exe, git_exe):
    with pytest.raises(ForgePilotError, match="repository_invalid"):
        await Repository.open(r"\\server\share\repo", rg_executable=rg_exe,
                              git_executable=git_exe)
    alias = tmp_path / "alias"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(cpp_repo)],
                                capture_output=True)
        assert result.returncode == 0
    else:
        alias.symlink_to(cpp_repo, target_is_directory=True)
    with pytest.raises(ForgePilotError, match="repository_invalid"):
        await Repository.open(str(alias), rg_executable=rg_exe, git_executable=git_exe)


def test_repository_leaf_rejects_ads_parent_and_protected_directory(cpp_repo, rg_exe):
    repository = Repository(cpp_repo.resolve(), rg_exe)
    for path in ("main.cpp:stream", "../private", ".git/config", ".env"):
        with pytest.raises(ForgePilotError):
            repository._path(path, allow_missing=True)
