"""Run locked Python Oracles with every candidate command inside AppContainers.

The locked Oracle scripts remain unmodified. Their ``common.command`` hook is
replaced only inside a dedicated evaluator thread while each script runs.
Generated Oracle sources are compiled in a separate domain from candidate
CMake; the resulting executable is tested in a third domain without source
access. Only finite status and command metadata leave this adapter.
"""

import asyncio
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from .isolation import AppContainerDomain, IsolationConfig, IsolationError, assert_plain_tree, validate_environment
from .external_probe import run_isolated_probe
from forgepilot.build_process import build_environment, run_build_process


class RealOracleAdapter:
    def __init__(self, config: IsolationConfig, oracle_script: Path, repository: Path,
                 *, timeout: float = 180, evidence_dir: Path | None = None):
        self.config = config
        self.oracle_script = oracle_script.resolve(strict=True)
        self.repository = repository.resolve(strict=True)
        self.timeout = timeout
        self.evidence_dir = evidence_dir.resolve() if evidence_dir is not None else None
        if self.evidence_dir is not None:
            if (self.evidence_dir == self.repository or
                    self.evidence_dir.is_relative_to(self.repository) or
                    self.repository.is_relative_to(self.evidence_dir)):
                raise IsolationError("Oracle evidence overlaps candidate repository")
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.commands = []
        self.phase = "init"

    async def run(self) -> dict:
        # A killable evaluator child belongs to the outer Job Object. A thread
        # would survive cancellation and could keep spending model budget or
        # running candidate commands after the caller has stopped waiting.
        validate_environment(self.config.env)
        self.config.validate_for(self.repository)
        with TemporaryDirectory(prefix="forgepilot-real-oracle-adapter-") as dirname:
            scratch = Path(dirname)
            config_path = scratch / "config.json"
            config_path.write_text(json.dumps({
                "launcher": str(self.config.launcher), "cmake": str(self.config.cmake),
                "ctest": str(self.config.ctest), "make": str(self.config.make),
                "compiler": str(self.config.compiler), "rc": str(self.config.rc),
                "mt": str(self.config.mt), "qt_kit": str(self.config.qt_kit) if self.config.qt_kit else None
                , "expected_sha256": self.config.expected_sha256
                , "read_only_roots": [str(p) for p in self.config.read_only_roots]
                }, ensure_ascii=True), encoding="utf-8")
            result = await run_build_process([sys.executable, "-I",
                str(Path(__file__).with_name("real_oracle_worker.py")), str(config_path),
                str(self.oracle_script), str(self.repository), str(self.timeout),
                str(self.evidence_dir) if self.evidence_dir is not None else ""],
                scratch, timeout=max(600, self.timeout * 8), env=self.config.env)
            if result.status != "succeeded":
                return {"status": "failed", "reason": "evaluator_worker_failed", "commands": []}
            try:
                data = json.loads(result.stdout)
                if (type(data) is not dict or data.get("status") not in {"passed", "failed"}
                        or type(data.get("commands")) is not list):
                    raise ValueError()
                self.commands = data["commands"]
                return data
            except (ValueError, TypeError):
                return {"status": "failed", "reason": "invalid_evaluator_worker_result", "commands": []}

    def _run_sync(self) -> dict:
        self.phase = "load_common"
        common_path = self.oracle_script.with_name("common.py")
        if not common_path.is_file() or not (self.repository / "CMakeLists.txt").is_file():
            raise IsolationError("Locked Oracle or candidate repository missing")
        old_common = sys.modules.get("common")
        common_spec = importlib.util.spec_from_file_location("common", common_path)
        common = importlib.util.module_from_spec(common_spec)
        common_spec.loader.exec_module(common)
        sys.modules["common"] = common
        original_command = common.command
        builds: dict[Path, str] = {}
        try:
            self.phase = "profiles"
            with ExitStack() as stack:
                domains = {name: stack.enter_context(AppContainerDomain(self.config.launcher))
                           for name in ("candidate", "compiler", "executable")}
                for domain in domains.values():
                    self.config.grant_read_only(domain)
                self.phase = "grant_candidate"
                domains["candidate"].grant(self.repository, writable=True)
                self.phase = "grant_compiler"
                domains["compiler"].grant(self.repository, writable=False)

                def command(argv, cwd, timeout=180):
                    tool = Path(argv[0]).name.casefold()
                    if tool not in {"cmake.exe", "cmake", "ctest.exe", "ctest"}:
                        raise IsolationError("Locked Oracle requested an unrecognised process")
                    args = list(argv)
                    if "-S" in args and "-B" in args:
                        source = Path(args[args.index("-S") + 1]).resolve(strict=True)
                        build = Path(args[args.index("-B") + 1]).resolve()
                        if source == self.repository:
                            role = "candidate"
                        elif source.parent != self.repository and (source / "oracle.cpp").is_file():
                            role = "compiler"
                            self.phase = "grant_oracle_source"
                            domains[role].grant(source, writable=False)
                        else:
                            raise IsolationError("Unknown CMake source root in locked Oracle")
                        build.mkdir(parents=True, exist_ok=True)
                        self.config.validate_for(self.repository, build)
                        self.phase = "grant_build"
                        domains[role].grant(build, writable=True)
                        builds[build] = role
                        # The locked scripts choose the generator; the private
                        # toolchain options close every implicit host lookup.
                        args.extend(self.config.cmake_options()[2:])
                    elif "--build" in args:
                        build = Path(args[args.index("--build") + 1]).resolve(strict=True)
                        role = builds.get(build)
                        if role is None: raise IsolationError("Unknown CMake build root")
                    elif "--test-dir" in args:
                        build = Path(args[args.index("--test-dir") + 1]).resolve(strict=True)
                        previous = builds.get(build)
                        if previous is None: raise IsolationError("Unknown CTest build root")
                        role = "executable" if previous == "compiler" else "candidate"
                        if role == "executable": domains[role].grant(build, writable=True)
                    else:
                        raise IsolationError("Unrecognised locked Oracle command shape")
                    workdir = self.repository if role == "candidate" else build
                    self.phase = "run_command"
                    result = asyncio.run(domains[role].run(args, workdir,
                        timeout=min(timeout, self.timeout), env=self.config.env))
                    if self.evidence_dir is not None:
                        number = len(self.commands) + 1
                        (self.evidence_dir / f"command-{number:02d}.stdout").write_text(
                            result.stdout, encoding="utf-8")
                        (self.evidence_dir / f"command-{number:02d}.stderr").write_text(
                            result.stderr, encoding="utf-8")
                    output = (result.stderr + result.stdout).casefold()
                    signatures = ("c1902", "c1083", "lnk1104", "access is denied",
                        "no such file or directory", "cmake_policy_version_minimum",
                        "compatibility with cmake", "could not find", "failed to run")
                    signature = next((s for s in signatures if s in output), "unclassified")
                    raw_lines = (result.stderr + result.stdout).splitlines()
                    compiler_error = next((line.strip() for line in raw_lines if "C1902" in line), "")
                    self.commands.append({"domain": role, "tool": tool,
                        "status": result.status, "returncode": result.returncode,
                        "duration_ms": result.duration_ms,
                        "debug_flag_override": any(x.startswith("-DCMAKE_CXX_FLAGS_DEBUG=/Z7") for x in args),
                        "diagnostic_mentions_zi": "/Zi" in result.stderr or "/Zi" in result.stdout,
                        "diagnostic_mentions_z7": "/Z7" in result.stderr or "/Z7" in result.stdout,
                        "compiler_error": compiler_error[-220:] if compiler_error else None,
                        "failure_signature": signature if result.status != "succeeded" else None})
                    if role == "candidate":
                        # Locked Oracle Python may inspect generated files after
                        # this hook returns. Validate the entire tree first so
                        # a candidate-created junction cannot redirect that read.
                        assert_plain_tree(self.repository)
                        assert_plain_tree(build)
                    if result.status == "timed_out": raise common.OracleTimeout()
                    return result.status == "succeeded"

                common.command = command
                self.phase = "load_script"
                script_spec = importlib.util.spec_from_file_location(
                    "forgepilot_locked_oracle_" + uuid4().hex, self.oracle_script)
                script = importlib.util.module_from_spec(script_spec)
                script_spec.loader.exec_module(script)
                args = SimpleNamespace(repository=self.repository, cmake=str(self.config.cmake),
                                       ctest=str(self.config.ctest), generator="NMake Makefiles")
                self.phase = "run_script"
                try:
                    if hasattr(script, "check"):
                        status = script.check(args)
                    elif hasattr(script, "SOURCE") and hasattr(script, "run_cpp_oracle"):
                        status = script.run_cpp_oracle(args, script.SOURCE)
                    else:
                        raise IsolationError("Unknown locked Oracle entry point")
                except common.OracleTimeout:
                    status = "timeout"
                except Exception:
                    status = "execution_error"
                if status == "passed":
                    contracts = {"bug_unmatched_short.py": "real-bug-01",
                                 "bug_float_trailing.py": "real-bug-03",
                                 "feature_ordered_arguments.py": "real-feature-01",
                                 "feature_string_view.py": "real-feature-02"}
                    if self.oracle_script.name in contracts:
                        with TemporaryDirectory(prefix="forgepilot-external-probe-") as probe_dir:
                            verified = asyncio.run(run_isolated_probe(contracts[self.oracle_script.name], self.config,
                                Path(probe_dir), candidate_include=self.repository / (
                                    "src" if self.oracle_script.name == "feature_string_view.py" else "include"),
                                candidate_source=self.repository / "src/pugixml.cpp"
                                    if self.oracle_script.name == "feature_string_view.py" else None,
                                timeout=self.timeout))
                        status = "passed" if verified else "external_oracle_failed"
                    elif self.oracle_script.name == "bug_pkgconfig_absolute.py":
                        # This Oracle's assertions inspect generated package
                        # files in the trusted Python worker, outside all
                        # candidate CMake processes.
                        status = "passed"
                    else:
                        status = "in_process_oracle_integrity_unverified"
                return {"status": "passed" if status == "passed" else "failed",
                        "reason": status if status in {"in_process_oracle_integrity_unverified", "tool_missing", "path_unsupported",
                            "isolation_failed", "configure_failed", "build_failed", "test_failed",
                            "timeout", "execution_error", "passed", "external_oracle_failed"} else "oracle_failed",
                        "commands": list(self.commands)}
        finally:
            common.command = original_command
            if old_common is None: sys.modules.pop("common", None)
            else: sys.modules["common"] = old_common
