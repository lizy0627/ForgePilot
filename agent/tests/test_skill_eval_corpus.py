"""The frozen cases retain their intended split, candidate boundary and native outcomes."""
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
from forgepilot.artifacts.canonical import content_sha256
from forgepilot_eval.grader import Grader
from forgepilot_eval.manifest import inventory
from skill_cases import load_cases


def test_skill_corpus_lock_and_candidate_boundary(tmp_path):
    cases = load_cases()
    assert [c.task.id for c in cases[:2]] == ["qt-signal-01", "qt-signal-02"]
    assert [c.split for c in cases] == ["development"] * 2 + ["holdout"] * 3 + ["counterexample"] * 2
    assert [c.relevant for c in cases] == [False] * 2 + [True] * 3 + [False] * 2
    assert len({c.locks["fixture"] for c in cases}) == 7
    for case in cases:
        repo = tmp_path / case.task.id
        case.copy_fixture(repo)
        assert set(inventory(repo)) == {
            "PROJECT.txt", "CMakeLists.txt", "cmake/Subject.cmake", "src/subject.h",
            "src/subject.cpp", "tests/public_test.cpp",
        }
        assert content_sha256(inventory(repo)) == case.locks["fixture"]
        assert case.oracle.is_file() and not case.oracle.is_relative_to(repo)
        for name, fixed in case.solution["files"].items():
            assert name in case.task.allowed_files
            assert (repo / name).read_text(encoding="utf-8") != fixed


@pytest.mark.skipif(
    os.name != "nt" or not os.getenv("FORGEPILOT_QT_KIT"),
    reason="Native corpus check requires Windows and a configured FORGEPILOT_QT_KIT",
)
@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case.task.id)
@pytest.mark.asyncio
async def test_skill_native_case(tmp_path, case):
    grader = Grader(case.dataset, shutil.which("cmake"), shutil.which("ctest"),
                    qt_kit=os.environ["FORGEPILOT_QT_KIT"])
    repo = tmp_path / "candidate"
    case.copy_fixture(repo)
    before = await grader.run(case.task, repo, tmp_path / "before")
    for name, fixed in case.solution["files"].items():
        (repo / name).write_text(fixed, encoding="utf-8", newline="\n")
    after = await grader.run(case.task, repo, tmp_path / "after")
    for result in (before, after):
        assert result["configure_status"] == result["build_status"] == "passed", result
        assert result["protected_unchanged"]
        assert result["public_test_count"] == result["oracle_test_count"] == 1
    assert before["public_test_status"] == before["oracle_status"] == "failed"
    assert after["public_test_status"] == after["oracle_status"] == "passed"
