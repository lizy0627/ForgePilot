"""The offline launcher must grant ACLs only on a fresh, byte-identical runtime."""

import hashlib
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from offline_cmake_runtime import copy_cmake_runtime


@pytest.fixture
def installed(tmp_path):
    source = tmp_path / "installation"
    (source / "bin").mkdir(parents=True)
    modules = source / "share/cmake-4.4/Modules"
    modules.mkdir(parents=True)
    (modules / "CMake.cmake").write_bytes(b"module1")
    (modules / "empty").mkdir()
    for name in ("cmake.exe", "ctest.exe", "helper.dll"):
        (source / "bin" / name).write_bytes(name.encode())
    return source


def copy_to(directory, installed):
    directory.mkdir()
    return copy_cmake_runtime(
        directory, installed / "bin/cmake.exe", installed / "bin/ctest.exe"
    )


def test_complete_copy_changes_identity_when_tool_or_module_changes(tmp_path, installed):
    first = copy_to(tmp_path / "first", installed)
    private = Path(first["root"])
    assert (private / "share/cmake-4.4/Modules/empty").is_dir()
    assert first["file_count"] == 4
    assert (private / "bin/helper.dll").read_bytes() == b"helper.dll"
    assert not (private / "bin/cmake.exe").samefile(installed / "bin/cmake.exe")
    assert first["cmake_sha256"] == hashlib.sha256(b"cmake.exe").hexdigest()
    assert first["ctest_sha256"] == hashlib.sha256(b"ctest.exe").hexdigest()

    (installed / "share/cmake-4.4/Modules/CMake.cmake").write_bytes(b"module2")
    second = copy_to(tmp_path / "second", installed)
    assert second["tree_sha256"] != first["tree_sha256"]
    assert (private / "share/cmake-4.4/Modules/CMake.cmake").read_bytes() == b"module1"

    (installed / "bin/cmake.exe").write_bytes(b"new version")
    third = copy_to(tmp_path / "third", installed)
    assert third["tree_sha256"] != second["tree_sha256"]
    assert third["cmake_sha256"] != second["cmake_sha256"]
    with pytest.raises(FileExistsError):
        copy_cmake_runtime(
            tmp_path / "first", installed / "bin/cmake.exe", installed / "bin/ctest.exe"
        )


def test_copy_corruption_fails_verification(tmp_path, installed, monkeypatch):
    copyfile = shutil.copyfile

    def corrupt(source, destination):
        copyfile(source, destination)
        if source.name == "helper.dll":
            destination.write_bytes(b"corrupt")

    monkeypatch.setattr(shutil, "copyfile", corrupt)
    with pytest.raises(ValueError, match="identity"):
        copy_to(tmp_path / "run", installed)


def test_rejects_runtime_under_installation(installed):
    with pytest.raises(ValueError, match="overlap"):
        copy_to(installed / "run", installed)
    assert not (installed / "run/cmake-runtime").exists()
