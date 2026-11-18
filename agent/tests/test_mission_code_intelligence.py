"""Mission clangd stays on the worktree and the approved task build."""
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest

from forgepilot.artifacts.models import TaskSpec
from forgepilot.code_intelligence import CodeIntelligence, CodeIntelligenceConfig
from forgepilot.compile_database import TrustedCompilationDirectory, load_database
from forgepilot.errors import ForgePilotError
from forgepilot.repository import Repository
from forgepilot.toolchain import ToolchainProfile
from forgepilot.validation import ProtectedOracle, ValidationService
from forgepilot.workspace_manager import WorkspaceManager
from forgepilot.workspace_safety import Guard
from test_code_intelligence import symbol_repo, args, database, peer_factory, assert_closed
from test_workspace_manager import git


def external_database(repository, directory):
    directory.mkdir(parents=True)
    (directory / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={repository.root}\n"
        f"CMAKE_CACHEFILE_DIR:INTERNAL={directory}\nCMAKE_GENERATOR:INTERNAL=Ninja\n", encoding="utf-8")
    source = repository.root / "src/main.cpp"
    entry = {"directory": str(directory), "file": str(source),
             "arguments": ["clang++", "-o", "main.obj", "-c", str(source)]}
    path = directory / "compile_commands.json"
    path.write_text(json.dumps([entry]))
    return TrustedCompilationDirectory(repository.root, Guard(directory)), path, entry


@pytest.mark.asyncio
async def test_task_database_grant_keeps_external_build_and_status_visible(symbol_repo, tmp_path):
    grant, _, _ = external_database(symbol_repo, tmp_path / "approved-build")
    factory, sessions, _ = peer_factory(tmp_path)
    observed = []
    tool = CodeIntelligence(symbol_repo, CodeIntelligenceConfig(sys.executable),
        selected_directory=lambda: grant, client_factory=factory, on_status=observed.append)
    assert tool.availability() == {"enabled": True, "used": False, "fallback_reasons": []}
    assert not sessions
    result = json.loads(await tool.query("find_symbol", **args(symbol_repo)))
    assert result["source"] == "clangd" and result["semantic"]
    assert result["compilation_database"] == "<approved-task-build>/compile_commands.json"
    assert observed == [{"enabled": True, "used": True, "fallback_reasons": []}]
    assert_closed(sessions)


@pytest.mark.parametrize("change,reason", [
    ("cache-source", "compile_database_stale"),
    ("cache-build", "compile_database_stale"),
    ("cwd", "compile_database_boundary"),
    ("source", "compile_database_stale"),
    ("output", "compile_database_boundary"),
    ("output-field", "compile_database_boundary"),
    ("plugin", "compile_database_unsupported"),
    ("generator", "compile_database_generator"),
])
@pytest.mark.asyncio
async def test_granted_database_still_checks_command_source_output_and_generator(
        symbol_repo, tmp_path, change, reason):
    grant, path, entry = external_database(symbol_repo, tmp_path / "approved-build")
    cache = path.parent / "CMakeCache.txt"
    if change == "cache-source":
        cache.write_text(cache.read_text(encoding="utf-8").replace(str(symbol_repo.root), str(tmp_path)), encoding="utf-8")
    elif change == "cache-build":
        cache.write_text(cache.read_text(encoding="utf-8").replace(str(path.parent), str(tmp_path)), encoding="utf-8")
    elif change == "generator":
        cache.write_text(cache.read_text(encoding="utf-8").replace("Ninja", "Visual Studio 17 2022"), encoding="utf-8")
    else:
        if change == "cwd": entry["directory"] = str(tmp_path)
        if change == "source": entry["arguments"][-1] = str(tmp_path / "other.cpp")
        if change == "output": entry["arguments"][2] = str(tmp_path / "other.obj")
        if change == "output-field": entry["output"] = str(tmp_path / "other.obj")
        if change == "plugin": entry["arguments"].append("-fplugin=outside.dll")
        path.write_text(json.dumps([entry]))
    tool = CodeIntelligence(symbol_repo, selected_directory=lambda: grant,
        client_factory=lambda *a, **kw: pytest.fail("Invalid metadata must not start clangd"))
    assert tool.availability()["fallback_reasons"] == [reason]
    result = json.loads(await tool.query("find_symbol", **args(symbol_repo)))
    assert result["fallback_reason"] == reason and result["text_matches"]


def test_raw_external_directory_and_cross_task_grant_are_rejected(symbol_repo, tmp_path):
    grant, _, _ = external_database(symbol_repo, tmp_path / "approved-build")
    with pytest.raises(ForgePilotError, match="build_path"):
        load_database(symbol_repo, grant.path)
    other = tmp_path / "other-worktree"
    other.mkdir()
    with pytest.raises(ForgePilotError, match="compile_database_boundary"):
        load_database(Repository(other, symbol_repo.rg_executable), grant)


