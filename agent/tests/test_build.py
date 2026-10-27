import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.build import BUILD_TOOLS, BuildPipeline
from forgepilot.build_process import BuildCancelled, build_environment, run_build_process
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.presets import BuildConfig, Presets, available_presets, safe_build_path
from forgepilot.policy import Principal, Role
from forgepilot.process import executable
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from test_changes import write
from test_planning import proposal


@pytest.fixture
def build_config():
    cmake = executable(os.environ.get("FORGEPILOT_TEST_CMAKE"), "cmake")
    ctest = str(Path(cmake).with_name("ctest.exe"))
    ninja = executable(os.environ.get("FORGEPILOT_TEST_NINJA"), "ninja")
    return BuildConfig(cmake, ctest, generator="Ninja", make_program=ninja)


@pytest.fixture
def build_repo(tmp_path, git_exe):
    root = tmp_path / "C++ build 项目 with spaces"
    root.mkdir()
    subprocess.run([git_exe, "init", "-q", str(root)], check=True, capture_output=True)
    (root / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.24)
project(ControlledFixture LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
add_executable(counter main.cpp)
enable_testing()
add_test(NAME counter_test COMMAND counter)
''', encoding="utf-8")
    (root / "main.cpp").write_text("int main() { return 1; }\n", encoding="utf-8")
    subprocess.run([git_exe, "-C", str(root), "add", "main.cpp"], check=True, capture_output=True)
    with (root / "main.cpp").open("a", encoding="utf-8") as f:
        f.write("// existing user change\n")
    (root / "notes.txt").write_text("user untracked\n")
    return root


def service_for_build(root, config, rg_exe, git_exe, *, content=None, paths=("main.cpp",), redactor=None):
    content = content if content is not None else "int main() { return 0; }\n// existing user change\n"
    llm = ScriptedLLM([reply("main.cpp causes the failure"), reply(json.dumps(proposal(*paths))),
                       reply(calls=[call("read_file", json.dumps({"path": paths[0]}), "read-source"),
                                    write(paths[0], content)]), reply("Source changes completed")])
    trace = MemoryTrace(redactor)
    return TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace,
                       build_config=config, redactor=redactor), trace


async def finish(service, root):
    pending = await service.start(str(root), "Fix the program and validate it")
    assert pending.state == TaskState.WAITING_APPROVAL, pending.error
    service.approve(pending.task_id, pending.plan_version)
    return await service.execute(pending.task_id)


def presets(root, config, *, multi=False):
    hidden = {"name": "base", "hidden": True, "generator": "Ninja Multi-Config" if multi else "Ninja",
              "binaryDir": "${sourceDir}/$env{BUILD_DIR}", "environment": {"BUILD_KIND": "Release", "BUILD_DIR": "out/preset build"},
              "cacheVariables": {"CMAKE_MAKE_PROGRAM": config.make_program}}
    if not multi:
        hidden["cacheVariables"]["CMAKE_BUILD_TYPE"] = "$env{BUILD_KIND}"
    (root / "cmake").mkdir(exist_ok=True)
    (root / "cmake/base.json").write_text(json.dumps({"version": 6, "configurePresets": [hidden]}))
    data = {"version": 6, "include": ["cmake/base.json"],
        "configurePresets": [{"name": "selected", "inherits": "base"}],
        "buildPresets": [{"name": "selected-build", "configurePreset": "selected", "configuration": "Release"}],
        "testPresets": [{"name": "selected-test", "configurePreset": "selected", "configuration": "Release"}]}
    (root / "CMakePresets.json").write_text(json.dumps(data))
    return replace(config, generator=None, make_program=None), data


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default_ninja", "default_vs", "preset_single", "preset_multi"])
async def test_real_mvp_modes_preserve_user_changes(build_repo, build_config, rg_exe, git_exe, mode):
    config = build_config
    if mode == "default_vs":
        config = replace(config, generator="Visual Studio 17 2022", architecture="x64", make_program=None)
    if mode.startswith("preset"):
        config, _ = presets(build_repo, config, multi=mode == "preset_multi")
    original = (build_repo / "main.cpp").read_bytes()
    index = (build_repo / ".git/index").read_bytes()
    service, trace = service_for_build(build_repo, config, rg_exe, git_exe)
    try:
        pending = await service.start(str(build_repo), "Fix and build")
        assert pending.state == TaskState.WAITING_APPROVAL, pending.error
        assert (build_repo / "main.cpp").read_bytes() == original
        selection = pending.build_result["selection"]
        assert not Path(selection["binary_dir"]).exists(), "No configure before approval"
        registry = service._runtime.registry
        assert all(registry.get(t) for t in BUILD_TOOLS)
        for tool in BUILD_TOOLS:
            denied = await registry.execute(tool, {})
            assert not denied.success and "policy_denied" in denied.error
        with registry.as_principal(Principal(Role.VALIDATOR)):
            assert "invalid_arguments" in (await registry.execute("cmake_build", {"command": "arbitrary"})).error
        service.approve(pending.task_id, 1)
        result = await service.execute(pending.task_id)
        assert result.state == TaskState.COMPLETED, json.dumps({"error": result.error, "build": result.build_result}, ensure_ascii=False)
        assert result.outcome == "modification_validated"
        report = result.build_result
        assert report["tests"] == {"passed": 1, "failed": 0, "total": 1, "not_run": 0, "status": "passed"}
        assert list(report["stages"].values()) == ["succeeded"] * 3
        selection = report["selection"]
        assert selection["source"] == ("preset" if mode.startswith("preset") else "default")
        assert selection["configuration"] == ("Release" if mode.startswith("preset") else "Debug")
        assert selection["multi_config"] == (mode in {"default_vs", "preset_multi"})
        assert Path(selection["binary_dir"]) == build_repo / ("out/preset build" if mode.startswith("preset") else ".forgepilot/build")
        assert report["compile_commands"]["status"] == ("unsupported_generator" if mode == "default_vs" else "available_after_configure")
        for command in report["commands"]:
            assert command["returncode"] == 0 and command["status"] == "succeeded"
            assert command["cwd"] == str(build_repo) and isinstance(command["command"], list)
            assert command["duration_ms"] >= 0
        states = [e.data["state"] for e in trace.events if e.kind == "task_state_changed"]
        assert states == ["analyzing", "planning", "waiting_approval", "modifying", "building", "testing", "completed"]
        assert result.steps == 4  # Automatic validation consumes no LLM steps and performs no Repair.
        assert result.task_diff["files"][0]["path"] == "main.cpp"
        assert (build_repo / ".git/index").read_bytes() == index
        assert (build_repo / "notes.txt").read_text() == "user untracked\n"
        restored = await service.restore(result.task_id)
        assert restored["conflicts"] == 0 and (build_repo / "main.cpp").read_bytes() == original, restored
        assert Path(selection["binary_dir"]).exists()  # Source restoration never deletes build artifacts.
        print("REAL BUILD MODE", mode, json.dumps(report, ensure_ascii=False))
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [("configure", ["failed", "not_run", "not_run"]),
    ("compile", ["succeeded", "failed", "not_run"]), ("tests", ["succeeded", "succeeded", "failed"]),
    ("zero", ["succeeded", "succeeded", "failed"]), ("disabled", ["succeeded", "succeeded", "failed"]),
    ("not_run", ["succeeded", "succeeded", "failed"])])
async def test_actual_failure_stages(build_repo, build_config, rg_exe, git_exe, failure, expected):
    if failure == "configure":
        with (build_repo / "CMakeLists.txt").open("a") as f:
            f.write('\nmessage(FATAL_ERROR "controlled configure failure")\n')
    elif failure == "zero":
        cmake = build_repo / "CMakeLists.txt"
        cmake.write_text(cmake.read_text().replace("add_test(NAME counter_test COMMAND counter)", ""))
    elif failure == "disabled":
        with (build_repo / "CMakeLists.txt").open("a") as f:
            f.write('\nset_tests_properties(counter_test PROPERTIES DISABLED TRUE)\n')
    elif failure == "not_run":
        cmake = build_repo / "CMakeLists.txt"
        cmake.write_text(cmake.read_text().replace("COMMAND counter)", "COMMAND missing-test-executable)"))
    # A stale successful report must never become this task's result.
    output = build_repo / ".forgepilot/build"
    output.mkdir(parents=True)
    (output / "old.xml").write_text('<testsuite tests="9"><testcase name="old"/></testsuite>')
    content = "syntax error here!\n" if failure == "compile" else "int main(){return 1;}\n" if failure == "tests" else None
    service, _ = service_for_build(build_repo, build_config, rg_exe, git_exe, content=content)
    if failure in {"compile", "tests"}:
        from test_repair import reflection
        messages = list(service.llm.replies)
        for _ in range(3):
            messages += [reflection(), reply(calls=[write("main.cpp", content)]), reply("Attempted repair")]
        service.llm.replies = iter(messages)
    try:
        result = await finish(service, build_repo)
        repairing = failure in {"compile", "tests"}
        assert result.state == TaskState.FAILED and result.outcome == ("repair_exhausted" if repairing else "validation_failed"), result.error
        assert result.final_result is None
        report = result.build_result
        assert list(report["stages"].values()) == expected, report
        if failure in {"configure", "compile"}:
            assert report["tests"]["total"] is None and report["tests"]["status"] == "not_run"
            assert not any(c["phase"] == "run_tests" for c in report["commands"])
        elif failure == "tests":
            assert report["tests"]["failed"] == 1 and report["tests"]["passed"] == 0
        elif failure == "zero":
            assert report["tests"]["total"] == 0 and report["tests"]["status"] == "no_tests"
        else:
            assert report["tests"]["not_run"] == 1 and report["tests"]["passed"] == 0
        assert (output / "old.xml").exists() and result.steps == (13 if repairing else 4)
        assert result.repair_round == (3 if repairing else 0)
        assert len(result.validation_history) == (4 if repairing else 1)
        assert all(list(r["stages"].values()) == expected for r in result.validation_history)
        print("REAL FAILURE", failure, json.dumps(report, ensure_ascii=False))
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
@pytest.mark.parametrize("phase", ["configure", "build", "test"])
async def test_phase_timeout_and_task_cancel(build_repo, build_config, rg_exe, git_exe, mode, phase):
    cmake = build_repo / "CMakeLists.txt"
    (build_repo / "slow.cmake").write_text('file(WRITE "${CMAKE_CURRENT_LIST_DIR}/phase-started.txt" "started")\nexecute_process(COMMAND "${CMAKE_COMMAND}" -E sleep 60)\n')
    if phase == "configure":
        cmake.write_text('cmake_minimum_required(VERSION 3.24)\ninclude(slow.cmake)\nproject(Slow LANGUAGES NONE)\n')
    elif phase == "build":
        with cmake.open("a") as f:
            f.write('add_custom_command(TARGET counter POST_BUILD COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/slow.cmake")\n')
    else:
        cmake.write_text(cmake.read_text().replace("COMMAND counter)", 'COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/slow.cmake")'))
    config = replace(build_config, **{phase + "_timeout": 1 if mode == "timeout" else 60})
    service, _ = service_for_build(build_repo, config, rg_exe, git_exe)
    try:
        pending = await service.start(str(build_repo), "fix")
        service.approve(pending.task_id, 1)
        task = asyncio.create_task(service.execute(pending.task_id))
        if mode == "cancel":
            async with asyncio.timeout(15):
                while not (build_repo / "phase-started.txt").exists():
                    await asyncio.sleep(0.02)
            assert service.cancel()
        result = await asyncio.wait_for(task, 15)
        assert result.state == (TaskState.CANCELLED if mode == "cancel" else TaskState.FAILED)
        assert result.build_result["commands"][-1]["status"] == ("cancelled" if mode == "cancel" else "timed_out")
        current = {"configure": "cmake_configure", "build": "cmake_build", "test": "run_tests"}[phase]
        stages = result.build_result["stages"]
        assert stages[current] == ("cancelled" if mode == "cancel" else "timed_out")
        assert all(stages[p] == "not_run" for p in BUILD_TOOLS[BUILD_TOOLS.index(current) + 1:])
        assert result.build_result["tests"]["total"] is None
        assert result.task_diff["files"] and "return 0" in (build_repo / "main.cpp").read_text()
        assert "owned process tree terminated" in result.build_result["commands"][-1]["stderr"]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_preset_ambiguity_and_explicit_selection(build_repo, build_config, rg_exe, git_exe):
    config, data = presets(build_repo, build_config)
    data["buildPresets"].append({"name": "second-build", "configurePreset": "selected", "configuration": "Release"})
    (build_repo / "CMakePresets.json").write_text(json.dumps(data))
    service, _ = service_for_build(build_repo, config, rg_exe, git_exe)
    first = await service.start(str(build_repo), "fix")
    assert first.state == TaskState.FAILED and "preset_ambiguous" in first.error
    assert first.steps == 0 and not (build_repo / "out").exists()
    service.close()
    service, _ = service_for_build(build_repo, replace(config, build_preset="second-build"), rg_exe, git_exe)
    try:
        selected = await service.start(str(build_repo), "fix")
        assert selected.state == TaskState.WAITING_APPROVAL, selected.error
        assert selected.build_result["selection"]["build_preset"] == "second-build"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_preset_changed_after_review_requires_new_task(build_repo, build_config, rg_exe, git_exe):
    config, data = presets(build_repo, build_config)
    service, _ = service_for_build(build_repo, config, rg_exe, git_exe)
    try:
        pending = await service.start(str(build_repo), "fix")
        service.approve(pending.task_id, 1)
        data["configurePresets"][0]["binaryDir"] = "${sourceDir}/another build"
        (build_repo / "CMakePresets.json").write_text(json.dumps(data))
        result = await service.execute(pending.task_id)
        assert result.state == TaskState.FAILED and "build_selection_changed" in result.error
        assert not any(c["phase"] == "cmake_configure" for c in result.build_result["commands"])
    finally:
        service.close()


@pytest.mark.parametrize("path", ["..\\escape", "C:\\outside", ".git/build", ".env/build", "build:stream", "NUL", ".", "build./x"])
def test_build_directory_boundary(build_repo, path):
    with pytest.raises(ForgePilotError, match="build_path"):
        safe_build_path(build_repo, path)


def test_build_directory_junction(build_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = build_repo / "linked"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(ForgePilotError, match="build_path"):
            safe_build_path(build_repo, str(junction / "build"))
    finally:
        junction.rmdir()  # Removes only this fixture junction, never its target.


def test_ctest_report_counts_do_not_trust_declared_total(build_repo, build_config):
    pipeline = BuildPipeline(build_repo, build_config, lambda _: None, MemoryTrace(), "test", Redactor())
    pipeline.binary_dir = build_repo / "out"
    pipeline.binary_dir.mkdir()
    report = pipeline.binary_dir / "new.xml"
    report.write_text('<testsuite tests="500"><testcase/><testcase><failure/></testcase><testcase><skipped/></testcase></testsuite>')
    pipeline._test_results(report, "succeeded")
    assert pipeline.report()["tests"] == {"passed": 1, "failed": 1, "total": 3, "not_run": 1, "status": "failed"}
    report.write_text('<!DOCTYPE x [<!ENTITY x "injected">]><testsuite/>')
    with pytest.raises(ForgePilotError, match="test_report"):
        pipeline._test_results(report, "succeeded")


@pytest.mark.asyncio
async def test_command_logs_redacted_and_secrets_not_inherited(build_repo, build_config, monkeypatch):
    key = "fixture-secret-phase4"
    monkeypatch.setenv("FORGEPILOT_API_KEY", key)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    env = build_environment()
    assert not {"FORGEPILOT_API_KEY", "OPENAI_API_KEY", "PYTHONPATH"} & env.keys()
    trace = MemoryTrace(Redactor((key,)))
    pipeline = BuildPipeline(build_repo, build_config, lambda _: None, trace, "test", Redactor((key,)))
    result = await run_build_process([sys.executable, "-I", "-c", f"print({key!r})"], build_repo, timeout=10)
    pipeline._save_command("redaction-test", result)
    assert key not in json.dumps(pipeline.report())
    assert key not in json.dumps([asdict(e) for e in trace.events])
    assert "[REDACTED]" in pipeline.report()["commands"][0]["stdout"]
    report = pipeline.report()
    report["commands"][0]["command"].clear()
    report["commands"][0]["stdout"] = "changed by reader"
    report["stages"].clear()
    assert pipeline.report()["commands"][0]["command"]
    assert "[REDACTED]" in pipeline.report()["commands"][0]["stdout"]
    assert set(pipeline.report()["stages"]) == set(BUILD_TOOLS)
