"""Offline five-task pairing evidence; no network or paid provider."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgepilot_eval.real_adapter import LockedMissionArm
from forgepilot_eval.real_pair import run_fake_pairs
from forgepilot_eval.manifest import verify_real_lock
from forgepilot_eval.runner import benchmark_model_configuration, parser


@pytest.mark.asyncio
async def test_five_locked_pairs_keep_failed_fake_samples_and_equal_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://fake.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL", "fake-review")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL_FAMILY", "fake-main-family")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL_FAMILY", "fake-review-family")
    config = benchmark_model_configuration(parser().parse_args(["--mode", "real"]), "fake-main")
    lock = json.loads((Path(__file__).parents[2] / "eval/real_tasks/lock.json").read_text())
    tasks = {task.id: task for task in verify_real_lock()}
    tool_names = ("launcher", "cmake", "ctest", "make", "compiler", "rc", "mt")
    isolation = SimpleNamespace(expected_sha256={name: "a" * 64 for name in tool_names},
        **{name: tmp_path / (name + ".exe") for name in tool_names})
    calls = []
    provider_calls = []

    def provider_factory(*_):
        class FakeProvider:
            async def chat(self, messages, tools=None):
                provider_calls.append(messages)
                return {"message": {"role": "assistant", "content": "offline fake"},
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}
        return FakeProvider()
    provider_factory.forgepilot_offline_fake = True

    def gateway_factory(task, topology):
        return SimpleNamespace(router=config.router(), _provider_factory=provider_factory,
            aclose=lambda: asyncio.sleep(0))

    def prepare(task_id, topology, checkout, *, git):
        checkout.mkdir()
        return LockedMissionArm(tasks[task_id], topology, checkout, lock["sha256"])

    async def execute(arm, *, gateway, isolation, output, git, rg, task_timeout,
                      run_index, config_hash):
        output.mkdir()
        await gateway._provider_factory().chat([{"role": "user", "content": arm.task.id}])
        calls.append((arm.task.id, arm.topology))
        row = {"id": arm.task.id, "agent_topology": arm.topology,
               "success": False, "reason": "fake_provider_declined",
               "config_hash": config_hash, "artifact_directory": str(output)}
        (output / "result.json").write_text(json.dumps(row))
        return row

    output = tmp_path / "evidence"
    report = await run_fake_pairs(output, gateway_factory=gateway_factory,
        isolation=isolation, prepare=prepare, execute=execute)
    saved = json.loads((output / "pairs.json").read_text())
    assert report == saved
    assert saved["complete"] and saved["paid_model_calls"] == 0
    assert len(calls) == 10 and len(saved["pairs"]) == 5
    assert len(provider_calls) == 10
    assert all(pair["conditions_equal"] for pair in saved["pairs"])
    assert all(not arm["result"]["success"] for pair in saved["pairs"]
               for arm in pair["arms"].values())
    for pair in saved["pairs"]:
        for topology, arm in pair["arms"].items():
            evidence = output / pair["task_id"] / topology / "evidence"
            assert arm["evidence_directory"] == str(evidence)
            assert arm["mission_directory"] == arm["result"]["artifact_directory"] == str(evidence / "mission")
            assert (evidence / "conditions.json").is_file()


@pytest.mark.asyncio
async def test_preparation_failure_is_retained_and_does_not_replace_task(tmp_path, monkeypatch):
    from forgepilot_eval import real_pair

    tasks = verify_real_lock()
    monkeypatch.setattr(real_pair, "verify_real_lock", lambda: tasks)
    def prepare(task_id, topology, checkout, *, git):
        if task_id == tasks[1].id:
            raise RuntimeError("pinned checkout unavailable")
        checkout.mkdir()
        lock = json.loads((Path(__file__).parents[2] / "eval/real_tasks/lock.json").read_text())
        return LockedMissionArm(next(t for t in tasks if t.id == task_id), topology,
                                checkout, lock["sha256"])
    def gateway_factory(*_):
        raise AssertionError("Execution must not start without complete preparation")
    # All other tasks reach the gateway factory and fail, but their slots remain.
    report = await run_fake_pairs(tmp_path / "failed", gateway_factory=gateway_factory,
        isolation=None, prepare=prepare)
    assert len(report["pairs"]) == 5
    failed = report["pairs"][1]
    assert failed["task_id"] == tasks[1].id
    assert not failed["conditions_equal"]
    assert [failed["arms"][t]["result"]["reason"] for t in ("single", "multi")] == [
        "preparation_failed", "preparation_failed"]


@pytest.mark.asyncio
async def test_default_executor_reaches_mission_creation_and_keeps_failed_evidence(
        tmp_path, monkeypatch, git_exe, rg_exe):
    from functools import partial

    from forgepilot.models.gateway import ModelGateway
    from forgepilot.workspace_manager import WorkspaceManager
    from forgepilot_eval import real_adapter
    from forgepilot_eval.isolation import IsolationConfig

    monkeypatch.setenv("FORGEPILOT_BASE_URL", "https://fake.invalid/v1")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL", "fake-review")
    monkeypatch.setenv("FORGEPILOT_TASK_MODEL_FAMILY", "fake-main-family")
    monkeypatch.setenv("FORGEPILOT_REVIEW_MODEL_FAMILY", "fake-review-family")
    config = benchmark_model_configuration(parser().parse_args(["--mode", "real"]), "fake-main")
    tasks = {task.id: task for task in verify_real_lock()}
    lock = json.loads((Path(__file__).parents[2] / "eval/real_tasks/lock.json").read_text())
    tool_names = ("launcher", "cmake", "ctest", "make", "compiler", "rc", "mt")
    tools = {name: tmp_path / (name + ".exe") for name in tool_names}
    for path in tools.values():
        path.write_bytes(b"offline tool boundary; never executed")
    isolation = IsolationConfig(env={}, expected_sha256={
        name: sha256(path.read_bytes()).hexdigest() for name, path in tools.items()},
                               **tools)

    def provider_factory(*_):
        raise AssertionError("Repository creation fails before any model call")
    provider_factory.forgepilot_offline_fake = True

    def gateway_factory(task, topology):
        return ModelGateway(config, secret_resolver=lambda _: "offline-only",
                            provider_factory=provider_factory)

    def prepare(task_id, topology, checkout, *, git):
        checkout.mkdir()
        return LockedMissionArm(tasks[task_id], topology, checkout, lock["sha256"])

    class FakeDomain:
        closed = []

        def __init__(self, launcher):
            pass

        def close(self):
            self.closed.append(self)

    monkeypatch.setattr(real_adapter, "IsolatedCandidateRunner",
        partial(real_adapter.IsolatedCandidateRunner, domain_factory=FakeDomain))
    mission_creations = []

    def fail_workspace_creation(self, repository_path, task_id, *, base_ref):
        mission_creations.append((Path(repository_path), base_ref))
        raise RuntimeError("offline repository creation failure")

    monkeypatch.setattr(WorkspaceManager, "create", fail_workspace_creation)
    mkdtemp = real_adapter.tempfile.mkdtemp
    monkeypatch.setattr(real_adapter.tempfile, "mkdtemp",
                        lambda **kwargs: mkdtemp(dir=tmp_path, **kwargs))

    output = tmp_path / "paired"
    # Keep the default executor: the evidence/mission ownership handoff is under test.
    report = await run_fake_pairs(output, gateway_factory=gateway_factory,
        isolation=isolation, prepare=prepare, git=git_exe, rg=rg_exe)
    assert len(mission_creations) == 10
    assert len(FakeDomain.closed) == 10
    assert report == json.loads((output / "pairs.json").read_text())
    assert report["complete"] and report["paid_model_calls"] == 0
    assert [pair["task_id"] for pair in report["pairs"]] == list(tasks)
    for pair in report["pairs"]:
        assert pair["conditions_equal"]
        for topology, arm in pair["arms"].items():
            evidence = output / pair["task_id"] / topology / "evidence"
            mission = evidence / "mission"
            assert arm["evidence_directory"] == str(evidence)
            assert arm["mission_directory"] == str(mission)
            conditions = json.loads((evidence / "conditions.json").read_text())
            assert conditions["task_id"] == pair["task_id"]
            row = arm["result"]
            assert not row["success"]
            assert row["reason"] == "evaluation_error:RuntimeError"
            assert row["artifact_directory"] == str(mission)
            assert row == json.loads((mission / "result.json").read_text())
            assert json.loads((mission / "mission.json").read_text()) is None
            assert (Path(row["repository"]), tasks[pair["task_id"]].base_commit) in mission_creations
            state = Path(row["state_directory"])
            assert state.is_relative_to(tmp_path) and (state / "mission.sqlite3").is_file()
