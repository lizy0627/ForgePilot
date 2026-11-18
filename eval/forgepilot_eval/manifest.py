"""Strict, versioned task metadata and link-free fixture assets."""
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forgepilot.repository import is_link
from forgepilot.artifacts.models import TaskSpec as ArtifactTaskSpec
from forgepilot.agents.contracts import validate_task_spec
from forgepilot.integrity import (
    AssertionUpdate, ControlFlowAddition, IntegrityTaskSpec,
    _assertions_preserved, _control_flow_preserved,
    _SUPPRESSION, _comments, _is_cmake, _cmake_commands, _CMAKE_PROTECTED,
    _canonical, _text,
)

DISTRIBUTION = {"C++ 编译错误": 3, "C++ 逻辑 Bug": 3, "Qt Signal/Slot": 2,
                "CMake": 2, "Unit Test": 2, "调用链分析": 1, "跨文件修改": 1, "Repair": 1}
DATA_ROOT = Path(__file__).resolve().parents[1]

# Each step restates an input and observation in its locked issue request. The
# manifest contains no separate reproduction-command field; do not invent one.
REAL_BUG_REPRODUCTION = {
    "real-bug-01": ("Allow unrecognised options, parse -z, and inspect ParseResult::unmatched().",),
    "real-bug-02": ("Configure with an absolute CMAKE_INSTALL_INCLUDEDIR and inspect the generated pkg-config include path.",),
    "real-bug-03": ("Parse floating-point option values 2.5typo and nanXYZ and inspect whether they are accepted.",),
}


def relative(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or value.startswith("/") or any(x in {"", ".", ".."} for x in value.split("/"))):
        raise ValueError("Expected a canonical relative asset path")
    return value


def asset(root, value):
    current = root.resolve()
    for part in PurePosixPath(relative(value)).parts:
        current = current / part
        stat = current.lstat()
        if is_link(stat) or (current.is_file() and stat.st_nlink != 1):
            raise ValueError("Benchmark assets must not be links or hardlinks")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("Asset escaped dataset")
    return current


def inventory(root, *, ignore_generated=False):
    """Do not traverse links, including Windows junctions. No ignored source extensions."""
    result = {}
    def walk(directory):
        for child in sorted(directory.iterdir()):
            if ignore_generated and child.parent == root and child.name in {".git", ".forgepilot"}:
                # Owned Git/build trees are outside source scoring, but cannot become links.
                if is_link(child.lstat()): raise ValueError("Generated directory became a link")
                continue
            if child.name == "__pycache__": continue
            stat = child.lstat()
            if is_link(stat) or (child.is_file() and stat.st_nlink != 1):
                raise ValueError("Linked benchmark source is forbidden")
            if child.is_dir(): walk(child)
            elif child.is_file():
                if stat.st_size > 2_097_152 or len(result) >= 512:
                    raise ValueError("Benchmark source inventory limit")
                result[child.relative_to(root).as_posix()] = hashlib.sha256(child.read_bytes()).hexdigest()
            else: raise ValueError("Unsupported benchmark file")
    if is_link(root.lstat()): raise ValueError("Linked benchmark root")
    walk(root)
    return result


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Initial(Strict):
    expected: str
    description: str


class Criterion(Strict):
    kind: str
    oracle: str | None
    required_changed: list[str]
    minimum_repair_rounds: int = Field(ge=0, le=3)
    expected_behavior: str
    expected: dict | None = None


class TaskSpec(Strict):
    id: str = Field(pattern=r"^[a-z]+(?:-[a-z]+)*-\d{2}$")
    category: str
    title: str
    fixture: str
    request: str = Field(min_length=1, max_length=12_000)
    initial_state: Initial
    allowed_files: list[str]
    preauthorized_files: list[str] | None
    qt: bool
    success: Criterion
    validation_commands: list[list[str]]

    @property
    def analysis(self):
        return self.category == "调用链分析"

    @model_validator(mode="after")
    def validate_task(self):
        if self.category not in DISTRIBUTION: raise ValueError("Unknown benchmark category")
        relative(self.fixture)
        if not self.fixture.startswith("fixtures/"): raise ValueError("Fixture must belong to dataset")
        for values in (self.allowed_files, self.preauthorized_files or [], self.success.required_changed):
            if len({v.casefold() for v in values}) != len(values): raise ValueError("Duplicate file scope")
            for value in values: relative(value)
        allowed = set(self.allowed_files)
        if not set(self.preauthorized_files or []) <= allowed or not set(self.success.required_changed) <= allowed:
            raise ValueError("Preauthorization/required changes exceed allowed files")
        if self.analysis:
            if allowed or self.success.kind != "analysis_relations" or self.success.oracle is not None or not self.success.expected:
                raise ValueError("Analysis tasks are immutable with an explicit relation oracle")
        elif not allowed or self.success.kind != "executable_oracle" or not self.success.oracle:
            raise ValueError("Modification tasks need file scope and executable oracle")
        if self.success.oracle:
            relative(self.success.oracle)
            if not self.success.oracle.startswith("oracles/"): raise ValueError("Oracle must be outside fixture")
        if not self.validation_commands: raise ValueError("Validation commands required")
        if self.initial_state.expected not in {"build_failed", "test_failed", "read_only"}:
            raise ValueError("Invalid initial state")
        return self


