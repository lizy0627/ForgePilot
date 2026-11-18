"""Task-owned, independently recorded Configure/Build/CTest rounds; no shell API."""

import asyncio
from dataclasses import asdict
import json
import re
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

from .build_process import BuildCancelled, build_environment, run_build_process
from .errors import ForgePilotError
from .path_identity import same_path
from .presets import BuildConfig, Presets, available_presets, safe_build_path
from .repository import is_link
from .tools import ToolResult
from .toolchain import ToolchainProfile, validate_executable

BUILD_TOOLS = ("cmake_configure", "cmake_build", "run_tests")


class BuildPipeline:
    def __init__(self, root: Path, config: BuildConfig, guard, trace, task_id, redactor):
        self.root, self.config, self.guard = root, config, guard
        self.trace, self.task_id, self.redactor = trace, task_id, redactor
        stat = root.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.env = build_environment()
        self.presets = Presets(root, self.env)
        self.profile = ToolchainProfile(config.cmake, config.ctest)
        self.selection = None
        self.binary_dir = None
        self._report = self._new_report(0)

    def _new_report(self, repair_round):
        return {"repair_round": repair_round, "selection": self.selection, "commands": [],
            "stages": {name: "not_run" for name in BUILD_TOOLS},
            "tests": {"passed": None, "failed": None, "total": None, "not_run": None, "status": "not_run"},
            "compile_commands": {"status": "not_checked", "path": None}}

    def begin_round(self, repair_round):
        """Runtime-only reset; never discard the preceding round before archiving it."""
        self.guard("cmake_configure")
        if (type(repair_round) is not int or not 1 <= repair_round <= 3
                or repair_round != self._report["repair_round"] + 1
                or not any(s == "failed" for s in self._report["stages"].values())):
            raise ForgePilotError("build_order", "A new repair validation requires the preceding failed round")
        self._report = self._new_report(repair_round)

    def report(self):
        return self.redactor.data(self._report)

    async def prepare(self):
        available = {kind: set() for kind in ("configure", "build", "test")}
        if self.presets.present:
            result = await self._command("preset_discovery", [self.config.cmake, "--list-presets=all"], 30)
            if result.status != "succeeded":
                raise ForgePilotError("preset_invalid", "CMake could not list valid presets; see command diagnostics")
            available = available_presets(result.stdout)
        self.selection = self.presets.choose(available, self.config)
        self.binary_dir = safe_build_path(self.root, self.selection["binary_dir"])
        self.selection["binary_dir"] = str(self.binary_dir)
        self._report["selection"] = self.selection
        self._validate_preset_policy()
        # Existing configured trees can provide a genuine pre-modification CTest
        # inventory. Never invent a baseline from the first post-edit configure.
        if self.binary_dir.is_dir() and (self.binary_dir / "CTestTestfile.cmake").is_file():
            result = await self._command("ctest_inventory", self._profile_command("ctest_inventory"), 30)
            if result.status != "succeeded":
                raise ForgePilotError("test_inventory", "Cannot capture the approval-time CTest inventory")
            self._report["initial_ctest_names"] = self._inventory_names(result.stdout)
        self.trace.emit(self.task_id, "build_selection", self.report()["selection"])

    @staticmethod
    def _inventory_names(output):
        try:
            document = json.loads(output)
            tests = document["tests"]
            if not isinstance(tests, list) or len(tests) > 100_000:
                raise ValueError()
            names = [test["name"] for test in tests]
            if any(not isinstance(name, str) or not name or len(name) > 4096 for name in names) or len(names) != len(set(names)):
                raise ValueError()
            return names
        except (ValueError, TypeError, KeyError):
            raise ForgePilotError("test_inventory", "CTest returned an invalid approval-time inventory") from None

    def _validate_preset_policy(self):
        if self.selection["source"] != "preset":
            return
        test = self.presets.effective("test", self.selection["test_preset"])
        if test.get("filter"):
            raise ForgePilotError("test_integrity", "CTest preset filters cannot exclude tests from validation")
        build = self.presets.effective("build", self.selection["build_preset"])
        if build.get("targets") or build.get("nativeToolOptions"):
            raise ForgePilotError("test_integrity", "Selected build preset must build all targets without native command overrides")
        configure = self.presets.effective("configure", self.selection["configure_preset"])
        for key, value in configure.get("cacheVariables", {}).items():
            value = value.get("value") if isinstance(value, dict) else value
            if (re.search(r"(?:BUILD_TESTING|ENABLE_TESTS?|BUILD_TESTS?)$", key, re.I)
                    and str(value).upper() in {"FALSE", "OFF", "NO", "0"}):
                raise ForgePilotError("test_integrity", "Selected preset disables existing tests")
            if re.search(r"(?:FLAGS|DEFINITIONS)$", key, re.I) and re.search(r"(?:[-/]D\s*NDEBUG|[-/]D\s*(?:DOCTEST_CONFIG_DISABLE|CATCH_CONFIG_DISABLE))", str(value), re.I):
                raise ForgePilotError("test_integrity", "Selected preset disables test assertions")

    def _profile_command(self, phase, *, report_path=None):
        selection = self.selection or {}
        values = {"source_dir": str(self.root), "build_dir": str(self.binary_dir or ""),
                  "configuration": selection.get("configuration", "Debug"), "jobs": "1",
                  "generator": self.config.generator or "", "architecture": self.config.architecture or "",
                  "make_program": self.config.make_program or "",
                  "configure_preset": selection.get("configure_preset") or "",
                  "build_preset": selection.get("build_preset") or "",
                  "test_preset": selection.get("test_preset") or "", "report_path": str(report_path or "")}
        return self.profile.resolve(phase, values, preset=selection.get("source") == "preset",
                                    multi_config=bool(selection.get("multi_config")),
                                    generator=bool(self.config.generator), architecture=bool(self.config.architecture),
                                    make_program=bool(self.config.make_program)).argv

    def _paths(self):
        stat = self.root.stat()
        if (is_link(self.root.lstat()) or not same_path(self.root.resolve(strict=True), self.root)
                or (stat.st_dev, stat.st_ino) != self.identity):
            raise ForgePilotError("build_path", "Repository root changed after task creation")
        if self.binary_dir is not None:
            safe_build_path(self.root, str(self.binary_dir))
        self.presets.unchanged()

    def _artifact(self, name: str) -> Path:
        self._paths()
        path = self.binary_dir / name
        if path.exists() or path.is_symlink():
            stat = path.lstat()
            if is_link(stat) or not path.is_file() or stat.st_nlink != 1:
                raise ForgePilotError("build_artifact", "Build metadata must be regular files without links")
        return path

    def _cache(self):
        path = self._artifact("CMakeCache.txt")
        if not path.exists():
            return {}
        if path.stat().st_size > 4_194_304:
            raise ForgePilotError("build_cache", "CMake cache exceeds the metadata limit")
        cache = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(("#", "//")) and ":" in line and "=" in line:
                key, value = line.split("=", 1)
                cache[key.split(":", 1)[0]] = value
        if not same_path(Path(cache.get("CMAKE_HOME_DIRECTORY", "")).resolve(), self.root):
            raise ForgePilotError("build_cache", "Existing CMake cache belongs to another source directory")
        if not same_path(Path(cache.get("CMAKE_CACHEFILE_DIR", "")).resolve(), self.binary_dir):
            raise ForgePilotError("build_cache", "CMake cache directory differs from the selected binaryDir")
        return cache

    async def _command(self, phase, command, timeout):
        if phase not in {*BUILD_TOOLS, "preset_discovery", "ctest_inventory"}:
            raise ForgePilotError("command_denied", "Only predefined validator phases can launch commands")
        self._paths()
        kind = "ctest" if phase in {"run_tests", "ctest_inventory"} else "cmake"
        validate_executable(command[0], expected=kind)
        try:
            result = await run_build_process(command, self.root, timeout=timeout, env=self.env)
        except BuildCancelled as exc:
            self._save_command(phase, exc.result)
            raise
        self._save_command(phase, result)
        return result

    def _save_command(self, phase, result):
        data = self.redactor.data({"phase": phase, "repair_round": self._report["repair_round"], **asdict(result)})
        self._report["commands"].append(data)
        self.trace.emit(self.task_id, "build_command_finished", data)

    async def execute(self, phase: str) -> ToolResult:
        self.guard(phase)
        index = BUILD_TOOLS.index(phase)
        stages = self._report["stages"]
        if stages[phase] != "not_run" or any(stages[p] != "succeeded" for p in BUILD_TOOLS[:index]):
            raise ForgePilotError("build_order", "Build phases run once in order, after successful prerequisites")
        stages[phase] = "running"
        try:
            self._paths()
            if phase == "cmake_configure":
                self._cache()  # Reject stale caches from another source tree before executing CMake.
                command = self._configure_command()
                timeout = self.config.configure_timeout
            elif phase == "cmake_build":
                command = self._profile_command(phase)
                timeout = self.config.build_timeout
            else:
                report_path = self._artifact("forgepilot-ctest-" + uuid4().hex + ".xml")
                if report_path.exists():
                    raise ForgePilotError("test_report", "Test report must be new for this invocation")
                command = self._profile_command(phase, report_path=report_path)
                timeout = self.config.test_timeout
            result = await self._command(phase, command, timeout)
            if phase == "cmake_configure" and result.status == "succeeded":
                self._configured()
            if phase == "run_tests":
                self._test_results(report_path, result.status)
            success = result.status == "succeeded" and (phase != "run_tests" or self._report["tests"]["status"] == "passed")
            stages[phase] = "succeeded" if success else result.status if result.status != "succeeded" else "failed"
            error = None if success else f"{phase}_failed: {stages[phase]}; see command output and test summary"
            if not success:
                self._report["error"] = error
            return ToolResult(success=success, output=json.dumps(self.report(), ensure_ascii=False),
                              error=error, duration_ms=result.duration_ms)
        except asyncio.CancelledError:
            stages[phase] = "cancelled"
            if phase == "run_tests":
                self._report["tests"]["status"] = "cancelled"
            raise
        except ForgePilotError as exc:
            stages[phase] = "failed"
            self._report["error"] = self.redactor.text(str(exc))
            raise
        except Exception:
            stages[phase] = "failed"
            self._report["error"] = "build_internal: Validation infrastructure failed"
            raise ForgePilotError("build_internal", "Validation infrastructure failed") from None

    def _configure_command(self):
        return self._profile_command("cmake_configure")

    def _configured(self):
        cache = self._cache()
        generator = cache.get("CMAKE_GENERATOR", "")
        if not generator:
            raise ForgePilotError("build_cache", "Successful configure did not produce a valid CMake cache")
        multi = bool(cache.get("CMAKE_CONFIGURATION_TYPES"))
        selected = self.selection["configuration"]
        if multi and selected not in cache["CMAKE_CONFIGURATION_TYPES"].split(";"):
            raise ForgePilotError("build_configuration", "Selected configuration is absent from generated configurations")
        if not multi and cache.get("CMAKE_BUILD_TYPE") != selected:
            raise ForgePilotError("build_configuration", "Generated build type differs from selected configuration")
        if self.selection["generator"] and generator != self.selection["generator"]:
            raise ForgePilotError("build_configuration", "Generated generator differs from selected preset/configuration")
        self.selection.update(generator=generator, multi_config=multi)
        supported = "Ninja" in generator or "Makefiles" in generator
        if not supported:
            self._report["compile_commands"] = {"status": "unsupported_generator", "path": None,
                "reason": f"{generator} does not support CMAKE_EXPORT_COMPILE_COMMANDS; use an explicitly configured separate Ninja analysis directory, otherwise symbol tools fall back to rg"}
        else:
            path = self._artifact("compile_commands.json")
            self._report["compile_commands"] = {"status": "available_after_configure" if path.is_file() else "not_generated",
                                                "path": str(path) if path.is_file() else None}

    def _test_results(self, path, process_status):
        summary = self._report["tests"]
        summary["status"] = process_status if process_status != "succeeded" else "report_missing"
        path = self._artifact(path.name)
        if not path.exists():
            return
        if path.stat().st_size > 4_194_304:
            raise ForgePilotError("test_report", "CTest JUnit report exceeds 4 MiB")
        try:
            data = path.read_bytes()
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError()
            root = ET.fromstring(data)
            if root.tag not in {"testsuite", "testsuites"}:
                raise ValueError()
            cases = list(root.iter("testcase"))
            self._report["test_names"] = [c.get("name", "") for c in cases]
            failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
            not_run = sum(c.find("failure") is None and c.find("error") is None and
                          (c.find("skipped") is not None or c.get("status", "run").lower() in {"notrun", "disabled"}) for c in cases)
            total = len(cases)
            summary.update(passed=total - failed - not_run, failed=failed, total=total, not_run=not_run)
            summary["status"] = ("no_tests" if total == 0 else "failed" if failed else "not_run" if not_run
                                 else "passed" if process_status == "succeeded" else process_status)
        except (ET.ParseError, ValueError):
            raise ForgePilotError("test_report", "CTest returned an invalid JUnit report") from None
