"""A behavioral probe needs complete, framed responses."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.external_probe import make_challenge, verify_response, write_probe_project
from forgepilot_eval.manifest import load_tasks, verify_real_lock
from forgepilot_eval.grader import check_scope, read_junit
from forgepilot_eval.manifest import check_real_test_integrity


MICRO_TASKS = ("cpp-compile-01", "cpp-compile-02", "cpp-compile-03",
               "cpp-logic-01", "cpp-logic-02", "cpp-logic-03",
               "cmake-01", "cmake-02", "unit-test-01", "unit-test-02",
               "cross-file-01", "repair-01", "qt-signal-01", "qt-signal-02")

NATIVE_REAL_TASKS = ("real-bug-01", "real-bug-03", "real-feature-01", "real-feature-02")


def test_every_locked_task_has_a_process_external_decision():
    micro = {task.id for task in load_tasks()}
    real = {task.id for task in verify_real_lock()}
    assert micro == set(MICRO_TASKS) | {"analysis-01"}
    assert real == set(NATIVE_REAL_TASKS) | {"real-bug-02"}


@pytest.mark.parametrize("task_id", MICRO_TASKS + NATIVE_REAL_TASKS)
def test_every_native_challenge_rejects_missing_or_partial_execution(task_id):
    challenge = make_challenge(task_id, seed=173, nonce="b" * 32)
    lines = [f"FP-PROBE-1 {challenge.nonce} {len(challenge.cases)}",
             *(f"{index} {case.expected}" for index, case in enumerate(challenge.cases))]
    complete = ("\n".join(lines) + "\n").encode("ascii")
    assert verify_response(challenge, returncode=0, stdout=complete)
    assert not verify_response(challenge, returncode=0, stdout=b"")
    assert not verify_response(challenge, returncode=0, stdout=("\n".join(lines[:-1]) + "\n").encode("ascii"))
    assert not verify_response(challenge, returncode=0, stdout=complete + b"0 0\n")
    assert not verify_response(challenge, returncode=1, stdout=complete)


@pytest.mark.parametrize("task_id", MICRO_TASKS)
def test_each_micro_task_rejects_public_test_removal_or_weakening(task_id):
    task = next(task for task in load_tasks() if task.id == task_id)
    before = {"tests/public_test.cpp": "assert(result == expected);"}
    after = {"tests/public_test.cpp": "assert(true);"}
    assert not check_scope(before, after, task)["valid"]
    assert not check_scope(before, {}, task)["valid"]


@pytest.mark.parametrize("task_id", NATIVE_REAL_TASKS + ("real-bug-02",))
def test_each_real_task_rejects_skipped_or_removed_ctest(task_id, tmp_path):
    task = next(task for task in verify_real_lock() if task.id == task_id)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    assert not check_real_test_integrity(task, baseline, candidate, ["existing"], [])
    report = tmp_path / "skipped.xml"
    report.write_text('<testsuite><testcase name="oracle"><skipped/></testcase></testsuite>', encoding="utf-8")
    assert read_junit(report, "oracle", True)["status"] == "failed"


@pytest.mark.parametrize("task_id", ["cpp-compile-01", "real-bug-01", "real-bug-03",
                                     "real-feature-01", "real-feature-02"])
def test_complete_framing_is_required(task_id):
    challenge = make_challenge(task_id, seed=73, nonce="a" * 32)
    assert not verify_response(challenge, returncode=0, stdout=b"")
    valid = f"FP-PROBE-1 {challenge.nonce} {len(challenge.cases)}\n".encode()
    valid += b"".join(f"{index} {case.expected}\n".encode()
                      for index, case in enumerate(challenge.cases))
    assert verify_response(challenge, returncode=0, stdout=valid)
    assert not verify_response(challenge, returncode=1, stdout=valid)
    assert not verify_response(challenge, returncode=0, stdout=valid + b"extra\n")


def test_probe_project_has_no_oracle_source_or_expected_values(tmp_path):
    source = write_probe_project("cpp-compile-01", tmp_path / "probe", candidate_include=tmp_path / "candidate",
                                 candidate_library=tmp_path / "subject.lib")
    assert sorted(path.name for path in source.iterdir()) == ["CMakeLists.txt", "probe.cpp"]
    assert "FORGEPILOT_ORACLE_TOKEN" not in (source / "probe.cpp").read_text()
    assert "expect(" not in (source / "probe.cpp").read_text()
    with pytest.raises(ValueError, match="contract"):
        make_challenge("unknown", seed=1)


@pytest.mark.parametrize("task_id", MICRO_TASKS)
def test_every_locked_micro_modification_has_external_contract(task_id, tmp_path):
    challenge = make_challenge(task_id, seed=73)
    assert challenge.cases
    source = write_probe_project(task_id, tmp_path / "probe",
        candidate_include=tmp_path / "candidate", candidate_library=tmp_path / "subject.lib")
    assert "FORGEPILOT_ORACLE_TOKEN" not in (source / "probe.cpp").read_text()


@pytest.mark.parametrize("task_id", ["real-bug-01", "real-bug-03", "real-feature-01",
                                     "real-feature-02"])
def test_locked_real_native_tasks_have_external_contract(task_id, tmp_path):
    challenge = make_challenge(task_id, seed=73)
    assert challenge.cases
    source = write_probe_project(task_id, tmp_path / "probe",
        candidate_include=tmp_path / "include",
        candidate_source=tmp_path / "pugixml.cpp" if task_id == "real-feature-02" else None)
    assert "FORGEPILOT_ORACLE_TOKEN" not in (source / "probe.cpp").read_text()
