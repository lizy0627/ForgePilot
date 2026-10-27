from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from forgepilot.build import BuildPipeline
from forgepilot.build_process import run_build_process
from forgepilot.errors import ForgePilotError
from forgepilot.security import Redactor
from forgepilot.toolchain import (CustomCommand, ResolvedCommand, ToolchainProfile, _expand,
                                  validate_executable)
from forgepilot.trace import MemoryTrace
from test_build import build_config, build_repo, presets


@pytest.fixture
def tools(tmp_path):
    directory = tmp_path / "Trusted tools with spaces"
    directory.mkdir()
    paths = []
    for name in ("cmake.exe", "ctest.exe", "ninja.exe", "custom-build.exe"):
        path = directory / name
        path.write_bytes(b"trusted binary fixture")
        paths.append(str(path))
    return paths


@pytest.fixture
def values(tmp_path):
    root = tmp_path / "source with spaces"
    root.mkdir()
    return {"source_dir": str(root), "build_dir": str(root / "out"), "configuration": "Debug",
            "generator": "Visual Studio 17 2022", "architecture": "x64", "jobs": "4",
            "make_program": "", "configure_preset": "selected", "build_preset": "selected-build",
            "test_preset": "selected-test", "report_path": str(root / "out/report.xml")}


@pytest.mark.parametrize("phase,preset,expected", [
    ("preset_discovery", False, ["--list-presets=all"]),
    ("cmake_configure", False, ["-S", "{source_dir}", "-B", "{build_dir}", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_BUILD_TYPE=Debug"]),
    ("cmake_configure", True, ["--preset", "selected", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_BUILD_TYPE=Debug"]),
    ("cmake_build", False, ["--build", "{build_dir}", "--config", "Debug"]),
    ("cmake_build", True, ["--build", "--preset", "selected-build", "--config", "Debug"]),
    ("run_tests", False, ["--test-dir", "{build_dir}", "-C", "Debug", "--output-on-failure", "--no-tests=error", "--output-junit", "{report_path}"]),
    ("run_tests", True, ["--preset", "selected-test", "-C", "Debug", "--output-on-failure", "--no-tests=error", "--output-junit", "{report_path}"]),
    ("ctest_inventory", False, ["--test-dir", "{build_dir}", "-C", "Debug", "--show-only=json-v1"]),
])
def test_builtin_profile_has_fixed_commands(tools, values, phase, preset, expected):
    command = ToolchainProfile(*tools[:2]).resolve(phase, values, preset=preset)
    assert command.argv == [tools[int(phase in {"run_tests", "ctest_inventory"})], *[part.format_map(values) for part in expected]]
    assert command.cwd == Path(values["source_dir"]) and command.shell is False
    assert command.review()["shell"] is False


def test_default_profile_optional_host_fields_and_multiconfig(tools, values):
    values["make_program"] = tools[2]
    result = ToolchainProfile(*tools[:2]).resolve("cmake_configure", values, multi_config=True,
                                                   generator=True, architecture=True, make_program=True)
    assert result.args[-6:] == ("-G", "Visual Studio 17 2022", "-A", "x64", "-DCMAKE_MAKE_PROGRAM=" + tools[2], "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
    assert not any("CMAKE_BUILD_TYPE" in arg for arg in result.args)


@pytest.mark.parametrize("template", ["{source_dir.__class__}", "{source_dir[0]}", "{source_dir!r}",
                                     "{source_dir:>99}", "{untrusted}", "{", "{jobs}"])
def test_only_plain_trusted_placeholders(template):
    with pytest.raises(ForgePilotError, match="toolchain_placeholder"):
        _expand(template, {"source_dir": "x"})


@pytest.mark.parametrize("name", ["cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "python3.12.exe",
                                 "pythonw.exe", "py.exe", "node.exe", "git.exe", "bash.exe", "wsl.exe",
                                 "build.cmd", "build.bat", "build.ps1", "build.py", "build.js", "wscript.exe"])
def test_shells_interpreters_git_and_scripts_denied(tmp_path, name):
    target = tmp_path / name
    target.write_bytes(b"fixture")
    with pytest.raises(ForgePilotError, match="command_denied"):
        validate_executable(str(target))


@pytest.mark.parametrize("value", ["cmake", "C:cmake.exe", "\\\\host\\tools\\cmake.exe", "cmake\x00.exe", "",
    "C:\\tools\\cmake.exe:stream", "C:\\tools\\cmake.exe::$DATA", "C:\\.git\\cmake.exe", "C:\\Oracle\\tool.exe", "NUL"])
def test_invalid_executable_paths_denied(value):
    with pytest.raises(ForgePilotError, match="command_denied"):
        validate_executable(value)


def test_profile_rejects_wrong_tool_kind_and_unknown_phase(tools, values):
    with pytest.raises(ForgePilotError, match="command_denied"):
        ToolchainProfile(tools[2], tools[1]).resolve("cmake_build", values)
    with pytest.raises(ForgePilotError, match="command_denied"):
        ToolchainProfile(*tools[:2]).resolve("arbitrary_shell", values)
    with pytest.raises(ForgePilotError, match="command_denied"):
        ResolvedCommand(tools[0], (), Path(values["source_dir"]), shell=True)


@pytest.mark.parametrize("arg", ["a|b", "a > out", "<in", "a&&b", "a;b", "a&b", "$(whoami)",
                               "`whoami`", "a\nb", "a\rb", "\x00"])
def test_custom_command_metacharacters_denied(tools, values, arg):
    custom = CustomCommand(Path(values["source_dir"]), tools[3], (arg,))
    with pytest.raises(ForgePilotError, match="command_denied"):
        custom.review()


@pytest.mark.parametrize("index,args", [(0, ("-E", "echo", "ok")), (0, ("-P", "script.cmake")),
    (0, ("--workflow", "preset")), (0, ("-Pscript.cmake",)), (0, ("--workflow=preset",)),
    (1, ("-S", "script.cmake")), (1, ("--script", "script.cmake")), (1, ("-Sscript.cmake",)),
    (3, ("C:\\Windows\\System32\\cmd.exe", "/c", "whoami")),
    (3, ("python3.12.exe", "-c", "print(1)")), (3, ("git.exe", "commit"))])
def test_custom_commands_cannot_dispatch_forbidden_commands(tools, values, index, args):
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(Path(values["source_dir"]), tools[index], args).review()


@pytest.mark.parametrize("cwd", ["..", "../outside", "C:\\outside", "\\\\server\\share", "out:stream",
                                ".git", "Oracle", "NUL", "trailing."])
def test_custom_command_cwd_boundary(tools, values, cwd):
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(Path(values["source_dir"]), tools[3], (), cwd).review()


@pytest.mark.parametrize("name", [".git", ".oracle", "oracle", "protected-oracle", "protected_oracle",
    "hidden-oracle", "hidden_oracle", ".hidden-tests", ".hidden_tests", "hidden-tests", "hidden_tests",
    "__hidden_tests__", ".forgepilot-tests"])
def test_custom_command_denies_existing_hidden_oracle_directories(tools, values, name):
    root = Path(values["source_dir"])
    (root / name).mkdir()
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(root, tools[3], (), name).review()


def test_custom_command_requires_separate_exact_approval(tools, values):
    command = CustomCommand(Path(values["source_dir"]), tools[3], ("--target", "test target"))
    review = command.review()
    assert review["executable"] == tools[3] and review["args"] == ["--target", "test target"]
    assert review["cwd"] == values["source_dir"] and review["requires_separate_approval"]
    with pytest.raises(ForgePilotError, match="command_approval"):
        command.resolve()
    for approved, digest in [(False, review["approval_digest"]), (True, "changed")]:
        with pytest.raises(ForgePilotError, match="command_approval"):
            command.approve(digest, approved=approved)
    token = command.approve(review["approval_digest"], approved=True)
    assert command.resolve(token).args == command.args
    with pytest.raises(ForgePilotError, match="command_approval"):
        replace(command, args=("other",)).resolve(token)
    Path(tools[3]).write_bytes(b"binary changed after approval")
    with pytest.raises(ForgePilotError, match="command_approval"):
        command.resolve(token)


@pytest.mark.parametrize("index,args", [(0, ("--build", "out", "--config", "Debug")),
                                      (1, ("--test-dir", "out", "--output-on-failure"))])
def test_custom_tool_build_and_test_modes_require_exact_approval(tools, values, index, args):
    command = CustomCommand(Path(values["source_dir"]), tools[index], args)
    with pytest.raises(ForgePilotError, match="command_approval"):
        command.resolve()
    token = command.approve(command.review()["approval_digest"], approved=True)
    assert command.resolve(token).args == args


@pytest.mark.parametrize("field,value", [("filter", {"exclude": {"name": ".*"}}),
    ("targets", ["production-only"]), ("nativeToolOptions", ["-f", "bypass.ninja"]),
    ("cacheVariables", {"BUILD_TESTING": "OFF"}), ("cacheVariables", {"ENABLE_TESTS": False}),
    ("cacheVariables", {"CMAKE_CXX_FLAGS": "-DNDEBUG"}),
    ("cacheVariables", {"CMAKE_CXX_FLAGS": "/DDOCTEST_CONFIG_DISABLE"})])
def test_selected_preset_cannot_bypass_tests(build_repo, build_config, field, value):
    config, data = presets(build_repo, build_config)
    if field == "filter":
        data["testPresets"][0][field] = value
    elif field in {"targets", "nativeToolOptions"}:
        data["buildPresets"][0][field] = value
    else:
        data["configurePresets"][0][field] = value
    (build_repo / "CMakePresets.json").write_text(json.dumps(data))
    pipeline = BuildPipeline(build_repo, config, lambda _: None, MemoryTrace(), "test", Redactor())
    pipeline.selection = {"source": "preset", "configure_preset": "selected", "build_preset": "selected-build", "test_preset": "selected-test"}
    with pytest.raises(ForgePilotError, match="test_integrity"):
        pipeline._validate_preset_policy()


@pytest.mark.parametrize("output", ['{}', '{"tests":{}}', '{"tests":[{"name":"a"},{"name":"a"}]}',
                                    '{"tests":[{}]}', '{"tests":[{"name":""}]}', "not JSON"])
def test_ctest_inventory_is_strict(output):
    with pytest.raises(ForgePilotError, match="test_inventory"):
        BuildPipeline._inventory_names(output)


def test_ctest_inventory_records_exact_names():
    assert BuildPipeline._inventory_names('{"tests":[{"name":"one"},{"name":"two"}]}') == ["one", "two"]


@pytest.mark.asyncio
async def test_existing_ctest_inventory_is_recorded_before_any_configure(build_repo, build_config):
    directory = build_repo / ".forgepilot/build"
    directory.mkdir(parents=True)
    command = Path(build_config.cmake).as_posix()
    (directory / "CTestTestfile.cmake").write_text(f'add_test(approval_inventory "{command}" -E true)\n')
    pipeline = BuildPipeline(build_repo, build_config, lambda _: None, MemoryTrace(), "test", Redactor())
    await pipeline.prepare()
    report = pipeline.report()
    assert report["initial_ctest_names"] == ["approval_inventory"]
    assert [entry["phase"] for entry in report["commands"]] == ["ctest_inventory"]
    assert set(report["stages"].values()) == {"not_run"}


@pytest.mark.asyncio
async def test_private_pipeline_still_refuses_arbitrary_phase(build_repo, build_config):
    pipeline = BuildPipeline(build_repo, build_config, lambda _: None, MemoryTrace(), "test", Redactor())
    with pytest.raises(ForgePilotError, match="command_denied"):
        await pipeline._command("shell", ["cmd.exe", "/c", "whoami"], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cmake --build .", [], [None], [""], ["tool\x00.exe"], ["tool", "x" * 120_001], ["tool"] * 258])
async def test_process_runner_requires_bounded_argv_before_spawning(tmp_path, command):
    with pytest.raises(ForgePilotError, match="command_denied"):
        await run_build_process(command, tmp_path, timeout=1)


def test_executable_must_be_regular_single_link_file(tools, tmp_path):
    with pytest.raises(ForgePilotError, match="command_denied"):
        validate_executable(str(tmp_path))
    duplicate = tmp_path / "hardlink.exe"
    os.link(tools[3], duplicate)
    with pytest.raises(ForgePilotError, match="command_denied"):
        validate_executable(tools[3])


def test_executable_parent_reparse_attribute_denied(tools, monkeypatch):
    original = Path.lstat
    parent = Path(tools[3]).parent
    def metadata(path, *args, **kwargs):
        if path == parent:
            return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ForgePilotError, match="command_denied"):
        validate_executable(tools[3])


def test_source_directory_must_be_absolute(tools, values):
    values["source_dir"] = "relative-source"
    with pytest.raises(ForgePilotError, match="command_denied"):
        ToolchainProfile(*tools[:2]).resolve("cmake_build", values)


@pytest.mark.parametrize("args", [["--target", "test"], tuple("x" for _ in range(257)), (None,), ("x" * 32_769,)])
def test_custom_arguments_are_typed_and_bounded(tools, values, args):
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(Path(values["source_dir"]), tools[3], args).review()


def test_custom_cwd_ordinary_child_directory_and_file(tools, values):
    root = Path(values["source_dir"])
    child = root / "child"
    child.mkdir()
    command = CustomCommand(root, tools[3], (), "child")
    token = command.approve(command.review()["approval_digest"], approved=True)
    assert command.resolve(token).cwd == child
    (root / "file.txt").write_text("ordinary file")
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(root, tools[3], (), "file.txt").review()


def test_custom_junction_cwd_and_root_denied(tools, values, tmp_path):
    root = Path(values["source_dir"])
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / "junction"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(ForgePilotError, match="command_denied"):
            CustomCommand(root, tools[3], (), "junction").review()
        with pytest.raises(ForgePilotError, match="command_denied"):
            CustomCommand(junction, tools[3], ()).review()
    finally:
        junction.rmdir()


def test_custom_executable_size_guard(tools, values, monkeypatch):
    original = Path.stat
    def metadata(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == Path(tools[3]) and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(st_size=268_435_457)
        return info
    monkeypatch.setattr(Path, "stat", metadata)
    with pytest.raises(ForgePilotError, match="command_denied"):
        CustomCommand(Path(values["source_dir"]), tools[3], ()).review()
