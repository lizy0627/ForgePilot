"""Validator boundaries; full C++ repair/review loops live in test_delivery_workflow."""
from hashlib import sha256
import asyncio
import ctypes
from ctypes import wintypes
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from forgepilot.errors import ForgePilotError

from forgepilot.artifacts.models import ChangeSet, ChangedFile, TaskSpec, TestPolicy as ArtifactTestPolicy
from forgepilot.agents.delivery import validation_passed
from forgepilot.artifacts import create_artifact
from forgepilot.build_process import CommandResult
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.validation import PIPELINE_STAGES, ProtectedOracle, ValidationService, classify_command
from forgepilot.toolchain import ToolchainProfile
from forgepilot.workspace_manager import WorkspaceManager
from test_workspace_manager import git
from test_build_process import alive


@pytest.mark.parametrize("phase,status,code,diagnostic,expected", [
    ("cmake_build", "failed", 1, "error C2065: undeclared identifier", "code_failure"),
    ("cmake_build", "failed", 1, "unresolved external symbol", "code_failure"),
    ("run_tests", "failed", 8, "Assertion failed", "code_failure"),
    ("cmake_build", "failed", 1, "No space left on device", "infrastructure_failure"),
    ("cmake_build", "failed", 1, "error MSB3491: path is too long", "infrastructure_failure"),
    ("cmake_build", "failed", 125, "", "infrastructure_failure"),
    ("cmake_build", "failed", -9, "", "infrastructure_failure"),
    ("run_tests", "failed", 3_221_225_786, "", "infrastructure_failure"),
    ("cmake_configure", "failed", 1, "Could not find Qt6", "infrastructure_failure"),
    ("run_tests", "timed_out", 1, "", "infrastructure_failure"),
    ("cmake_build", "output_limit", 1, "", "infrastructure_failure"),
    ("run_tests", "cancelled", 1, "", "cancelled"),
])
def test_failure_classification_is_deterministic(phase, status, code, diagnostic, expected):
    result = CommandResult([], ".", code, diagnostic, "", 1, status)
    assert classify_command(result, phase) == expected


@pytest.fixture
def validator_case(tmp_path, git_exe, request):
    root = tmp_path / "source"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "source.cpp").write_text("int value = 0;\n")
    (root / "tests/check.cpp").write_text("void check(){ assert(value == 0); }\n")
    cmake = 'cmake_minimum_required(VERSION 3.24)\nproject(ValidatorBoundary LANGUAGES NONE)\nenable_testing()\nadd_test(NAME public COMMAND "${CMAKE_COMMAND}" -E true)\n'
    if getattr(request, "param", None) == "runtime_skip":
        cmake = cmake.replace('-E true', '-E false') + 'set_tests_properties(public PROPERTIES SKIP_RETURN_CODE 1)\n'
    (root / "CMakeLists.txt").write_text(cmake)
    git(git_exe, root, "init", "-q", "-b", "main")
    git(git_exe, root, "config", "user.name", "Fixture")
    git(git_exe, root, "config", "user.email", "fixture@example.invalid")
    git(git_exe, root, "config", "core.autocrlf", "false")
    git(git_exe, root, "add", "--all")
    git(git_exe, root, "commit", "-qm", "baseline")
    oracle = tmp_path / "private reference"
    oracle.mkdir()
    (oracle / "CMakeLists.txt").write_text(cmake + 'message("PRIVATE_ORACLE_SOURCE_NEVER_SHARE")\n')
    manager = WorkspaceManager(tmp_path / "data", git_executable=git_exe)
    task = uuid4().hex[:12]
    record = manager.create(root, task)
    profile = ToolchainProfile(shutil.which("cmake"), shutil.which("ctest"))
    build = tmp_path.parent / ("v-" + task)
    validator = ValidationService(manager, profile, build_root=build, oracle=ProtectedOracle(oracle),
                                  generator="Ninja", make_program=shutil.which("ninja"))
    spec = TaskSpec.model_validate({
        "task_type": "bug_fix", "title": "Fixture", "objective": "Validate changes",
        "repository": {"base_ref": "HEAD", "toolchain_profile": profile.profile_id},
        "problem": {"observed_behavior": "Wrong value", "expected_behavior": "Correct value", "reproduction_steps": []},
        "acceptance_criteria": [{"id": "AC-1", "description": "All checks pass", "verification": "ctest"}],
        "allowed_scope": {"path_patterns": ["source.cpp", "tests/**", "CMakeLists.txt"], "max_changed_files": 3},
        "risk_level": "low",
    })
    return manager, task, Path(record["worktree_path"]), validator, spec, oracle, build


