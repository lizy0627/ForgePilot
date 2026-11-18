"""Locked real-task Mission inputs are identical across A/B arms."""
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot_eval.manifest import verify_real_lock
from forgepilot_eval.real_adapter import (
    IsolatedCandidateRunner, LockedMissionArm, LockedRealToolchainProfile,
    assert_paired_arms, run_offline_locked_arm, validate_fair_gateway_routes,
)
from forgepilot_eval.runner import benchmark_model_configuration, parser
from forgepilot_eval.isolation import IsolationConfig, IsolationError
from forgepilot_eval.real_oracle_adapter import RealOracleAdapter


def test_oracle_raw_evidence_cannot_overlap_candidate(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    oracle = tmp_path / "oracle.py"
    oracle.write_text("", encoding="utf-8")
    with pytest.raises(IsolationError, match="overlaps candidate"):
        RealOracleAdapter(None, oracle, candidate, evidence_dir=candidate / "evidence")
    assert not (candidate / "evidence").exists()


@pytest.mark.parametrize("task", verify_real_lock(), ids=lambda task: task.id)
def test_real_task_uses_exact_locked_commands_and_separate_oracle(task, tmp_path):
    cmake, ctest = shutil.which("cmake"), shutil.which("ctest")
    if not cmake or not ctest:
        pytest.skip("CMake and CTest are unavailable")
    profile = LockedRealToolchainProfile(task, cmake=cmake, ctest=ctest)
    source = tmp_path / "source"
    source.mkdir()
    build = tmp_path / "build"
    values = {"source_dir": str(source), "build_dir": str(build),
              "report_path": str(tmp_path / "report.xml"), "configuration": "Debug"}
    configure = profile.resolve("cmake_configure", values)
    command = task.task_spec.validation_commands[0]
    assert Path(configure.executable).name.lower().startswith("cmake")
    assert configure.args == tuple(str(build) if token == "{build_dir}" else token
                                   for token in command[1:])
    testing = profile.resolve("run_tests", values)
    assert testing.args[-3:] == ("--no-tests=error", "--output-junit", values["report_path"])
    with pytest.raises(ForgePilotError, match="command_denied"):
        profile.resolve_oracle("cmake_configure", values)


def test_pair_input_binds_same_task_spec_integrity_commands_and_lock(tmp_path):
    task = verify_real_lock()[0]
    repository = tmp_path / "pinned checkout"
    repository.mkdir()
    single = LockedMissionArm(task, "single", repository, "a" * 64)
    multi = LockedMissionArm(task, "multi", repository, "a" * 64)
    bound = assert_paired_arms(single, multi)
    assert bound["base_commit"] == task.base_commit
    assert all(len(bound[key]) == 64 for key in (
        "task_spec_sha256", "integrity_sha256", "validation_commands_sha256",
        "manifest_sha256"))
    with pytest.raises(ValueError, match="different locked"):
        assert_paired_arms(single, LockedMissionArm(task, "multi", repository, "b" * 64))


def test_fair_gateway_rejects_missing_or_unequal_routes(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL_FAMILY", "main-family")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL_FAMILY", "review-family")
    args = parser().parse_args(["--mode", "real"])
    config = benchmark_model_configuration(args, "main-model")
    assert validate_fair_gateway_routes(config) == "benchmark-main"
    routes = list(config.routes)
    from forgepilot.models.profiles import ModelConfiguration
    altered = ModelConfiguration(profiles=config.profiles,
        routes=tuple(route.model_copy(update={"primary": "benchmark-review"})
                     if route.route_id == "analysis" else route for route in routes),
        enforce_review_family_separation=True)
    with pytest.raises(ValueError, match="different primary"):
        validate_fair_gateway_routes(altered)


@pytest.mark.asyncio
async def test_offline_runner_rejects_unmarked_provider_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL_FAMILY", "main-family")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL_FAMILY", "review-family")
    config = benchmark_model_configuration(parser().parse_args(["--mode", "real"]), "main-model")
    arm = LockedMissionArm(verify_real_lock()[0], "single", tmp_path, "a" * 64)
    gateway = SimpleNamespace(router=config.router(), _provider_factory=lambda *_: None)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="marked fake provider"):
        await run_offline_locked_arm(arm, gateway=gateway, isolation=None,
                                     output=output, git="git", rg="rg")
    assert not output.exists()


@pytest.mark.asyncio
async def test_offline_runner_refuses_to_overwrite_existing_evidence(tmp_path, monkeypatch):
    from forgepilot_eval import real_adapter

    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL_FAMILY", "main-family")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL_FAMILY", "review-family")
    config = benchmark_model_configuration(parser().parse_args(["--mode", "real"]), "main-model")

    def provider_factory(*_):
        pytest.fail("Existing evidence must be rejected before any fake model call")
    provider_factory.forgepilot_offline_fake = True
    gateway = SimpleNamespace(router=config.router(), _provider_factory=provider_factory)
    tools = ("launcher", "cmake", "ctest", "make", "compiler", "rc", "mt")
    isolation = IsolationConfig(env={}, **{name: tmp_path / (name + ".exe") for name in tools})
    arm = LockedMissionArm(verify_real_lock()[0], "single", tmp_path, "a" * 64)
    output = tmp_path / "evidence" / "mission"
    output.mkdir(parents=True)
    existing = {output.parent / "conditions.json": b'{"conditions":"original"}\n',
                output / "result.json": b'{"success":false,"reason":"original"}\n',
                output / "mission.json": b'{"task_id":"original"}\n'}
    for path, content in existing.items():
        path.write_bytes(content)

    def forbid_state_directory(*args, **kwargs):
        pytest.fail("Existing evidence must be rejected before creating a database")
    monkeypatch.setattr(real_adapter.tempfile, "mkdtemp", forbid_state_directory)

    with pytest.raises(FileExistsError):
        await run_offline_locked_arm(arm, gateway=gateway, isolation=isolation,
                                     output=output, git="git", rg="rg")

    assert {path: path.read_bytes() for path in output.parent.rglob("*") if path.is_file()} == existing


@pytest.mark.asyncio
async def test_candidate_runner_uses_one_granted_source_and_build_tree(tmp_path):
    class FakeDomain:
        instances = []
        def __init__(self, launcher):
            self.grants = []
            self.runs = []
            self.closed = 0
            self.instances.append(self)
        def grant(self, path, *, writable):
            self.grants.append((path, writable))
        async def run(self, argv, cwd, *, timeout, env):
            self.runs.append((argv, cwd, timeout, env))
            return "isolated"
        def close(self):
            self.closed += 1

    validation_root = tmp_path / "validation"
    build = validation_root / "task" / "build"
    build.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    checks = []
    config = SimpleNamespace(launcher="fixture", env={"PATH": "x"},
                             validate_for=lambda source, build: checks.append((source, build)),
                             grant_read_only=lambda domain: None)
    runner = IsolatedCandidateRunner(config,
                                     validation_root, domain_factory=FakeDomain)
    try:
        assert await runner(["cmake", "-B", str(build)], source, timeout=3, env={}) == "isolated"
        assert await runner(["ctest", "--test-dir", str(build)], source, timeout=3, env={}) == "isolated"
        assert FakeDomain.instances[-1].grants == [(source, True), (build, True)]
        assert checks == [(source, build), (source, build)]
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(Exception, match="changed source workspace"):
            await runner(["cmake", "-B", str(build)], other, timeout=3, env={})
    finally:
        runner.close()
        runner.close()
    assert FakeDomain.instances[-1].closed == 1