@pytest.mark.asyncio
async def test_main_database_never_becomes_current_worktree_evidence(symbol_repo, tmp_path):
    main_database = database(symbol_repo)
    worktree = tmp_path / "task-worktree"
    shutil.copytree(symbol_repo.root / "src", worktree / "src")
    task_repository = Repository(worktree, symbol_repo.rg_executable)
    tool = CodeIntelligence(task_repository,
        CodeIntelligenceConfig(compile_commands_dir=str(main_database)),
        client_factory=lambda *a, **kw: pytest.fail("Main repository database must not start clangd"))
    result = json.loads(await tool.query("find_symbol", **args(task_repository)))
    assert result["source"] == "rg" and result["fallback_reason"] == "build_path"
    assert all(item["path"].startswith("src/") for item in result["text_matches"])


@pytest.mark.asyncio
async def test_validator_grants_real_generated_database_only_after_capture_and_restart(
        symbol_repo, tmp_path, git_exe, monkeypatch):
    cmake_path = symbol_repo.root / "CMakeLists.txt"
    cmake_path.write_text(cmake_path.read_text() +
        'enable_testing()\nadd_test(NAME public COMMAND "${CMAKE_COMMAND}" -E true)\n')
    git(git_exe, symbol_repo.root, "config", "user.name", "Fixture")
    git(git_exe, symbol_repo.root, "config", "user.email", "fixture@example.invalid")
    git(git_exe, symbol_repo.root, "add", "--all")
    git(git_exe, symbol_repo.root, "commit", "-qm", "baseline")
    manager = WorkspaceManager(tmp_path / "workspaces", git_executable=git_exe)
    task = uuid4().hex[:12]
    record = manager.create(symbol_repo.root, task)
    profile = ToolchainProfile(shutil.which("cmake"), shutil.which("ctest"))
    oracle_root = tmp_path / "oracle"
    oracle_root.mkdir()
    (oracle_root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.24)\nproject(Oracle LANGUAGES NONE)\n")
    oracle = ProtectedOracle(oracle_root)
    validator = ValidationService(manager, profile, build_root=tmp_path / "validation", oracle=oracle,
                                  generator="Ninja", make_program=shutil.which("ninja"))
    spec = TaskSpec.model_validate({
        "task_type": "bug_fix", "title": "Fixture", "objective": "Inspect task symbols",
        "repository": {"base_ref": "HEAD", "toolchain_profile": profile.profile_id},
        "problem": {"observed_behavior": "Wrong value", "expected_behavior": "Correct value", "reproduction_steps": []},
        "acceptance_criteria": [{"id": "AC-1", "description": "All checks pass", "verification": "ctest"}],
        "allowed_scope": {"path_patterns": ["src/**", "include/**", "CMakeLists.txt"], "max_changed_files": 4},
        "risk_level": "low",
    })
    task_repository = Repository(Path(record["worktree_path"]), symbol_repo.rg_executable)
    async def forbidden(*a, **kw):
        pytest.fail("Pre-approval inspection must not start configure/build")
    tool = CodeIntelligence(task_repository,
        selected_directory=lambda: validator.compilation_directory(task))
    with monkeypatch.context() as patch:
        patch.setattr("forgepilot.validation.run_build_process", forbidden)
        assert validator.compilation_directory(task) is None
        assert tool.availability()["fallback_reasons"] == ["compile_database_missing"]
    await validator.capture(task, spec)
    grant = validator.compilation_directory(task)
    loaded = load_database(task_repository, grant)
    assert set(loaded.files) == {"src/main.cpp", "src/calc.cpp", "src/helper.cpp"}
    assert loaded.path.parent.is_relative_to(validator.guard.root)
    restarted = ValidationService(manager, profile, build_root=tmp_path / "validation", oracle=oracle,
        generator="Ninja", make_program=shutil.which("ninja"))
    assert restarted.compilation_directory(task).path == grant.path
    factory, sessions, _ = peer_factory(tmp_path)
    observed = []
    tool = CodeIntelligence(task_repository, CodeIntelligenceConfig(str(tmp_path / "absent-clangd.exe")),
        selected_directory=lambda: restarted.compilation_directory(task),
        client_factory=factory, on_status=observed.append)
    result = json.loads(await tool.query("find_symbol", **args(task_repository)))
    assert result["fallback_reason"] == "clangd_unavailable" and result["text_matches"]
    assert observed[-1]["fallback_reasons"] == ["clangd_unavailable"] and not sessions
    tool.config = CodeIntelligenceConfig(sys.executable)
    result = json.loads(await tool.query("find_symbol", **args(task_repository)))
    assert result["source"] == "clangd"
    assert_closed(sessions)