def edit(case, path="source.cpp", content="int value = 1;\n"):
    manager, task, root, _, _, _, _ = case
    old = (root / path).read_bytes()
    manager.approve_plan(task, approved=True, expected=manager.inspect_task(task))
    with manager.writer(task, expected=manager.inspect_task(task)) as worktree:
        (worktree / path).write_bytes(content.encode())
    manager.checkpoint_commit(task, kind="implementation", expected=manager.inspect_task(task))
    return ChangeSet(execution_plan_artifact_id=str(uuid4()), summary="Observed change",
        files=(ChangedFile(path=path, change_type="modified", unified_diff="-old\n+new\n",
            before_sha256=sha256(old).hexdigest(), after_sha256=sha256(content.encode()).hexdigest(),
            plan_step_ids=("STEP-1",), acceptance_criteria_ids=("AC-1",)),))


def test_existing_baseline_load_does_not_trust_path_exists(validator_case, monkeypatch):
    """A Win32 long-path false negative cannot bless edited tests as a new baseline."""
    from forgepilot.integrity import IntegrityMonitor
    from forgepilot.repository import Repository

    manager, task, root, validator, spec, _, _ = validator_case
    record, _, _ = validator._paths(task)
    monitor = IntegrityMonitor(Repository(root, ""), spec)
    monitor.capture(1, ctest_names=["public"])
    monitor._validation_manifest = dict(manager.inspect_task(task).file_hashes)
    monitor._validation_oracle = None
    validator._save_baseline(task, record, spec, monitor)
    baseline = validator.guard.path(task + "/integrity.json")
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path == baseline else original_exists(path))
    assert not baseline.exists()  # Reproduce the Windows >260-character failure.

    (root / "tests/check.cpp").write_text("void check(){ assert(true); }\n")
    restored = validator._load_baseline(task, record, spec, root)
    assert restored is not None
    assert restored.snapshot.test_hashes == monitor.snapshot.test_hashes
    with pytest.raises(ForgePilotError, match="Existing test hash changed"):
        restored.verify()


