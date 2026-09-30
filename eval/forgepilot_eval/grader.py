"""Executable oracle outside the Agent's repository, using fresh build directories."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from uuid import uuid4

from forgepilot.build_process import build_environment, run_build_process

from .manifest import asset, inventory


def read_junit(path, expected_name, process_passed):
    if not path.is_file() or path.stat().st_size > 1_048_576:
        return {"status": "failed", "count": 0, "reason": "missing_or_oversized_junit"}
    try:
        data = path.read_bytes()
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data: raise ValueError()
        root = ET.fromstring(data)
        if root.tag not in {"testsuite", "testsuites"}: raise ValueError()
        cases = list(root.iter("testcase"))
        good = len(cases) == 1 and cases[0].get("name") == expected_name
        good = good and all(c.find("failure") is None and c.find("error") is None and c.find("skipped") is None
            and c.get("status", "run").lower() not in {"notrun", "disabled"} for c in cases)
        return {"status": "passed" if good and process_passed else "failed", "count": len(cases),
                "reason": "verified_junit" if good and process_passed else "failed_skipped_zero_or_unexpected_tests"}
    except (ValueError, ET.ParseError):
        return {"status": "failed", "count": 0, "reason": "invalid_junit"}


def grade_analysis(answer, expected):
    try:
        value = json.loads(answer)
        if not isinstance(value, dict) or set(value) != {"files", "symbols", "relations"}: return False
        if any(not isinstance(value[k], list) for k in value): return False
        for key in ("files", "symbols"):
            if len(value[key]) != len(set(value[key])) or set(value[key]) != set(expected[key]): return False
        edges = value["relations"]
        if any(not isinstance(e, dict) or set(e) != {"caller", "callee"} for e in edges): return False
        actual = {(e["caller"], e["callee"]) for e in edges}
        return len(actual) == len(edges) and actual == {(e["caller"], e["callee"]) for e in expected["relations"]}
    except (ValueError, TypeError, KeyError):
        return False


def initial_matches(task, result):
    if task.analysis: return True
    if result["configure_status"] != "passed": return False
    if task.initial_state.expected == "build_failed": return result["build_status"] == "failed"
    return result["build_status"] == "passed" and result["test_status"] == "failed" and result["test_count"] > 0


def check_scope(before, after, task):
    changes = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    allowed = {p.casefold() for p in task.allowed_files}
    forbidden = [p for p in changes if p.casefold() not in allowed]
    missing = [p for p in task.success.required_changed if p not in changes]
    return {"changed": changes, "forbidden": forbidden, "missing_required": missing,
            "valid": not forbidden and not missing}


class Grader:
    def __init__(self, dataset, cmake, ctest, *, generator="Visual Studio 17 2022", ninja=None, qt_kit=None, timeout=120):
        self.dataset, self.cmake, self.ctest = dataset, cmake, ctest
        self.generator, self.ninja, self.qt_kit, self.timeout = generator, ninja, qt_kit, timeout

    def configure_options(self):
        options = ["-G", self.generator]
        if self.generator.startswith("Visual Studio"): options += ["-A", "x64"]
        else: options += ["-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_MAKE_PROGRAM=" + self.ninja]
        if self.qt_kit: options += ["-DQt6_DIR=" + str(Path(self.qt_kit) / "lib/cmake/Qt6")]
        return options

    def write_presets(self, repo):
        configure = {"name": "eval", "generator": self.generator, "binaryDir": "${sourceDir}/.forgepilot/build",
                     "cacheVariables": {"CMAKE_BUILD_TYPE": "Debug"}}
        if self.generator.startswith("Visual Studio"): configure["architecture"] = "x64"
        else: configure["cacheVariables"]["CMAKE_MAKE_PROGRAM"] = self.ninja
        if self.qt_kit: configure["cacheVariables"]["Qt6_DIR"] = str(Path(self.qt_kit) / "lib/cmake/Qt6")
        data = {"version": 6, "configurePresets": [configure],
                "buildPresets": [{"name": "eval-build", "configurePreset": "eval", "configuration": "Debug"}],
                "testPresets": [{"name": "eval-test", "configurePreset": "eval", "configuration": "Debug"}]}
        (repo / "CMakeUserPresets.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def run(self, task, repo, directory, *, baseline=False):
        directory.mkdir(parents=True, exist_ok=False)
        source, build = directory / "protected", directory / "build"
        source.mkdir()
        if is_unsafe_path(repo): raise ValueError("Unsafe grader candidate")
        text = 'cmake_minimum_required(VERSION 3.24)\nproject(ForgePilotProtectedOracle LANGUAGES CXX)\n'
        text += 'set(CMAKE_CXX_STANDARD 20)\nset(CMAKE_CXX_STANDARD_REQUIRED ON)\nenable_testing()\n'
        if task.qt: text += 'find_package(Qt6 REQUIRED COMPONENTS Core Test)\nset(CMAKE_AUTOMOC ON)\n'
        text += 'add_subdirectory("${CANDIDATE}" candidate)\n'
        name = "public_test" if baseline else "forgepilot_oracle"
        completion_token = "FORGEPILOT_ORACLE_DONE_" + uuid4().hex
        target = "smoke" if baseline else "forgepilot_oracle"
        if not baseline:
            shutil.copyfile(asset(self.dataset, task.success.oracle), source / "oracle.cpp")
            text += 'add_executable(forgepilot_oracle oracle.cpp)\ntarget_link_libraries(forgepilot_oracle PRIVATE subject)\n'
            text += f'target_compile_definitions(forgepilot_oracle PRIVATE FORGEPILOT_ORACLE_TOKEN="{completion_token}")\n'
            if task.qt:
                text += 'target_link_libraries(forgepilot_oracle PRIVATE Qt6::Test)\n'
                for lib in ("Core", "Test"):
                    text += f'add_custom_command(TARGET forgepilot_oracle POST_BUILD COMMAND ${{CMAKE_COMMAND}} -E copy_if_different $<TARGET_FILE:Qt6::{lib}> $<TARGET_FILE_DIR:forgepilot_oracle>)\n'
            text += 'add_test(NAME forgepilot_oracle COMMAND forgepilot_oracle)\nset_tests_properties(forgepilot_oracle PROPERTIES TIMEOUT 15)\n'
        (source / "CMakeLists.txt").write_text(text, encoding="utf-8")
        protected_before = inventory(source)
        result = {"configure_status": "not_run", "build_status": "not_run", "test_status": "not_run",
                  "test_count": 0, "commands": [], "protected_unchanged": True}
        report = build / "oracle.xml"
        commands = [
            ("configure", [self.cmake, "-S", str(source), "-B", str(build), "-DCANDIDATE=" + str(repo), *self.configure_options()]),
            ("build", [self.cmake, "--build", str(build), "--config", "Debug", "--target", target]),
            ("test", [self.ctest, "--test-dir", str(build), "-C", "Debug", "-R", "^" + name + "$",
                      "--verbose", "--output-on-failure", "--no-tests=error", "--timeout", "15", "--output-junit", str(report)])]
        for phase, argv in commands:
            command = await run_build_process(argv, directory, timeout=self.timeout, env=build_environment())
            result["commands"].append({"phase": phase, **asdict(command)})
            result[phase + "_status"] = "passed" if command.status == "succeeded" else "failed"
            if phase == "configure" and command.status != "succeeded": result["build_status"] = "failed"
            if phase == "test":
                tests = read_junit(report, name, command.status == "succeeded")
                result.update(test_status=tests["status"], test_count=tests["count"], test_reason=tests["reason"])
                if not baseline and completion_token not in command.stdout:
                    result.update(test_status="failed", test_reason="protected_oracle_did_not_complete")
            if inventory(source) != protected_before:
                result.update(protected_unchanged=False, test_status="failed", test_reason="protected_grader_modified")
                break
            if command.status != "succeeded": break
        return result


def is_unsafe_path(path):
    # CMake receives a single argv value, never shell text. Reject list/newline injection.
    return any(c in str(path) for c in (';', '\n', '\r', '"'))
