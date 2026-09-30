"""Strict, versioned task metadata and link-free fixture assets."""
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forgepilot.repository import is_link

DISTRIBUTION = {"C++ 编译错误": 3, "C++ 逻辑 Bug": 3, "Qt Signal/Slot": 2,
                "CMake": 2, "Unit Test": 2, "调用链分析": 1, "跨文件修改": 1, "Repair": 1}
DATA_ROOT = Path(__file__).resolve().parents[1]


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
