"""Executable oracle outside the Agent's repository, using fresh build directories."""
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from uuid import uuid4

from forgepilot.build_process import build_environment, run_build_process

from .manifest import asset, inventory
from .isolation import AppContainerDomain, IsolationConfig, IsolationError, assert_plain_tree
from .external_probe import run_isolated_probe


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
    def __init__(self, dataset, cmake, ctest, *, generator="Visual Studio 17 2022", ninja=None, qt_kit=None, timeout=120,
                 isolation: IsolationConfig | None = None):
        self.dataset, self.cmake, self.ctest = dataset, cmake, ctest
        self.generator, self.ninja, self.qt_kit, self.timeout = generator, ninja, qt_kit, timeout
        self.isolation = isolation

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
        if self.isolation is not None:
            return await self.run_isolated(task, repo, directory, baseline=baseline)
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
                  "public_test_status": "not_run", "oracle_status": "not_applicable" if baseline else "not_run",
                  "oracle_test_status": "not_applicable" if baseline else "not_run",
                  "test_count": 0, "public_test_count": 0, "oracle_test_count": 0,
                  "commands": [], "protected_unchanged": True}
        commands = [
            ("configure", [self.cmake, "-S", str(source), "-B", str(build), "-DCANDIDATE=" + str(repo), *self.configure_options()]),
            ("public_build", [self.cmake, "--build", str(build), "--config", "Debug", "--target", "smoke"]),
            ("public_test", [self.ctest, "--test-dir", str(build), "-C", "Debug", "-R", "^public_test$",
                             "--no-tests=error", "--timeout", "15", "--output-junit", str(build / "public.xml")])]
        if not baseline:
            commands += [
                ("oracle_build", [self.cmake, "--build", str(build), "--config", "Debug", "--target", target]),
                ("oracle_test", [self.ctest, "--test-dir", str(build), "-C", "Debug", "-R", "^forgepilot_oracle$",
                                 "--verbose", "--no-tests=error", "--timeout", "15", "--output-junit", str(build / "oracle.xml")])]
        for phase, argv in commands:
            command = await run_build_process(argv, directory, timeout=self.timeout, env=build_environment())
            # Oracle stdout/stderr, command lines and absolute paths never enter Agent-facing artifacts.
            result["commands"].append({"phase": phase, "status": command.status,
                                       "returncode": command.returncode, "duration_ms": command.duration_ms})
            if phase == "configure": result["configure_status"] = "passed" if command.status == "succeeded" else "failed"
            if phase.endswith("build"):
                result["build_status"] = "passed" if command.status == "succeeded" else "failed"
            if phase.endswith("test"):
                expected = "public_test" if phase == "public_test" else "forgepilot_oracle"
                report = build / ("public.xml" if phase == "public_test" else "oracle.xml")
                tests = read_junit(report, expected, command.status == "succeeded")
                if phase == "oracle_test" and completion_token not in command.stdout:
                    tests = {**tests, "status": "failed", "reason": "protected_oracle_did_not_complete"}
                result[phase + "_status"] = tests["status"]
                result[phase + "_count"] = tests["count"]
                result["test_count"] += tests["count"]
                result["test_reason"] = tests["reason"]
                result["test_status"] = tests["status"]
            if inventory(source) != protected_before:
                result.update(protected_unchanged=False, test_status="failed", test_reason="protected_grader_modified")
                break
            if (command.status != "succeeded" and phase != "public_test") or (
                    phase == "oracle_test" and result[phase + "_status"] != "passed"):
                break
        if not baseline:
            result["test_status"] = ("passed" if result["public_test_status"] == "passed"
                                     and result["oracle_test_status"] == "passed" else "failed")
            result["oracle_status"] = result["oracle_test_status"]
            if result["public_test_status"] != "passed": result["test_reason"] = "public_ctest_failed"
            elif result["oracle_test_status"] == "not_run": result["test_reason"] = "oracle_not_run"
        if result["configure_status"] != "passed": result["build_status"] = "failed"
        return result

    async def run_isolated(self, task, repo, directory, *, baseline=False):
        """Three domains: candidate build, trusted Oracle compile, Oracle execution.

        Candidate CMake is never evaluated in the domain that can read Oracle
        source; the execution domain cannot read either candidate repo or source.
        """
        if task.analysis or self.isolation is None:
            raise IsolationError("Executable isolated grader requires a modification task")
        if is_unsafe_path(repo):
            raise IsolationError("Unsafe grader candidate path")
        repo_root = repo.resolve(strict=True)
        directory_root = directory.parent.resolve(strict=True) / directory.name
        if (directory_root == repo_root or directory_root.is_relative_to(repo_root)
                or repo_root.is_relative_to(directory_root)):
            raise IsolationError("Candidate and protected grader directories overlap")
        config = self.isolation
        directory.mkdir(parents=True, exist_ok=False)
        public_build = directory / "public-build"
        public_build.mkdir()
        config.validate_for(repo, public_build)
        result = {"configure_status": "not_run", "build_status": "not_run", "test_status": "not_run",
                  "public_test_status": "not_run", "oracle_status": "not_applicable" if baseline else "not_run",
                  "oracle_test_status": "not_applicable" if baseline else "not_run",
                  "test_count": 0, "public_test_count": 0, "oracle_test_count": 0,
                  "commands": [], "protected_unchanged": True}

        async def phase(domain, name, argv, cwd):
            command = await domain.run(argv, cwd, timeout=self.timeout, env=config.env)
            stdout_file, stderr_file = f"{name}.stdout", f"{name}.stderr"
            (directory / stdout_file).write_text(command.stdout, encoding="utf-8")
            (directory / stderr_file).write_text(command.stderr, encoding="utf-8")
            result["commands"].append({"phase": name, "argv": argv, "status": command.status,
                "returncode": command.returncode, "duration_ms": command.duration_ms,
                "stdout_file": stdout_file, "stderr_file": stderr_file})
            return command

        with AppContainerDomain(config.launcher) as candidate:
            config.grant_read_only(candidate, qt=task.qt)
            candidate.grant(repo, writable=True)
            candidate.grant(public_build, writable=True)
            configured = await phase(candidate, "configure",
                [str(config.cmake), "-S", str(repo), "-B", str(public_build),
                 *config.cmake_options(qt=task.qt)], repo)
            result["configure_status"] = "passed" if configured.status == "succeeded" else "failed"
            if configured.status != "succeeded":
                result.update(build_status="failed", test_status="failed", test_reason="public_configure_failed")
                return result
            built = await phase(candidate, "public_build", [str(config.cmake), "--build",
                str(public_build), "--config", "Debug", "--target", "smoke",
                *(["--verbose"] if task.qt else [])], repo)
            result["build_status"] = "passed" if built.status == "succeeded" else "failed"
            if built.status != "succeeded":
                result.update(test_status="failed", test_reason="public_build_failed")
                return result
            tested = await phase(candidate, "public_test", [str(config.ctest), "--test-dir",
                str(public_build), "-C", "Debug", "-R", "^public_test$", "--no-tests=error",
                "--timeout", "15", "--output-junit", str(public_build / "public.xml")], repo)
            public = read_junit(public_build / "public.xml", "public_test", tested.status == "succeeded")
            result.update(public_test_status=public["status"], public_test_count=public["count"],
                          test_count=public["count"], test_status=public["status"], test_reason=public["reason"])
            if baseline:
                assert_plain_tree(repo)
                assert_plain_tree(public_build)
                return result
            if public["status"] != "passed":
                assert_plain_tree(repo)
                assert_plain_tree(public_build)
                result["test_reason"] = "public_ctest_failed"
                return result

        assert_plain_tree(repo)
        assert_plain_tree(public_build)
        libs = [p for p in public_build.rglob("*") if p.is_file() and p.name.casefold() == "subject.lib"]
        if len(libs) != 1 or is_unsafe_path(libs[0]):
            result.update(test_status="failed", test_reason="subject_library_missing_or_ambiguous")
            return result
        source, build = directory / "oracle-src", directory / "oracle-build"
        source.mkdir()
        build.mkdir()
        shutil.copyfile(asset(self.dataset, task.success.oracle), source / "oracle.cpp")
        token = "FORGEPILOT_ORACLE_DONE_" + uuid4().hex
        cmake_text = 'cmake_minimum_required(VERSION 3.24)\nproject(ForgePilotProtectedOracle LANGUAGES CXX)\n'
        cmake_text += 'set(CMAKE_CXX_STANDARD 20)\nset(CMAKE_CXX_STANDARD_REQUIRED ON)\n'
        if task.qt:
            cmake_text += 'find_package(Qt6 REQUIRED COMPONENTS Core Test)\nset(CMAKE_AUTOMOC ON)\n'
        cmake_text += 'add_executable(forgepilot_oracle oracle.cpp)\n'
        cmake_text += f'target_include_directories(forgepilot_oracle PRIVATE "{(repo / "src").as_posix()}")\n'
        cmake_text += f'target_link_libraries(forgepilot_oracle PRIVATE "{libs[0].as_posix()}")\n'
        cmake_text += f'target_compile_definitions(forgepilot_oracle PRIVATE FORGEPILOT_ORACLE_TOKEN="{token}")\n'
        if task.qt:
            cmake_text += 'target_link_libraries(forgepilot_oracle PRIVATE Qt6::Core Qt6::Test)\n'
            for lib in ("Core", "Test"):
                cmake_text += f'add_custom_command(TARGET forgepilot_oracle POST_BUILD COMMAND ${{CMAKE_COMMAND}} -E copy_if_different $<TARGET_FILE:Qt6::{lib}> $<TARGET_FILE_DIR:forgepilot_oracle>)\n'
        cmake_text += 'enable_testing()\nadd_test(NAME forgepilot_oracle COMMAND forgepilot_oracle)\n'
        cmake_text += 'set_tests_properties(forgepilot_oracle PROPERTIES TIMEOUT 15)\n'
        (source / "CMakeLists.txt").write_text(cmake_text, encoding="utf-8")
        protected_before = inventory(source)
        with AppContainerDomain(config.launcher) as compiler:
            config.grant_read_only(compiler, qt=task.qt)
            compiler.grant(repo, writable=False)
            compiler.grant(source, writable=False)
            compiler.grant(build, writable=True)
            compiler.grant(public_build, writable=False)
            configured = await phase(compiler, "oracle_configure", [str(config.cmake), "-S", str(source),
                "-B", str(build), *config.cmake_options(qt=task.qt)], build)
            if configured.status != "succeeded":
                result.update(test_status="failed", test_reason="oracle_configure_failed")
                return result
            built = await phase(compiler, "oracle_build", [str(config.cmake), "--build", str(build),
                "--config", "Debug", "--target", "forgepilot_oracle"], build)
            if built.status != "succeeded":
                result.update(test_status="failed", test_reason="oracle_build_failed")
                return result
        if inventory(source) != protected_before:
            result.update(protected_unchanged=False, test_status="failed", test_reason="protected_grader_modified")
            return result
        with AppContainerDomain(config.launcher) as executable:
            config.grant_read_only(executable, qt=task.qt)
            executable.grant(build, writable=True)
            tested = await phase(executable, "oracle_test", [str(config.ctest), "--test-dir", str(build),
                "-C", "Debug", "-R", "^forgepilot_oracle$", "--verbose", "--no-tests=error",
                "--timeout", "15", "--output-junit", str(build / "oracle.xml")], build)
            oracle = read_junit(build / "oracle.xml", "forgepilot_oracle", tested.status == "succeeded")
            if token not in tested.stdout:
                oracle = {**oracle, "status": "failed", "reason": "protected_oracle_did_not_complete"}
        if inventory(source) != protected_before:
            result.update(protected_unchanged=False, test_status="failed", test_reason="protected_grader_modified")
            return result
        result.update(oracle_test_status=oracle["status"], oracle_status=oracle["status"],
            oracle_test_count=oracle["count"], test_count=result["test_count"] + oracle["count"],
            test_status=oracle["status"], test_reason=oracle["reason"])
        if result["test_status"] == "passed":
            # The CTest result is necessary but cannot establish assertion
            # completion: candidate initializers execute in that same process.
            # Only a complete response checked outside the candidate process
            # may authorize success for a task with a fixed behavioral contract.
            verified = False
            if task.id != "analysis-01":
                verified = await run_isolated_probe(task.id, config, directory,
                    candidate_include=repo / "src", candidate_library=libs[0], timeout=self.timeout,
                    evidence_dir=directory)
            if not verified:
                result.update(test_status="failed", oracle_status="failed",
                              oracle_test_status="failed",
                              test_reason="external_oracle_failed")
        return result


def is_unsafe_path(path):
    # CMake receives a single argv value, never shell text. Reject list/newline injection.
    return any(c in str(path) for c in (';', '\n', '\r', '"'))