def load_tasks(root=DATA_ROOT):
    path = asset(root, "tasks.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"version", "name", "tasks"} or data["version"] != 1:
        raise ValueError("Unsupported benchmark manifest")
    tasks = [TaskSpec.model_validate(t) for t in data["tasks"]]
    if len({t.id for t in tasks}) != 15 or Counter(t.category for t in tasks) != DISTRIBUTION:
        raise ValueError("Benchmark must contain exactly the specified 15 unique tasks")
    for task in tasks:
        source = asset(root, task.fixture)
        files = inventory(source)
        if any(p.startswith((".git/", ".forgepilot/")) or p == "CMakeUserPresets.json" for p in files):
            raise ValueError("Fixture contains local/generated metadata")
        if task.success.oracle: asset(root, task.success.oracle)
    return tasks


class RealTaskDefinition(Strict):
    """The exact text and checks supplied to either A/B arm."""

    request: str = Field(min_length=1, max_length=12_000)
    acceptance_criteria: list[str] = Field(min_length=1)
    allowed_files: list[str] = Field(min_length=1)
    required_changed: list[str] = Field(min_length=1)
    validation_commands: list[list[str]] = Field(min_length=3)
    integrity_policy: "RealIntegrityPolicy"

    @model_validator(mode="after")
    def validate_definition(self):
        if len({path.casefold() for path in self.allowed_files}) != len(self.allowed_files):
            raise ValueError("Duplicate real-task file scope")
        for path in self.allowed_files:
            relative(path)
        if len({path.casefold() for path in self.required_changed}) != len(self.required_changed):
            raise ValueError("Duplicate required real-task change")
        for path in self.required_changed:
            relative(path)
        if not {path.casefold() for path in self.required_changed} <= {path.casefold() for path in self.allowed_files}:
            raise ValueError("Required real-task changes exceed allowed files")
        if not {path.casefold() for path in self.integrity_policy.editable_existing_test_files} <= {
                path.casefold() for path in self.allowed_files}:
            raise ValueError("Editable test files exceed allowed scope")
        for command in self.validation_commands:
            if not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("Invalid real-task validation command")
            if any("protected_oracle" in arg or "official_patch" in arg for arg in command):
                raise ValueError("Protected material cannot be an agent validation command")
        return self


class RealIntegrityPolicy(Strict):
    editable_existing_test_files: list[str]
    assertion_updates: list["RealAssertionUpdate"] = Field(default_factory=list)
    control_flow_additions: list["RealControlFlowAddition"] = Field(default_factory=list)
    preserve_ctest_names: Literal[True]
    minimum_ctest_cases: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_policy(self):
        if len({path.casefold() for path in self.editable_existing_test_files}) != len(self.editable_existing_test_files):
            raise ValueError("Duplicate editable test path")
        for path in self.editable_existing_test_files:
            relative(path)
        if len({item.path.casefold() for item in self.assertion_updates}) != len(self.assertion_updates):
            raise ValueError("Duplicate assertion update path")
        if not {item.path.casefold() for item in self.assertion_updates} <= {path.casefold() for path in self.editable_existing_test_files}:
            raise ValueError("Assertion updates need exact editable test files")
        if len({(item.path.casefold(), item.kind, item.signature_sha256)
                for item in self.control_flow_additions}) != len(self.control_flow_additions):
            raise ValueError("Duplicate control-flow addition")
        if not {item.path.casefold() for item in self.control_flow_additions} <= {
                path.casefold() for path in self.editable_existing_test_files}:
            raise ValueError("Control-flow additions need exact editable test files")
        return self


class RealAssertionUpdate(Strict):
    path: str
    baseline_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_assertion_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_assertion_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_update(self):
        relative(self.path)
        return self


class RealControlFlowAddition(Strict):
    path: str
    baseline_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["cpp", "cmake"]
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_addition(self):
        relative(self.path)
        if (self.kind == "cmake") != _is_cmake(self.path):
            raise ValueError("Control-flow kind disagrees with test file type")
        return self


class RealTaskSpec(Strict):
    id: str = Field(pattern=r"^real-(?:bug|feature)-\d{2}$")
    kind: Literal["bug", "feature"]
    title: str = Field(min_length=1)
    repository_url: str
    license: str = Field(pattern=r"^[A-Za-z0-9.-]+$")
    license_file: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    issue_url: str
    init_script: str
    task_spec: RealTaskDefinition
    protected_oracle: str

    @model_validator(mode="after")
    def validate_real_task(self):
        if not self.id.startswith(f"real-{self.kind}-"):
            raise ValueError("Real task ID and kind disagree")
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", self.repository_url)
        if match is None:
            raise ValueError("Real task needs a canonical public GitHub repository URL")
        expected_issue_prefix = self.repository_url + "/issues/"
        if not self.issue_url.startswith(expected_issue_prefix) or not re.fullmatch(r"[1-9][0-9]*", self.issue_url[len(expected_issue_prefix):]):
            raise ValueError("Issue URL must belong to the pinned repository")
        relative(self.license_file)
        relative(self.init_script)
        relative(self.protected_oracle)
        if not self.init_script.startswith("real_tasks/init/") or not self.init_script.endswith(".py"):
            raise ValueError("Initialization script must be in the protected dataset")
        if not self.protected_oracle.startswith("real_tasks/protected_oracles/") or not self.protected_oracle.endswith(".py"):
            raise ValueError("Oracle must be outside the Agent candidate checkout")
        if len(self.task_spec.allowed_files) < 2 and self.kind == "feature":
            raise ValueError("Real feature tasks must span source and tests")
        return self

    def to_artifact_task_spec(self):
        """Map the frozen definition to the strict Mission TaskSpec contract.

        The benchmark's assertion replacement rule is separate from the
        production TaskSpec schema and stays in trusted evaluator metadata.
        """
        criteria = [
            {"id": f"AC-{index}", "description": item,
             "verification": "protected_test" if index == 1 else "ctest"}
            for index, item in enumerate(self.task_spec.acceptance_criteria, start=1)
        ]
        criteria.append({"id": f"AC-{len(criteria) + 1}",
                         "description": "The pinned project configures and builds successfully with the locked CMake command.",
                         "verification": "build"})
        reproduction = REAL_BUG_REPRODUCTION[self.id] if self.kind == "bug" else ()
        return validate_task_spec(ArtifactTaskSpec.model_validate({
            "task_type": "bug_fix" if self.kind == "bug" else "feature",
            "title": self.title,
            "objective": self.task_spec.request,
            "repository": {"base_ref": self.base_commit, "toolchain_profile": "cmake-ctest-v1"},
            "problem": {"observed_behavior": self.task_spec.request,
                        "expected_behavior": self.task_spec.acceptance_criteria[0],
                        "reproduction_steps": reproduction},
            "acceptance_criteria": criteria,
            "allowed_scope": {"path_patterns": self.task_spec.allowed_files,
                              "max_changed_files": len(self.task_spec.allowed_files)},
            "forbidden_scope": [".git/**", "eval/**"],
            "test_policy": {"existing_tests_read_only": not bool(self.task_spec.integrity_policy.editable_existing_test_files),
                            "allow_new_tests": True,
                            "modifiable_existing_test_patterns": self.task_spec.integrity_policy.editable_existing_test_files},
            "data_policy": {"cloud_model_allowed": True, "external_mcp_allowed": False},
            "assumptions": [],
            "risks": ["Preserve existing test coverage; honor only the task's locked test corrections and file scope."],
            "risk_level": "medium",
        }))

    def to_integrity_task_spec(self):
        """Trusted exact-file permissions for a future real-run adapter."""
        policy = self.task_spec.integrity_policy
        return IntegrityTaskSpec(
            "bug_fix" if self.kind == "bug" else "feature",
            tuple(policy.editable_existing_test_files),
            tuple(AssertionUpdate(**update.model_dump()) for update in policy.assertion_updates),
            tuple(ControlFlowAddition(**addition.model_dump()) for addition in policy.control_flow_additions),
        )


def _real_files(root):
    """Hash every locked asset without traversing links or accepting untracked additions."""
    real_root = root / "real_tasks"
    files = inventory(real_root)
    files.pop("lock.json", None)
    return files


def load_real_tasks(root=DATA_ROOT):
    root = Path(root).resolve()
    path = asset(root, "real_tasks/tasks.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"version", "name", "tasks"} or data["version"] != 1:
        raise ValueError("Unsupported real-task manifest")
    tasks = [RealTaskSpec.model_validate(item) for item in data["tasks"]]
    if len(tasks) != 5 or len({task.id for task in tasks}) != 5 or Counter(task.kind for task in tasks) != {"bug": 3, "feature": 2}:
        raise ValueError("Real benchmark must contain exactly three bugs and two features")
    for task in tasks:
        asset(root, task.init_script)
        asset(root, task.protected_oracle)
        task.to_artifact_task_spec()
        task.to_integrity_task_spec()
    return tasks


def _real_lock_payload(root):
    tasks = load_real_tasks(root)
    files = _real_files(Path(root))
    payload = {
        "version": 1,
        "tasks": [{"id": task.id, "kind": task.kind, "repository_url": task.repository_url,
                   "base_commit": task.base_commit, "issue_url": task.issue_url} for task in tasks],
        "files": files,
    }
    payload["sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return payload


def lock_real_tasks(root=DATA_ROOT):
    """Explicit one-time lock; normal evaluation only calls verify_real_lock."""
    root = Path(root).resolve()
    path = root / "real_tasks" / "lock.json"
    if path.exists():
        raise FileExistsError("Real benchmark is already locked")
    payload = _real_lock_payload(root)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_real_lock(root=DATA_ROOT):
    root = Path(root).resolve()
    locked = json.loads(asset(root, "real_tasks/lock.json").read_text(encoding="utf-8"))
    expected = _real_lock_payload(root)
    if locked != expected:
        raise ValueError("Locked real-task manifest or protected asset changed")
    return load_real_tasks(root)


def check_real_test_integrity(task, baseline_root, candidate_root, baseline_ctest_names, final_ctest_names):
    """Fail closed if an Agent weakens a public test or removes a CTest entry."""
    policy = task.task_spec.integrity_policy
    if (len(baseline_ctest_names) < policy.minimum_ctest_cases
            or len(final_ctest_names) < policy.minimum_ctest_cases
            or len(set(baseline_ctest_names)) != len(baseline_ctest_names)
            or len(set(final_ctest_names)) != len(final_ctest_names)):
        return False
    if policy.preserve_ctest_names and not set(baseline_ctest_names) <= set(final_ctest_names):
        return False
    for path in policy.editable_existing_test_files:
        try:
            before = asset(Path(baseline_root), path).read_bytes()
            after = asset(Path(candidate_root), path).read_bytes()
        except (OSError, ValueError):
            return False
        old, new = _text(before), _text(after)
        if old is None or new is None:
            return False
        if not _assertions_preserved(old, new, path=path, baseline_bytes=before,
                                     updates=task.to_integrity_task_spec().assertion_updates):
            return False
        cmake = _is_cmake(path)
        if not cmake and not _control_flow_preserved(
                old, new, path=path, baseline_bytes=before,
                additions=task.to_integrity_task_spec().control_flow_additions, kind="cpp"):
            return False
        if Counter(match.group().casefold() for match in _SUPPRESSION.finditer(_comments(new, cmake=cmake))) - Counter(
                match.group().casefold() for match in _SUPPRESSION.finditer(_comments(old, cmake=cmake))):
            return False
        if cmake:
            old_commands = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(old)
                                   if name in _CMAKE_PROTECTED)
            new_commands = Counter((name, _canonical(body)) for name, body, _ in _cmake_commands(new)
                                   if name in _CMAKE_PROTECTED)
            if old_commands - new_commands:
                return False
            if not _control_flow_preserved(
                    old, new, path=path, baseline_bytes=before,
                    additions=task.to_integrity_task_spec().control_flow_additions, kind="cmake"):
                return False
    return True
