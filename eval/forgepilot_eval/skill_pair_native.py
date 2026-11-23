"""Run the existing micro-benchmark's executable Qt grader on one offline candidate."""
import hashlib
import os
from pathlib import Path

from forgepilot.process import executable

from .grader import Grader
from .manifest import inventory


def _file_hash(path):
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class NativeValidator:
    def __init__(self):
        self.kit = Path(os.environ.get("FORGEPILOT_QT_KIT", ""))
        if not self.kit.is_absolute() or not (self.kit / "lib/cmake/Qt6/Qt6Config.cmake").is_file():
            raise ValueError("Run scripts/Run-SkillPairs.cmd with the configured MSVC Qt kit")
        self.cmake = executable(os.environ.get("FORGEPILOT_CMAKE"), "cmake")
        self.ctest = executable(str(Path(self.cmake).with_name("ctest.exe")), "ctest")
        self.ninja = executable(os.environ.get("FORGEPILOT_NINJA"), "ninja")
        self.timeout = 120
        tools = {
            "cmake": self.cmake, "ctest": self.ctest, "ninja": self.ninja,
            "cl": executable(None, "cl"), "link": executable(None, "link"),
            "moc": self.kit / "bin/moc.exe", "Qt6Cored": self.kit / "bin/Qt6Cored.dll",
            "Qt6Testd": self.kit / "bin/Qt6Testd.dll",
        }
        self.conditions = {
            "name": "forgepilot_eval.grader.Grader", "generator": "Ninja",
            "configuration": "Debug", "command_timeout_seconds": self.timeout,
            "qt_kit": self.kit.name, "tool_sha256": {k: _file_hash(v) for k, v in tools.items()},
            "msvc_version": os.environ.get("VCToolsVersion"), "sdk_version": os.environ.get("WindowsSDKVersion"),
        }

    async def run(self, case, repo, scratch):
        grader = Grader(case.dataset, self.cmake, self.ctest, generator="Ninja",
                        ninja=self.ninja, qt_kit=self.kit, timeout=self.timeout)
        before = inventory(repo, ignore_generated=True)
        result = await grader.run(case.task, repo.resolve(), scratch.resolve())
        result["candidate_unchanged"] = before == inventory(repo, ignore_generated=True)
        result["passed"] = (
            result["configure_status"] == result["build_status"] == result["test_status"] == "passed"
            and result["public_test_count"] == result["oracle_test_count"] == 1
            and result["protected_unchanged"] and result["candidate_unchanged"]
        )
        return result
