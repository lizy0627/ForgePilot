"""Bounded failure evidence and conservative validation-integrity policy."""

from collections import Counter
import json
from pathlib import PurePosixPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import ForgePilotError
from .planning import TaskPlan, _unique_object, validate_plan

MAX_REPAIR_ROUNDS = 3


class Reflection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: Literal["code", "environment", "credentials", "unknown"]
    plan: TaskPlan


def parse_reflection(text, normalize_path):
    try:
        if len(text) > 32_768:
            raise ValueError()
        value = Reflection.model_validate(json.loads(text, object_pairs_hook=_unique_object))
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ForgePilotError("invalid_reflection", "Expected a category and a complete repair plan JSON object") from None
    plan = validate_plan(value.plan.model_dump(mode="json"), normalize_path)
    if not plan.root_cause:
        raise ForgePilotError("invalid_reflection", "Reflection must explain the observed root cause")
    return value.category, plan


def classify_failure(report, phase):
    """Logs may veto repair, but never grant permission or advance task state."""
    commands = [c for c in report["commands"] if c["phase"] == phase]
    if report["stages"][phase] in {"timed_out", "output_limit", "cancelled"}:
        return report["stages"][phase]
    if not commands or commands[-1]["returncode"] is None:
        return "environment"
    command = commands[-1]
    if command["status"] != "failed":
        return "validation"  # Invalid/missing report, cache, limits or other infrastructure errors.
    diagnostic = (command["stdout"] + "\n" + command["stderr"]).casefold()
    if re.search(r"\b(?:401|403)\b|authentication failed|unauthorized|invalid api.?key|permission denied.*credential", diagnostic):
        return "credentials"
    if re.search(r"not recognized as an internal|command not found|no such file or directory|cannot find (?:the file|the compiler)|"
                 r"unable to find executable|could not find.*(?:compiler|ninja|cmake|package)|"
                 r"could not load cache|could not find cmake_project_name|could not find.*build program|"
                 r"ninja: error: loading|createprocess failed|no cmake_cxx_compiler|"
                 r"fatal error lnk1104", diagnostic):
        return "environment"
    if phase == "cmake_configure":
        return "configuration"  # Phase 5 repairs Build/Test failures; configure needs human attention.
    if phase == "run_tests":
        if report["tests"].get("not_run") or not report["tests"].get("failed"):
            return "validation"
        if re.search(r"\*\*\*timeout|test timeout computed|<timeout>", diagnostic):
            return "timed_out"
    return "code"


def excerpt(text, limit=16_000):
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[TRUNCATED: middle omitted; full bounded log in validation_history]\n" + text[-limit // 2:]


# Freeze existing test/expectation files rather than attempt semantic equivalence of C++ assertions.
_CHECK = re.compile(r"\b(?:assert|static_assert|Q_ASSERT(?:_X)?|QVERIFY\w*|QCOMPARE\w*|QFAIL|"
                    r"ASSERT_\w+|EXPECT_\w+|REQUIRE\w*|CHECK\w*|TEST(?:_F|_P|_CASE|_SUITE)?)\s*\(")
_SUPPRESS = re.compile(r"\bNDEBUG\b|\bQT_NO_DEBUG\b|#\s*if\s+0\b|#\s*pragma\s+warning\s*\(\s*disable|"
                       r"-Wno-[\w-]+|(?<!\w)-w\b|/W0\b|\b(?:GTEST_SKIP|QSKIP)\s*\(|"
                       r"\b(?:WILL_FAIL|DISABLED|PASS_REGULAR_EXPRESSION|SKIP_RETURN_CODE|SKIP_REGULAR_EXPRESSION)\b", re.I)


def _cmake_checks(text):
    # A deliberately conservative line inventory includes surrounding conditionals and error reporting.
    return Counter(line.strip() for line in text.splitlines() if re.search(
        r"\b(?:add_test|enable_testing|include\s*\(\s*CTest|set_tests_properties|set_property|"
        r"BUILD_TESTING|CMAKE_(?:CXX|C)_FLAGS|target_compile_(?:options|definitions)|"
        r"if|else|elseif|endif|return|message)\b", line, re.I))


def ensure_validation_integrity(path, current, content):
    """Called under the exclusive write handle, before snapshot/mutation. No model overrides."""
    old = (current or b"").decode("utf-8-sig")
    if old == content:
        return
    p = PurePosixPath(path.casefold())
    is_test = any(part in {"test", "tests", "testing"} for part in p.parts) or bool(
        re.search(r"(?:^test[_.-]|[_.-]test[s]?\.|^tst_|test\.cpp$)", p.name))
    if current is not None and (is_test or _CHECK.search(old)):
        raise ForgePilotError("validation_integrity", "Existing test/expectation files are immutable during automatic validated changes")
    if Counter(_SUPPRESS.findall(content)) - Counter(_SUPPRESS.findall(old)):
        raise ForgePilotError("validation_integrity", "Adding test/error suppression is forbidden")
    if p.name == "cmakelists.txt" or p.suffix == ".cmake":
        if _cmake_checks(old) - _cmake_checks(content):
            raise ForgePilotError("validation_integrity", "Removing/changing existing test registration, control flow or error checks is forbidden")
        # Existing multiline test/config commands must retain their entire body.
        for match in re.finditer(r"\b(?:add_test|set_tests_properties|set_property|target_compile_definitions|target_compile_options)\s*\(", old, re.I):
            end, depth = match.end(), 1
            while end < len(old) and depth:
                depth += (old[end] == "(") - (old[end] == ")")
                end += 1
            if old[match.start():end] not in content:
                raise ForgePilotError("validation_integrity", "Existing validation command bodies must be preserved")
