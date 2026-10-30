"""Locked real-task definitions and evaluator-side isolation gates."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.integrity import (
    IntegrityMonitor, _assertions, _canonical, _cmake_commands,
    _control_flow, _CMAKE_FLOW,
)
from forgepilot.repository import Repository

from forgepilot_eval.manifest import (
    DATA_ROOT, RealTaskSpec, check_real_test_integrity, load_real_tasks, lock_real_tasks,
    verify_real_lock,
)


def dataset_copy(tmp_path):
    root = tmp_path / "eval"
    shutil.copytree(DATA_ROOT / "real_tasks", root / "real_tasks")
    return root


def test_five_public_issues_are_frozen_and_map_to_runtime_task_spec():
    tasks = verify_real_lock()
    assert Counter(task.kind for task in tasks) == {"bug": 3, "feature": 2}
    assert len({task.issue_url for task in tasks}) == 5
    assert all(len(task.base_commit) == 40 for task in tasks)
    for task in tasks:
        artifact = task.to_artifact_task_spec()
        assert artifact.repository.base_ref == task.base_commit
        assert artifact.allowed_scope.path_patterns == tuple(task.task_spec.allowed_files)
        assert task.to_integrity_task_spec().test_files_to_modify == tuple(
            task.task_spec.integrity_policy.editable_existing_test_files)
        assert not (DATA_ROOT / task.protected_oracle).is_relative_to(DATA_ROOT / "fixtures")
        assert "protected_oracle" not in artifact.model_dump_json()
        assert "issue_url" not in artifact.model_dump_json()
        assert "assertion_updates" not in artifact.model_dump_json()
        assert "control_flow_additions" not in artifact.model_dump_json()


@pytest.mark.parametrize("tamper", ["manifest", "oracle", "extra_asset"])
def test_locked_assets_reject_every_kind_of_drift(tmp_path, tamper):
    root = dataset_copy(tmp_path)
    assert len(verify_real_lock(root)) == 5
    if tamper == "manifest":
        path = root / "real_tasks" / "tasks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["title"] = "Changed after lock"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif tamper == "oracle":
        path = root / "real_tasks" / "protected_oracles" / "bug_unmatched_short.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    else:
        (root / "real_tasks" / "extra.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Locked real-task"):
        verify_real_lock(root)


def test_existing_lock_cannot_be_replaced(tmp_path):
    root = dataset_copy(tmp_path)
    with pytest.raises(FileExistsError, match="already locked"):
        lock_real_tasks(root)
    (root / "real_tasks" / "lock.json").unlink()
    first = lock_real_tasks(root)
    assert first["sha256"] == json.loads((DATA_ROOT / "real_tasks" / "lock.json").read_text())["sha256"]
    with pytest.raises(FileExistsError):
        lock_real_tasks(root)


def test_real_manifest_rejects_distribution_scope_and_path_escape(tmp_path):
    root = dataset_copy(tmp_path)
    path = root / "real_tasks" / "tasks.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for mutation in ("duplicate_id", "scope_escape", "oracle_escape"):
        data = json.loads(json.dumps(original))
        if mutation == "duplicate_id":
            data["tasks"][1]["id"] = data["tasks"][0]["id"]
        elif mutation == "scope_escape":
            data["tasks"][0]["task_spec"]["allowed_files"] = ["../secret"]
        else:
            data["tasks"][0]["protected_oracle"] = "../oracle.py"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            load_real_tasks(root)


def test_existing_test_edits_preserve_assertions_control_and_ctest_names(tmp_path):
    task = next(task for task in verify_real_lock() if task.id == "real-bug-03")
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "test").mkdir(parents=True)
    (baseline / "test/options.cpp").write_text('TEST_CASE("old") { CHECK(answer == 42); }\n', encoding="utf-8")
    target = candidate / "test/options.cpp"
    target.write_text('TEST_CASE("old") { int helper = 1; CHECK(answer == 42); }\n'
                      'TEST_CASE("new") { CHECK(helper == 1); }\n', encoding="utf-8")
    assert check_real_test_integrity(task, baseline, candidate, ["old"], ["old", "new"])
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["new"])
    assert not check_real_test_integrity(task, baseline, candidate, [], ["new"])
    target.write_text('TEST_CASE("new") { CHECK(helper == 1); }\n', encoding="utf-8")
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])
    target.write_text('TEST_CASE("old") { CHECK(answer == 42); }\n#if 0\n', encoding="utf-8")
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])


def _task_with_assertion_update(task, baseline_bytes, old, new):
    data = task.model_dump()
    old_removed = _assertions(old) - _assertions(new)
    new_added = _assertions(new) - _assertions(old)
    assert len(old_removed) == len(new_added) == 1
    data["task_spec"]["integrity_policy"]["assertion_updates"] = [{
        "path": "test/options.cpp",
        "baseline_file_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "old_assertion_sha256": hashlib.sha256(next(iter(old_removed)).encode()).hexdigest(),
        "new_assertion_sha256": hashlib.sha256(next(iter(new_added)).encode()).hexdigest(),
    }]
    return RealTaskSpec.model_validate(data)


def test_one_base_bound_assertion_correction_requires_exact_new_assertion(tmp_path):
    task = next(task for task in verify_real_lock() if task.id == "real-bug-01")
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "test").mkdir(parents=True)
    old = 'TEST_CASE("old") { CHECK(value == 0); CHECK(other == 1); }\n'
    new = 'TEST_CASE("old") { CHECK(value == 1); CHECK(other == 1); }\n'
    baseline_bytes = old.encode()
    task = _task_with_assertion_update(task, baseline_bytes, old, new)
    (baseline / "test/options.cpp").write_bytes(baseline_bytes)
    target = candidate / "test/options.cpp"
    target.write_text(new, encoding="utf-8")
    assert check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])
    target.write_text(new.replace("other == 1", "other == 0"), encoding="utf-8")
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])
    target.write_text(new.replace("value == 1", "value == 2"), encoding="utf-8")
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])
    target.write_text(new, encoding="utf-8")
    (baseline / "test/options.cpp").write_text(old + "\n", encoding="utf-8")
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old"])


def test_production_integrity_monitor_uses_trusted_assertion_rule(tmp_path):
    (tmp_path / "test").mkdir()
    old = 'TEST_CASE("old") { CHECK(value == 0); CHECK(other == 1); }\n'
    new = 'TEST_CASE("old") { CHECK(value == 1); CHECK(other == 1); }\n'
    path = tmp_path / "test/options.cpp"
    path.write_bytes(old.encode())
    task = next(task for task in verify_real_lock() if task.id == "real-bug-01")
    task = _task_with_assertion_update(task, old.encode(), old, new)
    monitor = IntegrityMonitor(Repository(tmp_path.resolve(), "rg"), task.to_integrity_task_spec())
    monitor.capture(1, approved_test_files=("test/options.cpp",), ctest_names=("old",))
    monitor.guard("test/options.cpp", old.encode(), new)
    with pytest.raises(ForgePilotError, match="assertion"):
        monitor.guard("test/options.cpp", old.encode(), new.replace("other == 1", "other == 0"))
    path.write_text(new, encoding="utf-8")
    assert monitor.verify(ctest_names=("old",))["status"] == "passed"


def _task_with_control_additions(task, baseline_bytes, old, new, path, kind):
    data = task.model_dump()
    if kind == "cmake":
        before = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(old)
                         if name in _CMAKE_FLOW)
        after = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(new)
                        if name in _CMAKE_FLOW)
        def digest(value):
            return hashlib.sha256((value[0] + "\0" + value[1]).encode()).hexdigest()
    else:
        before, after = _control_flow(old), _control_flow(new)
        def digest(value):
            return hashlib.sha256(value.encode()).hexdigest()
    assert not before - after
    added = after - before
    assert added
    data["task_spec"]["integrity_policy"]["control_flow_additions"] = [
        {"path": path, "baseline_file_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
         "kind": kind, "signature_sha256": digest(value)}
        for value, count in added.items() for _ in range(count)]
    return RealTaskSpec.model_validate(data)


@pytest.mark.parametrize("task_id,path,kind,old,addition", [
    ("real-bug-03", "test/options.cpp", "cpp",
     'TEST_CASE("old") { CHECK(value == 1); }\n',
     'for (auto value : {1, 2}) { CHECK(value > 0); }\n'),
    ("real-bug-02", "test/CMakeLists.txt", "cmake",
     'add_test(NAME old COMMAND old)\n',
     'foreach(value a b)\nadd_test(NAME new-${value} COMMAND new)\nendforeach()\n'),
])
def test_only_pinned_new_test_control_flow_is_allowed(tmp_path, task_id, path, kind, old, addition):
    task = next(task for task in verify_real_lock() if task.id == task_id)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / Path(path).parent).mkdir(parents=True)
    (baseline / path).write_bytes(old.encode())
    new = old + addition
    task = _task_with_control_additions(task, old.encode(), old, new, path, kind)
    (candidate / path).write_bytes(new.encode())
    assert check_real_test_integrity(task, baseline, candidate, ["old"], ["old", "new"])
    monitor = IntegrityMonitor(Repository(baseline.resolve(), "rg"), task.to_integrity_task_spec())
    monitor.capture(1, approved_test_files=(path,), ctest_names=("old",))
    monitor.guard(path, old.encode(), new)
    if kind == "cpp":
        tampered = old + 'if (false) { CHECK(value == 1); }\n' + addition
    else:
        tampered = 'if(FALSE)\n' + old + 'endif()\n' + addition
    (candidate / path).write_bytes(tampered.encode())
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old", "new"])
    with pytest.raises(ForgePilotError, match="control flow"):
        monitor.guard(path, old.encode(), tampered)
    (baseline / path).write_bytes(old.encode() + b"\n")
    (candidate / path).write_bytes(new.encode())
    assert not check_real_test_integrity(task, baseline, candidate, ["old"], ["old", "new"])


def test_init_rejects_junction_parent_resolving_into_project(tmp_path, monkeypatch):
    from importlib.util import module_from_spec, spec_from_file_location

    init_file = DATA_ROOT / "real_tasks" / "init" / "init_repository.py"
    spec = spec_from_file_location("real_init_repository_test", init_file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires elevated Windows privileges")
    monkeypatch.setattr(module, "PROJECT_ROOT", project)
    with pytest.raises(ValueError, match="outside ForgePilot"):
        module.prepare("real-bug-01", alias / "candidate")


def test_init_rejects_direct_candidate_inside_project():
    from importlib.util import module_from_spec, spec_from_file_location

    init_file = DATA_ROOT / "real_tasks" / "init" / "init_repository.py"
    spec = spec_from_file_location("real_init_direct_path_test", init_file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="outside ForgePilot"):
        module.prepare("real-bug-01", DATA_ROOT / "real_tasks" / "candidate")


def test_init_fetch_failure_leaves_visible_incomplete_marker(tmp_path, monkeypatch):
    from importlib.util import module_from_spec, spec_from_file_location
    from types import SimpleNamespace

    init_file = DATA_ROOT / "real_tasks" / "init" / "init_repository.py"
    spec = spec_from_file_location("real_init_failure_test", init_file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "candidate"
    monkeypatch.setattr(module.shutil, "which", lambda name: "git")

    def fake_git(argv, **_kwargs):
        if "init" in argv:
            destination.mkdir()
        return SimpleNamespace(returncode=1 if "fetch" in argv else 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_git)
    with pytest.raises(RuntimeError, match="preparation failed"):
        module.prepare("real-bug-01", destination)
    assert (destination / ".forgepilot-preparation-failed").read_text() == "incomplete\n"
