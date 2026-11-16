"""Deterministic validation of checkpointed changes; no model is consulted.

All command templates belong to ToolchainProfile. Build products, execution
inventories and hidden Oracle sources live outside the Agent's repository view.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4
from uuid import UUID
import xml.etree.ElementTree as ET

from .artifacts.canonical import content_sha256
from .artifacts.models import ChangeSet, CommandResult, TaskSpec, ValidationReport
from .build import BuildPipeline
from .build_process import BuildCancelled, build_environment, run_build_process
from .errors import ForgePilotError
from .integrity import IntegrityMonitor, IntegritySnapshot, Registration
from .path_identity import path_key, path_within, same_path
from .presets import MissionBuildSelection
from .repository import Repository
from .security import Redactor
from .toolchain import ToolchainProfile
from .workspace_safety import Guard, manifest, validate_relative, _exists


PIPELINE_STAGES = (
    "Workspace Integrity", "Test Integrity", "CMake Configure", "Build", "CTest",
    "Protected Oracle", "Diff Scope Check", "ValidationReport",
)
_INFRASTRUCTURE = re.compile(
    r"command not found|not recognized as an internal|unable to find executable|"
    r"could not find.*(?:compiler|ninja|cmake|package)|no cmake_cxx_compiler|"
    r"could not load cache|ninja: error: loading|createprocess failed|"
    r"cannot find (?:the file|the compiler)|fatal error lnk1104|"
    r"fatal error c1083:.*(?:windows\.h|stdlib\.h|stddef\.h|iostream)|"
    r"no space left|disk full|not enough space|access is denied|permission denied|"
    r"path.*(?:too long|exceed.*(?:260|maximum))|msb3491|msb3101|"
    r"compiler.*(?:not found|not able)|forgepilot-worker: configured executable", re.I)


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProtectedOracle:
    """Trusted external CMake project, never exposed in model context."""
    source_dir: Path


class _Failure(Exception):
    def __init__(self, category, summary):
        self.category, self.summary = category, summary


def classify_command(result, phase):
    """Only observed build/test failures may spend the repair budget."""
    if result.status == "succeeded":
        return "none"
    if result.status == "cancelled":
        return "cancelled"
    if (result.status != "failed" or result.returncode in {None, 125}
            or result.returncode < 0 or result.returncode > 2_147_483_647
            or phase == "cmake_configure"
            or _INFRASTRUCTURE.search(result.stdout + "\n" + result.stderr)):
        return "infrastructure_failure"
    return "code_failure"


class ValidationService:
    def __init__(self, workspace_manager, profile: ToolchainProfile, *, build_root: Path,
                 oracle: ProtectedOracle | None, configuration="Debug", generator=None,
                 architecture=None, make_program=None, timeout=120, output_limit=65_536,
                 log_sink=None, redactor=None):
        if not isinstance(profile, ToolchainProfile):
            raise TypeError("Validator requires a host-owned ToolchainProfile")
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600
                or type(output_limit) is not int or not 1024 <= output_limit <= 1_048_576):
            raise ValueError("Invalid validation process limits")
        self.workspace, self.profile = workspace_manager, profile
        self.guard = Guard(build_root, create=True)
        self.oracle = oracle
        self.configuration, self.generator = configuration, generator
        self.architecture, self.make_program = architecture, make_program
        self.timeout, self.output_limit = timeout, output_limit
        self.log_sink, self.redactor = log_sink, redactor or Redactor()
        self._monitors = {}
        self._selection_hashes = {}
        self._lock = asyncio.Lock()

    def _store_log(self, task_id, phase, data):
        """Host-only bounded log artifact; hidden Oracle output never enters here."""
        from .mission_events import safe_event_data
        self.guard.mkdir(task_id + "/logs")
        identifier = str(uuid4())
        content = safe_event_data(self.redactor, {"phase": phase, **data})
        record = {"artifact_id": identifier, "artifact_type": "ValidationLog", "task_id": task_id,
                  "created_at": _now(), "content_sha256": content_sha256(content), "content": content}
        self.guard.write(task_id + "/logs/" + identifier + ".json", json.dumps(record, ensure_ascii=False).encode())
        return identifier

    def read_log(self, task_id, artifact_id):
        """Application-only lookup, intentionally absent from Agent capabilities."""
        self._paths(task_id)
        try:
            if str(UUID(artifact_id)) != artifact_id:
                raise ValueError()
            record = json.loads(self.guard.read(task_id + "/logs/" + artifact_id + ".json"))
            if (record["artifact_id"] != artifact_id or record["task_id"] != task_id
                    or record["artifact_type"] != "ValidationLog"
                    or record["content_sha256"] != content_sha256(record["content"])):
                raise ValueError()
            from .mission_events import safe_event_data
            if safe_event_data(self.redactor, record) != record:
                raise ValueError()
            return record
        except (ValueError, KeyError, TypeError, OSError):
            raise ForgePilotError("validation_log", "Validation log identity or hash mismatch") from None

    def _paths(self, task_id):
        validate_relative(task_id)
        if "/" in task_id or "\\" in task_id:
            raise ForgePilotError("validation_policy", "Invalid validation task identity")
        record = self.workspace.record(task_id)
        root = Path(record["worktree_path"])
        if path_within(self.guard.root, root) or path_within(root, self.guard.root):
            raise ForgePilotError("validation_policy", "Validation storage must be outside the Worktree")
        self.guard.check()
        self.guard.mkdir(task_id)
        self.guard.mkdir(task_id + "/build")
        return record, root, self.guard.path(task_id + "/build")

    def _workspace_check(self, task_id):
        with self.workspace._operation(task_id) as state:
            current = self.workspace.inspect_task(task_id)
            self.workspace._assert_expected(state, current)
            return current

    def compilation_directory(self, task_id):
        """Expose only the build of a successfully captured execution baseline.

        This read-only grant survives restart and starts no configure/build.
        Before approval/capture, CodeIntelligence uses existing worktree metadata.
        """
        from .compile_database import TrustedCompilationDirectory

        record, root, build = self._paths(task_id)
        path = task_id + "/integrity.json"
        if not _exists(self.guard.path(path, missing=True)):
            return None
        try:
            state = json.loads(self.guard.read(path, max_bytes=16_777_216))
            selection = self._build_selection(task_id, record, root)
            if (state["version"] != 1 or state["workspace_id"] != record["workspace_id"]
                    or state["base_ref"] != record["base_ref"]
                    or state["snapshot"]["ctest_inventory_source"] != "ctest"
                    or state.get("build_selection_sha256") != (
                        content_sha256(selection.to_dict()) if selection is not None else None)):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ForgePilotError("compile_database_stale", "Approved compilation database belongs to another task baseline") from None
        return TrustedCompilationDirectory(root, Guard(build))

    def bind_build_selection(self, task_id: str, selection: MissionBuildSelection) -> None:
        """Bind once after source-baseline confirmation; restoring never changes frozen options."""
        if not isinstance(selection, MissionBuildSelection):
            raise TypeError("Mission build selection must be a resolved immutable selection")
        record, root, _ = self._paths(task_id)
        selection.verify(root)
        selection.verify_tools(self.profile.cmake, self.profile.ctest)
        state = {"version": 1, "workspace_id": record["workspace_id"], "base_ref": record["base_ref"],
                 "selection": selection.to_dict(), "selection_hash": content_sha256(selection.to_dict())}
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
        path = task_id + "/build-selection.json"
        if _exists(self.guard.path(path, missing=True)):
            try:
                previous = json.loads(self.guard.read(path))
            except (ValueError, TypeError, RecursionError):
                raise ForgePilotError("build_selection_changed", "Stored Mission build selection is invalid") from None
            if previous != json.loads(encoded):
                raise ForgePilotError("build_selection_changed", "Mission build selection is already frozen with different inputs")
            self._selection_hashes[task_id] = state["selection_hash"]
            return
        self.guard.write(path, encoded)
        self._selection_hashes[task_id] = state["selection_hash"]

    def _build_selection(self, task_id, record, root):
        path = task_id + "/build-selection.json"
        if not _exists(self.guard.path(path, missing=True)):
            if task_id in self._selection_hashes:
                raise ForgePilotError("build_selection_changed", "Required frozen Mission build selection is missing")
            return None  # Historical tasks keep their original explicit configuration.
        try:
            state = json.loads(self.guard.read(path))
            if (state["version"] != 1 or state["workspace_id"] != record["workspace_id"]
                    or state["base_ref"] != record["base_ref"]
                    or state["selection_hash"] != content_sha256(state["selection"])
                    or (task_id in self._selection_hashes and state["selection_hash"] != self._selection_hashes[task_id])):
                raise ValueError()
            selection = MissionBuildSelection.from_dict(state["selection"])
            selection.verify(root)
            selection.verify_tools(self.profile.cmake, self.profile.ctest)
            return selection
        except (KeyError, ValueError, TypeError):
            raise ForgePilotError("build_selection_changed", "Frozen Mission build selection identity or hash changed") from None

    def _values(self, root, build_dir, *, report_path=None):
        return {"source_dir": str(root), "build_dir": str(build_dir),
                "configuration": self.configuration, "jobs": "1",
                "generator": self.generator or "", "architecture": self.architecture or "",
                "make_program": self.make_program or "", "report_path": str(report_path or "")}

    def _command(self, phase, values, *, hidden=False, selection=None):
        if selection is not None and not hidden:
            selection.verify_tools(self.profile.cmake, self.profile.ctest)
            selection.verify(Path(values["source_dir"]))
            return self.profile.resolve_mission(phase, values, selection)
        options = {"generator": bool(self.generator), "architecture": bool(self.architecture),
                   "make_program": bool(self.make_program),
                   "multi_config": bool(self.generator and (
                       "Visual Studio" in self.generator or "Multi-Config" in self.generator))}
        method = self.profile.resolve_oracle if hidden else self.profile.resolve
        return method(phase, values, **options)

    async def _run(self, command, is_cancelled):
        if is_cancelled():
            raise asyncio.CancelledError()
        task = asyncio.create_task(run_build_process(command.argv, command.cwd,
            timeout=self.timeout, output_limit=self.output_limit, env=build_environment()))
        try:
            while not task.done():
                if is_cancelled():
                    task.cancel()
                    break
                await asyncio.wait({task}, timeout=0.02)
            return await task
        except asyncio.CancelledError:
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            # Retrieve BuildCancelled to avoid orphaned exceptions; process tree
            # cleanup completes before the caller observes cancellation.
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            raise

    def _save_baseline(self, task_id, record, spec, monitor):
        state = {"version": 1, "workspace_id": record["workspace_id"],
                 "base_ref": record["base_ref"], "spec_hash": content_sha256(spec),
                 "snapshot": asdict(monitor.snapshot), "tests": sorted(monitor._tests),
                 "approved": sorted(monitor._approved),
                 "modifiable_test_files": list(monitor.task_spec.test_files_to_modify),
                 "file_hashes": monitor._validation_manifest,
                 "oracle_manifest": monitor._validation_oracle,
                 "approval_checkpoint_hash": getattr(monitor, "_validation_approval_checkpoint", None),
                 "files": {path: base64.b64encode(data).decode("ascii")
                           for path, data in monitor._baseline.items()}}
        selection = self._build_selection(task_id, record, Path(record["worktree_path"]))
        state["build_selection_sha256"] = content_sha256(selection.to_dict()) if selection is not None else None
        self.guard.write(task_id + "/integrity.json", json.dumps(state, ensure_ascii=False).encode())

    def _load_baseline(self, task_id, record, spec, root):
        path = self.guard.path(task_id + "/integrity.json", missing=True)
        if not _exists(path):
            return None
        try:
            raw = json.loads(self.guard.read(task_id + "/integrity.json", max_bytes=16_777_216))
            if (raw["version"] != 1 or raw["workspace_id"] != record["workspace_id"]
                    or raw["base_ref"] != record["base_ref"] or raw["spec_hash"] != content_sha256(spec)):
                raise ValueError()
            selection = self._build_selection(task_id, record, root)
            if raw.get("build_selection_sha256") != (content_sha256(selection.to_dict()) if selection is not None else None):
                raise ForgePilotError("build_selection_changed", "Validation baseline requires its original frozen build selection")
            snapshot = raw["snapshot"]
            monitor = IntegrityMonitor(Repository(root, ""), spec)
            monitor.snapshot = IntegritySnapshot(snapshot["plan_version"],
                tuple(tuple(item) for item in snapshot["test_hashes"]),
                tuple(snapshot["ctest_names"]), tuple(Registration(**item) for item in snapshot["registrations"]),
                snapshot["test_count"], snapshot["ctest_inventory_source"])
            monitor._baseline = {path: base64.b64decode(value, validate=True) for path, value in raw["files"].items()}
            monitor._tests = set(raw["tests"])
            monitor._approved = frozenset(raw["approved"])
            monitor._ctest_bound = snapshot["ctest_inventory_source"] == "ctest"
            monitor._validation_manifest = raw["file_hashes"]
            monitor._validation_oracle = raw.get("oracle_manifest")
            monitor._validation_approval_checkpoint = raw.get("approval_checkpoint_hash")
            if any(monitor._validation_manifest.get(path) != sha256(data).hexdigest()
                   for path, data in monitor._baseline.items()):
                raise ValueError()
            return monitor
        except (ValueError, TypeError, KeyError):
            raise ForgePilotError("validation_policy", "Validation approval baseline is invalid") from None

    async def capture(self, task_id, spec, *, approved_test_files=(), plan_version=1,
                      is_cancelled=lambda: False, approval_checkpoint=None):
        """Capture before writes; an existing baseline is never replaced by edits."""
        spec = TaskSpec.model_validate(spec)
        if spec.repository.toolchain_profile != self.profile.profile_id:
            raise ForgePilotError("validation_policy", "Configured ToolchainProfile differs from the frozen TaskSpec")
        if self.oracle is None:
            raise ForgePilotError("validation_infrastructure", "Protected Oracle is not configured")
        async with self._lock:
            record, root, build = self._paths(task_id)
            expected = self._workspace_check(task_id)
            if approval_checkpoint is not None and (
                    expected.head_commit != approval_checkpoint.head_commit or
                    dict(expected.file_hashes) != dict(approval_checkpoint.file_hashes)):
                raise ForgePilotError("validation_policy", "Baseline differs from the approved checkpoint")
            checkpoint_hash = approval_checkpoint.data_sha256 if approval_checkpoint is not None else None
            monitor = self._load_baseline(task_id, record, spec, root)
            if monitor is not None:
                if (monitor._validation_approval_checkpoint != checkpoint_hash or
                        monitor._validation_manifest != dict(expected.file_hashes)):
                    raise ForgePilotError("validation_policy", "Stored baseline differs from the approved checkpoint")
                monitor.capture(plan_version, approved_test_files=approved_test_files)
                self._save_baseline(task_id, record, spec, monitor)
                self._monitors[task_id] = monitor
                return monitor.report()
            monitor = IntegrityMonitor(Repository(root, ""), spec)
            monitor.capture(plan_version, approved_test_files=approved_test_files)
            monitor._validation_manifest = dict(expected.file_hashes)
            monitor._validation_oracle = None
            monitor._validation_approval_checkpoint = checkpoint_hash
            if self.oracle is not None:
                oracle_root = Path(self.oracle.source_dir)
                if (not oracle_root.is_absolute() or path_within(oracle_root, root)
                        or path_within(root, oracle_root) or path_within(oracle_root, self.guard.root)):
                    raise ForgePilotError("validation_policy", "Protected Oracle must be isolated outside the Worktree")
                try:
                    monitor._validation_oracle = manifest(oracle_root)
                except (ForgePilotError, OSError):
                    raise ForgePilotError("validation_infrastructure", "Protected Oracle is unavailable") from None
            selection = self._build_selection(task_id, record, root)
            values = self._values(root, build)
            try:
                for phase in ("cmake_configure", "ctest_inventory"):
                    result = await self._run(self._command(phase, values, selection=selection), is_cancelled)
                    if result.status == "cancelled":
                        raise asyncio.CancelledError()
                    if result.status != "succeeded":
                        raise ForgePilotError("validation_infrastructure", "Execution baseline CMake/CTest inventory could not be captured")
                monitor.bind_ctest(BuildPipeline._inventory_names(result.stdout))
            except ForgePilotError as exc:
                if exc.code == "command_denied":
                    raise ForgePilotError("validation_infrastructure", "Configured validation tool is unavailable") from None
                raise
            finally:
                if self._workspace_check(task_id) != expected:
                    raise ForgePilotError("validation_policy", "Worktree changed during baseline capture")
            if is_cancelled():
                raise asyncio.CancelledError()
            self._save_baseline(task_id, record, spec, monitor)
            self._monitors[task_id] = monitor
            return monitor.report()

    def approve_tests(self, task_id, paths):
        """Host's separate, exact-file approval; never recapture source bytes."""
        record, root, _ = self._paths(task_id)
        raw = json.loads(self.guard.read(task_id + "/integrity.json", max_bytes=16_777_216))
        repository = Repository(root, "")
        approved = frozenset(path_key(repository.normalize_plan_path(path)) for path in paths)
        requested = frozenset(path_key(path) for path in raw["modifiable_test_files"])
        if approved - requested or approved - {path_key(path) for path in raw["tests"]}:
            raise ForgePilotError("validation_integrity", "Test approval must match exact existing TaskSpec files")
        if raw["workspace_id"] != record["workspace_id"]:
            raise ForgePilotError("validation_policy", "Test baseline belongs to a different workspace")
        self._workspace_check(task_id)
        raw["approved"] = sorted(approved)
        self.guard.write(task_id + "/integrity.json", json.dumps(raw, ensure_ascii=False).encode())
        if task_id in self._monitors:
            self._monitors[task_id]._approved = approved

    @staticmethod
    def _tests(path):
        try:
            if path.stat().st_size > 4_194_304 or path.is_symlink():
                raise ValueError()
            data = path.read_bytes()
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError()
            root = ET.fromstring(data)
            if root.tag not in {"testsuite", "testsuites"}:
                raise ValueError()
            cases = list(root.iter("testcase"))
            names = [case.get("name", "") for case in cases]
            if not names or not all(names) or len(names) != len(set(names)):
                raise ValueError()
            failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
            skipped = sum(case.find("skipped") is not None or case.get("status", "run").lower() in {"notrun", "disabled"}
                          for case in cases)
            return names, len(cases) - failed - skipped, failed, skipped
        except (OSError, ValueError, ET.ParseError):
            raise _Failure("infrastructure_failure", "CTest did not produce a valid complete test report") from None

    def _scope(self, task_id, spec, changes, expected, monitor):
        current = self._workspace_check(task_id)
        if current != expected:
            raise _Failure("policy_failure", "Worktree changed during deterministic validation")
        baseline = monitor._validation_manifest
        actual = {path for path in set(baseline) | set(current.file_hashes)
                  if baseline.get(path) != current.file_hashes.get(path)}
        # File manifests include ignored and binary paths too: every actual
        # change must be represented by the host-generated cumulative ChangeSet.
        claimed = {item.path for item in changes.files}
        if actual != claimed or changes.requires_additional_approval:
            raise _Failure("policy_failure", "Final ChangeSet does not match the complete approved diff")
        if len(actual) > spec.allowed_scope.max_changed_files:
            raise _Failure("policy_failure", "Final diff exceeds the approved file limit")
        for item in changes.files:
            key = path_key(item.path)
            if (not any(fnmatchcase(key, path_key(pattern)) for pattern in spec.allowed_scope.path_patterns)
                    or any(fnmatchcase(key, path_key(pattern)) for pattern in spec.forbidden_scope)
                    or item.before_sha256 != baseline.get(item.path)
                    or item.after_sha256 != current.file_hashes.get(item.path)):
                raise _Failure("policy_failure", "Final diff is outside approved scope or has inconsistent hashes")

    async def validate(self, task_id, spec, changes, *, is_cancelled=lambda: False):
        spec, changes = TaskSpec.model_validate(spec), ChangeSet.model_validate(changes)
        async with self._lock:
            return await self._validate(task_id, spec, changes, is_cancelled)

    async def _validate(self, task_id, spec, changes, is_cancelled):
        commands, stages, logs = [], [], []
        state = {"workspace_integrity": "not_checked", "test_integrity": "not_checked",
                 "diff_scope": "not_checked", "hidden_oracle": "not_run"}
        passed = failed = 0
        classification, status, summary = "none", "passed", "All deterministic validation stages passed"
        compile_errors = ()
        expected = monitor = selection = None

        def record_check(message, *, success=True):
            now = _now()
            commands.append(CommandResult(kind="integrity", command_summary=stages[-1], started_at=now,
                finished_at=now, exit_code=0 if success else 1, output_summary=message))

        async def execute(phase, values, *, hidden=False):
            started = _now()
            try:
                command = self._command(phase, values, hidden=hidden, selection=selection)
                result = await self._run(command, is_cancelled)
            except ForgePilotError as exc:
                category = "infrastructure_failure" if exc.code in {"command_denied", "build_job", "platform_unsupported"} else "policy_failure"
                raise _Failure(category, "Configured validation command could not be executed") from None
            except BuildCancelled as exc:
                result = exc.result
            kind = "protected_test" if hidden else {"cmake_configure": "configure", "cmake_build": "build",
                "run_tests": "test", "ctest_inventory": "integrity"}[phase]
            output = ("Protected Oracle " + ("passed" if result.status == "succeeded" else "failed; hidden diagnostics withheld")) if hidden else self.redactor.data(result.stdout + "\n" + result.stderr)[:8192]
            command_summary = "Protected Oracle " + phase if hidden else json.dumps(
                {"shell": False, **command.review()}, ensure_ascii=False)[:512]
            code = result.returncode if result.returncode is not None else 1
            # Windows process termination can use unsigned NTSTATUS values.
            if code > 2_147_483_647:
                code -= 4_294_967_296
            if result.status != "succeeded" and code == 0:
                code = 1
            commands.append(CommandResult(kind=kind, command_summary=command_summary, started_at=started,
                finished_at=_now(), exit_code=code, output_summary=output))
            if not hidden:
                sink = self.log_sink or self._store_log
                artifact_id = sink(task_id, phase, self.redactor.data(asdict(result)))
                if artifact_id:
                    logs.append(artifact_id)
            if result.status == "cancelled":
                raise asyncio.CancelledError()
            category = classify_command(result, phase)
            if category != "none":
                raise _Failure(category, ("Protected Oracle failed; hidden diagnostics withheld" if hidden else
                    f"{phase} failed ({result.status})"))
            return result

        try:
            stages.append(PIPELINE_STAGES[0])
            if is_cancelled():
                raise asyncio.CancelledError()
            record, root, build = self._paths(task_id)
            selection = self._build_selection(task_id, record, root)
            if spec.repository.toolchain_profile != self.profile.profile_id:
                raise _Failure("policy_failure", "Configured ToolchainProfile differs from the frozen TaskSpec")
            expected = self._workspace_check(task_id)
            state["workspace_integrity"] = "passed"
            record_check("Checkpoint HEAD, index and full Worktree manifest match")
            stages.append(PIPELINE_STAGES[1])
            monitor = self._load_baseline(task_id, record, spec, root)
            if monitor is None:
                raise _Failure("policy_failure", "Execution test integrity baseline is missing")
            monitor.verify()
            state["test_integrity"] = "passed"
            record_check("Test hashes, assertions, skip policy and registration locations match")
            values = self._values(root, build, report_path=build / ("ctest-" + uuid4().hex + ".xml"))
            stages.append(PIPELINE_STAGES[2])
            await execute("cmake_configure", values)
            stages.append(PIPELINE_STAGES[3])
            await execute("cmake_build", values)
            stages.append(PIPELINE_STAGES[4])
            inventory = await execute("ctest_inventory", values)
            names = BuildPipeline._inventory_names(inventory.stdout)
            monitor.verify(ctest_names=names)
            # Even a failing CTest result must be parsed for skipped tests and
            # registration loss; these policy failures outrank code failures.
            test_failure = None
            try:
                await execute("run_tests", values)
            except _Failure as exc:
                if exc.category != "code_failure":
                    raise
                test_failure = exc
            test_names, passed, failed, skipped = self._tests(Path(values["report_path"]))
            monitor.verify(ctest_names=test_names)
            if set(names) != set(test_names) or skipped:
                raise _Failure("policy_failure", "CTest skipped, disabled or omitted registered tests")
            if test_failure:
                raise test_failure
            if failed:
                raise _Failure("code_failure", "CTest reported failed tests")
            stages.append(PIPELINE_STAGES[5])
            if self.oracle is None:
                raise _Failure("infrastructure_failure", "Protected Oracle is not configured")
            oracle_root = Path(self.oracle.source_dir)
            if (not oracle_root.is_absolute() or not oracle_root.is_dir()
                    or not same_path(oracle_root.resolve(), oracle_root)
                    or path_within(oracle_root, root) or path_within(root, oracle_root)
                    or path_within(oracle_root, self.guard.root)):
                raise _Failure("policy_failure", "Protected Oracle is not an isolated external source")
            oracle_guard = Guard(oracle_root)
            oracle_files = manifest(oracle_root)
            if monitor._validation_oracle is None:
                raise _Failure("infrastructure_failure", "Protected Oracle was not bound at approval")
            if oracle_files != monitor._validation_oracle:
                raise _Failure("policy_failure", "Protected Oracle changed after approval")
            self.guard.mkdir(task_id + "/protected-build")
            oracle_build = self.guard.path(task_id + "/protected-build")
            oracle_values = self._values(oracle_root, oracle_build,
                report_path=oracle_build / ("ctest-" + uuid4().hex + ".xml"))
            oracle_values.update(workspace_dir=str(root), target_build_dir=str(build))
            try:
                with oracle_guard.protect():
                    for phase in ("cmake_configure", "cmake_build", "run_tests"):
                        await execute(phase, oracle_values, hidden=True)
                    _, _, oracle_failed, oracle_skipped = self._tests(Path(oracle_values["report_path"]))
                    if oracle_failed or oracle_skipped:
                        raise _Failure("code_failure", "Protected Oracle failed; hidden diagnostics withheld")
                    if manifest(oracle_root) != oracle_files:
                        raise _Failure("policy_failure", "Protected Oracle integrity changed")
            except _Failure:
                state["hidden_oracle"] = "failed"
                raise
            except (ForgePilotError, OSError, ValueError):
                state["hidden_oracle"] = "failed"
                raise _Failure("infrastructure_failure", "Protected Oracle infrastructure failed; hidden diagnostics withheld") from None
            state["hidden_oracle"] = "passed"
            stages.append(PIPELINE_STAGES[6])
            monitor.verify(ctest_names=test_names)
            self._scope(task_id, spec, changes, expected, monitor)
            state["diff_scope"] = "passed"
            record_check("Final checkpoint diff and hashes are inside the frozen approved scope")
        except asyncio.CancelledError:
            classification, status, summary = "cancelled", "cancelled", "Validation cancelled; owned process tree terminated"
        except _Failure as exc:
            classification, status, summary = exc.category, "failed", exc.summary
        except ForgePilotError as exc:
            classification, status = "policy_failure", "failed"
            summary = self.redactor.text(str(exc))[:2048]
        except (OSError, ValueError, TypeError):
            classification, status, summary = "infrastructure_failure", "failed", "Validation infrastructure could not produce trustworthy evidence"
        if expected is not None and status != "cancelled":
            # A failing compiler or test must not conceal a write from a build
            # script. Such a result is policy-blocked before Repair is considered.
            try:
                if self._workspace_check(task_id) != expected:
                    raise ForgePilotError("validation_policy", "Worktree changed during validation")
                if monitor is not None:
                    monitor.verify()
            except ForgePilotError:
                classification, status, summary = "policy_failure", "failed", "Workspace or test integrity changed during validation"
                state["workspace_integrity"] = "failed"
        if status != "passed":
            stage = stages[-1] if stages else PIPELINE_STAGES[0]
            if classification == "policy_failure":
                field = {PIPELINE_STAGES[0]: "workspace_integrity", PIPELINE_STAGES[1]: "test_integrity",
                         PIPELINE_STAGES[4]: "test_integrity", PIPELINE_STAGES[6]: "diff_scope"}.get(stage)
                if field:
                    state[field] = "failed"
            if stage == "Build" and classification == "code_failure":
                compile_errors = tuple(item.output_summary for item in commands[-1:] if item.output_summary)
            record_check(summary, success=False)
        stages.append(PIPELINE_STAGES[-1])
        return ValidationReport(toolchain_profile=self.profile.profile_id, commands=tuple(commands),
            status=status, tests_passed=passed, tests_failed=failed, failure_classification=classification,
            summary=summary, compile_errors=compile_errors, log_artifact_ids=tuple(logs),
            pipeline_stages=tuple(stages), **state)