@pytest.mark.asyncio
async def test_full_pipeline_and_baseline_survive_validator_restart(validator_case):
    manager, task, root, validator, spec, oracle, build = validator_case
    baseline = await validator.capture(task, spec)
    assert baseline["ctest_inventory_source"] == "ctest"
    assert baseline["test_count"] == 1 and baseline["test_files"]
    changes = edit(validator_case)
    restarted = ValidationService(manager, validator.profile, build_root=build,
        oracle=ProtectedOracle(oracle), generator="Ninja", make_program=shutil.which("ninja"))
    report = await restarted.validate(task, spec, changes)
    assert report.status == "passed", report
    assert report.pipeline_stages == PIPELINE_STAGES
    assert report.workspace_integrity == report.test_integrity == report.diff_scope == "passed"
    assert report.hidden_oracle == "passed"
    assert "PRIVATE_ORACLE_SOURCE_NEVER_SHARE" not in report.model_dump_json()
    assert str(oracle) not in report.model_dump_json()
    assert not (root / "build").exists()
    assert len(report.log_artifact_ids) == 4
    assert all(restarted.read_log(task, identifier)["content_sha256"] for identifier in report.log_artifact_ids)
    # The CTest inventory is JSON text. Its canonicalization must already be
    # complete before content hashing, including when saved as an Artifact.
    assert all(restarted.redactor.data(item.output_summary) == item.output_summary
               for item in report.commands)
    artifact_store = SqliteEventStore(":memory:", redactor=restarted.redactor,
                                      artifact_root=build.parent / ("artifact-check-" + task))
    try:
        candidate = create_artifact(task_id=str(uuid4()), producer="validator.v1",
            producer_node="validate", content=report)
        artifact_store.artifacts.put(candidate)
        assert artifact_store.artifacts.get(candidate.artifact_id) == candidate
    finally:
        artifact_store.close()
    assert all('"shell": false' in command.command_summary for command in report.commands
               if command.kind in {"configure", "build", "test"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hash", "skip", "assertion", "registration"])
async def test_integrity_violation_blocks_before_any_command(validator_case, mode):
    manager, task, root, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    if mode == "registration":
        path = "CMakeLists.txt"
        content = (root / path).read_text().replace("add_test(NAME public COMMAND", "add_test(NAME renamed COMMAND")
    else:
        path = "tests/check.cpp"
        content = {"hash": "// edit\nvoid check(){ assert(value == 0); }\n",
                   "skip": "void check(){ GTEST_SKIP(); }\n",
                   "assertion": "void check(){ assert(true); }\n"}[mode]
    changes = edit(validator_case, path, content)
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "policy_failure"
    assert report.test_integrity == "failed"
    assert not any(item.kind in {"configure", "build", "test"} for item in report.commands)


@pytest.mark.asyncio
async def test_missing_baseline_is_not_recaptured_after_edits(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    changes = edit(validator_case)
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "policy_failure"
    assert "baseline" in report.summary


@pytest.mark.asyncio
async def test_missing_oracle_pauses_before_baseline_and_can_be_configured(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    oracle, validator.oracle = validator.oracle, None
    with pytest.raises(ForgePilotError, match="validation_infrastructure"):
        await validator.capture(task, spec)
    validator.oracle = oracle
    baseline = await validator.capture(task, spec)
    assert baseline["ctest_inventory_source"] == "ctest"


@pytest.mark.asyncio
async def test_public_validation_without_oracle_keeps_baseline_and_reports_unrun_hidden_tests(validator_case):
    manager, task, root, validator, spec, _, build = validator_case
    validator.oracle = None
    baseline = await validator.capture(task, spec, require_oracle=False)
    assert baseline["ctest_inventory_source"] == "ctest"
    changes = edit(validator_case)
    restarted = ValidationService(manager, validator.profile, build_root=build,
        oracle=None, generator="Ninja", make_program=shutil.which("ninja"))
    report = await restarted.validate(task, spec, changes)
    assert report.status == "passed", report
    assert report.workspace_integrity == report.test_integrity == report.diff_scope == "passed"
    assert report.hidden_oracle == "not_run"
    assert "not configured" in report.summary.lower()
    assert "not run" in report.summary.lower()
    assert {item.kind for item in report.commands} >= {"configure", "build", "test", "integrity"}
    assert not any(item.kind == "protected_test" for item in report.commands)
    assert validation_passed(report, require_oracle=False)
    assert not validation_passed(report)


@pytest.mark.asyncio
async def test_protected_criterion_cannot_downgrade_when_oracle_is_missing(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    spec = spec.model_copy(update={"acceptance_criteria": tuple(
        item.model_copy(update={"verification": "protected_test"})
        for item in spec.acceptance_criteria)})
    validator.oracle = None
    with pytest.raises(ForgePilotError, match="Protected Oracle is not configured"):
        await validator.capture(task, spec, require_oracle=False)


@pytest.mark.asyncio
async def test_unexpected_isolated_runner_error_is_retryable_infrastructure(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    changes = edit(validator_case)
    async def broken_runner(*args, **kwargs):
        raise RuntimeError("isolated worker crashed")
    validator.process_runner = broken_runner
    report = await validator.validate(task, spec, changes)
    assert report.status == "failed"
    assert report.failure_classification == "infrastructure_failure"
    assert report.hidden_oracle == "not_run"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,classification", [
    ("external_oracle_failed", "code_failure"),
    ("in_process_oracle_integrity_unverified", "infrastructure_failure"),
    ("evaluator_worker_failed", "infrastructure_failure"),
    ("invalid_evaluator_worker_result", "infrastructure_failure"),
])
async def test_real_oracle_adapter_reason_preserves_failure_classification(validator_case, reason, classification):
    _, task, _, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    changes = edit(validator_case)
    async def oracle_runner(_root):
        return {"status": "failed", "reason": reason, "commands": []}
    validator.oracle_runner = oracle_runner
    report = await validator.validate(task, spec, changes)
    assert report.status == "failed"
    assert report.failure_classification == classification
    assert report.hidden_oracle == "failed"
    assert "hidden diagnostics withheld" in report.summary


@pytest.mark.asyncio
async def test_separate_test_approval_still_cannot_weaken_assertion(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    spec = spec.model_copy(update={"test_policy": ArtifactTestPolicy(
        modifiable_existing_test_patterns=("tests/check.cpp",))})
    await validator.capture(task, spec, approved_test_files=("tests/check.cpp",))
    changes = edit(validator_case, "tests/check.cpp", "void check(){ assert(true); }\n")
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "policy_failure" and report.test_integrity == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("validator_case", ["runtime_skip"], indirect=True)
async def test_ctest_runtime_skip_is_policy_failure(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    changes = edit(validator_case)
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "policy_failure"
    assert report.test_integrity == "failed"
    assert any(command.kind == "test" for command in report.commands)
    assert report.hidden_oracle == "not_run"


@pytest.mark.asyncio
async def test_oracle_failure_has_only_fixed_summary(validator_case):
    _, task, _, validator, spec, oracle, _ = validator_case
    source = oracle / "CMakeLists.txt"
    source.write_text(source.read_text().replace('-E true', '-E false'))
    await validator.capture(task, spec)
    changes = edit(validator_case)
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "code_failure"
    assert report.hidden_oracle == "failed"
    assert "PRIVATE_ORACLE" not in report.model_dump_json()
    assert str(oracle) not in report.model_dump_json()
    assert all(item.output_summary.startswith("Protected Oracle") for item in report.commands if item.kind == "protected_test")


@pytest.mark.asyncio
async def test_configure_log_limit_is_infrastructure_and_bounds_report(validator_case):
    _, task, root, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    content = (root / "CMakeLists.txt").read_text() + 'string(REPEAT "output" 50000 noisy)\nmessage("${noisy}")\n'
    changes = edit(validator_case, "CMakeLists.txt", content)
    validator.output_limit = 1024
    report = await validator.validate(task, spec, changes)
    assert report.failure_classification == "infrastructure_failure"
    assert "output_limit" in report.summary
    assert all(len(item.output_summary) <= 8192 for item in report.commands)


@pytest.mark.asyncio
async def test_cancelled_validator_returns_no_success(validator_case):
    _, task, _, validator, spec, _, _ = validator_case
    await validator.capture(task, spec)
    changes = edit(validator_case)
    report = await validator.validate(task, spec, changes, is_cancelled=lambda: True)
    assert report.status == "cancelled" and report.failure_classification == "cancelled"
    assert report.hidden_oracle == "not_run"


@pytest.mark.asyncio
async def test_cancellation_while_configuring_kills_owned_process_tree(validator_case, monkeypatch):
    from forgepilot import build_process
    _, task_id, root, validator, spec, _, build = validator_case
    await validator.capture(task_id, spec)
    content = (root / "CMakeLists.txt").read_text() + (
        'execute_process(COMMAND "${CMAKE_COMMAND}" -E touch "${CMAKE_BINARY_DIR}/started.txt")\n'
        'execute_process(COMMAND "${CMAKE_COMMAND}" -E sleep 30)\n')
    changes = edit(validator_case, "CMakeLists.txt", content)
    jobs = []
    original = build_process.ProcessJob

    class ObservedJob(original):
        def __init__(self):
            super().__init__()
            jobs.append(self)

    monkeypatch.setattr(build_process, "ProcessJob", ObservedJob)
    cancelled = asyncio.Event()
    operation = asyncio.create_task(validator.validate(task_id, spec, changes, is_cancelled=cancelled.is_set))
    async with asyncio.timeout(60):
        while not (build / task_id / "build/started.txt").exists():
            if operation.done():
                pytest.fail(str(operation.result()))
            await asyncio.sleep(.03)

    class ProcessIds(ctypes.Structure):
        _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                    ("pids", ctypes.c_size_t * 64)]

    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    query.restype = wintypes.BOOL
    processes = ProcessIds()
    assert query(jobs[-1].handle, 3, ctypes.byref(processes), ctypes.sizeof(processes), None)
    pids = list(processes.pids[:processes.count])
    assert len(pids) >= 2  # Worker plus CMake, and normally its sleep child.
    cancelled.set()
    report = await asyncio.wait_for(operation, timeout=20)
    assert report.status == "cancelled" and report.failure_classification == "cancelled"
    assert all(not alive(pid) for pid in pids)
    assert all(job.handle is None for job in jobs)
