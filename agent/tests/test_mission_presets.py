"""Read-only safe-preset selection and real approved Validator execution."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from forgepilot.artifacts.models import ChangeSet, ChangedFile, TaskSpec
from forgepilot.errors import ForgePilotError
from forgepilot.presets import BuildConfig, MissionBuildSelection, Presets
from forgepilot.toolchain import ToolchainProfile
from forgepilot.validation import ProtectedOracle, ValidationService
from forgepilot.workspace_manager import WorkspaceManager
from test_build import build_config
from test_workspace_manager import git


def documents(*, generator="Ninja"):
    return {"version": 6, "include": ["cmake/base.json"],
        "configurePresets": [{"name": "chosen", "inherits": ["first", "second"], "binaryDir": "${sourceDir}/out/old"}],
        "buildPresets": [{"name": "build", "configurePreset": "chosen", "configuration": "Debug", "jobs": 1}],
        "testPresets": [{"name": "test", "configurePreset": "chosen", "configuration": "Debug",
            "output": {"outputOnFailure": True}, "execution": {"noTestsAction": "error"}}]}, {
        "version": 6, "configurePresets": [{"name": "first", "hidden": True, "generator": generator,
            "cacheVariables": {"ENABLED": True, "MESSAGE": {"type": "STRING", "value": "fixture"}, "REMOVED": None}},
            {"name": "second", "hidden": True, "cacheVariables": {"MESSAGE": "wrong"}}]}


def write_presets(root, document=None, base=None):
    default, default_base = documents()
    document, base = document or default, base or default_base
    (root / "cmake").mkdir(exist_ok=True)
    (root / "CMakePresets.json").write_text(json.dumps(document), encoding="utf-8")
    (root / "cmake/base.json").write_text(json.dumps(base), encoding="utf-8")
    return document, base


def selected(config):
    return replace(config, generator=None, make_program=None,
        configure_preset="chosen", build_preset="build", test_preset="test")


def test_virtual_baseline_includes_are_read_only_and_hash_bound(tmp_path, build_config):
    root = tmp_path / "source"; root.mkdir()
    document, base = documents()
    overlay = {"CMakePresets.json": json.dumps(document).encode(), "cmake/base.json": json.dumps(base).encode()}
    selection = Presets(root, {}, overrides=overlay).resolve_mission(selected(build_config))
    assert list(root.iterdir()) == []
    assert selection.cache_variables == (("ENABLED", "BOOL", "TRUE"), ("MESSAGE", "STRING", "fixture"))
    assert selection.binary_dir == root.as_posix() + "/out/old"
    assert selection.matches_config(selected(build_config))
    assert not selection.matches_config(replace(selected(build_config), configure_timeout=91))
    restored = MissionBuildSelection.from_dict(json.loads(json.dumps(selection.to_dict())))
    assert restored == selection
    with pytest.raises(ForgePilotError, match="build_selection_changed"):
        restored.verify(root)
    write_presets(root, document, base)
    restored.verify(root)
    (root / "cmake/base.json").write_text(json.dumps(base) + " ")
    with pytest.raises(ForgePilotError, match="Preset files changed"):
        restored.verify(root)


def test_frozen_tools_accept_equivalent_windows_path_spelling(tmp_path, build_config):
    write_presets(tmp_path)
    selection = Presets(tmp_path, {}).resolve_mission(selected(build_config))
    if os.name == "nt":
        selection.verify_tools(build_config.cmake.replace("/", "\\").swapcase(),
                               build_config.ctest.replace("/", "\\").swapcase())
    with pytest.raises(ForgePilotError, match="Trusted CMake/CTest selection changed"):
        selection.verify_tools(build_config.ctest, build_config.cmake)


def test_frozen_executable_replacement_is_rejected_without_execution(tmp_path):
    root = tmp_path / "source"; root.mkdir(); write_presets(root)
    tools = tmp_path / "tools"; tools.mkdir()
    cmake, ctest = tools / "cmake.exe", tools / "ctest.exe"
    cmake.write_bytes(b"offline cmake fixture"); ctest.write_bytes(b"offline ctest fixture")
    config = selected(BuildConfig(str(cmake), str(ctest)))
    selection = Presets(root, {}).resolve_mission(config)
    cmake.write_bytes(b"replaced cmake fixture")
    with pytest.raises(ForgePilotError, match="frozen build executable changed"):
        selection.verify_tools(str(cmake), str(ctest))


@pytest.mark.parametrize("case,field", [("environment", "environment"), ("macro", "cacheVariables.MESSAGE"),
    ("directory", "cacheVariables.MESSAGE"), ("filter", "filter"), ("nested", "output"),
    ("toolchain", "toolchainFile"), ("condition", "condition"),
    ("configure-script", "cacheVariables.CMAKE_PROJECT_Project_INCLUDE_BEFORE"),
    ("compiler", "cacheVariables.CMAKE_CXX_COMPILER_LAUNCHER")])
def test_unsupported_semantics_name_the_field_before_any_execution(tmp_path, build_config, case, field):
    document, base = documents()
    configure, test = document["configurePresets"][0], document["testPresets"][0]
    if case == "environment": configure["environment"] = {"PATH": "other"}
    if case == "macro": configure["cacheVariables"] = {"MESSAGE": "$env{PRIVATE_VALUE}"}
    if case == "directory": configure["cacheVariables"] = {"MESSAGE": "${sourceDir}/out/old/generated"}
    if case == "filter": test["filter"] = {"include": {"name": "one"}}
    if case == "nested":
        document["testPresets"].append({"name": "base-test", "hidden": True, "output": {"verbosity": "verbose"}})
        test["inherits"] = "base-test"
    if case == "toolchain": configure["toolchainFile"] = "custom.cmake"
    if case == "configure-script": configure["cacheVariables"] = {
        "CMAKE_PROJECT_Project_INCLUDE_BEFORE": {"type": "FILEPATH", "value": "../mutable-configure.cmake"}}
    if case == "compiler": configure["cacheVariables"] = {
        "CMAKE_CXX_COMPILER_LAUNCHER": {"type": "FILEPATH", "value": "../unfrozen-launcher.exe"}}
    if case == "condition": configure["condition"] = {"type": "equals", "lhs": "a", "rhs": "a"}
    write_presets(tmp_path, document, base)
    with pytest.raises(ForgePilotError, match=field):
        Presets(tmp_path, {}).resolve_mission(selected(build_config))
    assert not (tmp_path / "out").exists()


def test_configuration_macros_use_each_presets_own_context(tmp_path, build_config):
    document, base = documents()
    document["buildPresets"][0]["configuration"] = "${presetName}"
    document["testPresets"][0]["configuration"] = "${presetName}"
    write_presets(tmp_path, document, base)
    with pytest.raises(ForgePilotError, match="configurations do not agree"):
        Presets(tmp_path, {}).resolve_mission(selected(build_config))


def test_project_cannot_inherit_user_preset_and_disabled_selection_is_rejected(tmp_path, build_config):
    document, base = documents()
    document["configurePresets"][0]["inherits"] = "user-only"
    write_presets(tmp_path, document, base)
    (tmp_path / "CMakeUserPresets.json").write_text(json.dumps({"version": 6,
        "configurePresets": [{"name": "user-only", "hidden": True, "generator": "Ninja"}]}))
    with pytest.raises(ForgePilotError, match="not reachable"):
        Presets(tmp_path, {}).resolve_mission(selected(build_config))
    document["configurePresets"][0].update(inherits="first", condition=False)
    write_presets(tmp_path, document, base)
    with pytest.raises(ForgePilotError, match="disabled"):
        Presets(tmp_path, {}).resolve_mission(selected(build_config))


@pytest.mark.asyncio
async def test_supported_preset_runs_real_configure_build_ctest_only_in_controlled_directory(tmp_path, git_exe, build_config):
    source = tmp_path / "source"; source.mkdir()
    document, base = documents(generator="Visual Studio 17 2022")
    document["configurePresets"][0]["architecture"] = {"value": "x64", "strategy": "set"}
    write_presets(source, document, base)
    (source / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.24)\nproject(PresetFixture LANGUAGES CXX)\n'
        'if(NOT ENABLED OR NOT MESSAGE STREQUAL "fixture")\nmessage(FATAL_ERROR "Preset cache semantics lost")\nendif()\n'
        'add_executable(check source.cpp)\nenable_testing()\nadd_test(NAME public COMMAND check)\n')
    (source / "source.cpp").write_text("int main(){return 0;}\n")
    git(git_exe, source, "init", "-q", "-b", "main")
    git(git_exe, source, "config", "user.name", "Fixture"); git(git_exe, source, "config", "user.email", "fixture@example.invalid")
    git(git_exe, source, "add", "--all"); git(git_exe, source, "commit", "-qm", "baseline")
    original = b"int main(){return 0;}\n// main repository user change\n"
    (source / "source.cpp").write_bytes(original)
    manager = WorkspaceManager(tmp_path / "workspaces", git_executable=git_exe)
    task = uuid4().hex[:12]; record = manager.create(source, task); root = Path(record["worktree_path"])
    profile = ToolchainProfile(build_config.cmake, build_config.ctest)
    oracle = tmp_path / "oracle"; oracle.mkdir()
    (oracle / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.24)\nproject(PresetOracle LANGUAGES NONE)\n'
        'enable_testing()\nadd_test(NAME protected COMMAND "${CMAKE_COMMAND}" -E true)\n')
    validator = ValidationService(manager, profile, build_root=tmp_path / "validation", oracle=ProtectedOracle(oracle))
    selection = Presets(root, {}).resolve_mission(selected(build_config))
    validator.bind_build_selection(task, selection)
    assert not (root / "out").exists()
    assert not (validator.guard.root / task / "build/CMakeCache.txt").exists()
    spec = TaskSpec.model_validate({"task_type": "bug_fix", "title": "Preset", "objective": "Use approved preset",
        "repository": {"base_ref": "HEAD", "toolchain_profile": profile.profile_id},
        "problem": {"observed_behavior": "Fixture", "expected_behavior": "Pass", "reproduction_steps": []},
        "acceptance_criteria": [{"id": "AC-1", "description": "CTest passes", "verification": "ctest"}],
        "allowed_scope": {"path_patterns": ["source.cpp"], "max_changed_files": 1}, "risk_level": "low"})
    manager.approve_plan(task, approved=True, expected=manager.inspect_task(task))
    await validator.capture(task, spec)
    before = (root / "source.cpp").read_bytes()
    after = before + b"// approved change\n"
    with manager.writer(task, expected=manager.inspect_task(task)) as worktree:
        (worktree / "source.cpp").write_bytes(after)
    manager.checkpoint_commit(task, kind="implementation", expected=manager.inspect_task(task))
    changes = ChangeSet(execution_plan_artifact_id=str(uuid4()), summary="Approved source change",
        files=(ChangedFile(path="source.cpp", change_type="modified", unified_diff="+// approved change\n",
            before_sha256=sha256(before).hexdigest(), after_sha256=sha256(after).hexdigest(),
            plan_step_ids=("S-1",), acceptance_criteria_ids=("AC-1",), test_change="none"),))
    restarted = ValidationService(manager, profile, build_root=validator.guard.root, oracle=ProtectedOracle(oracle))
    report = await restarted.validate(task, spec, changes)
    assert report.status == "passed", report
    assert report.tests_passed == 1 and report.tests_failed == 0
    assert {item.kind for item in report.commands} >= {"configure", "build", "test"}
    assert not (root / "out").exists()
    assert (source / "source.cpp").read_bytes() == original
    (restarted.guard.root / task / "build-selection.json").unlink()
    rejected = await restarted.validate(task, spec, changes)
    assert rejected.status == "failed" and "original frozen build selection" in rejected.summary
    assert not any(item.kind in {"configure", "build", "test"} for item in rejected.commands)
