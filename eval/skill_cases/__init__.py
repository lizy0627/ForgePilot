"""Frozen Qt samples for the offline skill experiment. Only copy_fixture is candidate-facing."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from forgepilot.artifacts.canonical import content_sha256
from forgepilot_eval.manifest import DATA_ROOT, TaskSpec, asset, inventory, load_tasks

ROOT = Path(__file__).resolve().parent
DEVELOPMENT_IDS = ("qt-signal-01", "qt-signal-02")


def _text(path):
    # All corpus assets are UTF-8 text. Materialize the same bytes on CRLF checkouts.
    return path.read_text(encoding="utf-8")


def _files(directory):
    return {name: _text(asset(directory, name)) for name in inventory(directory)}


def _sha(text):
    return sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillCase:
    task: TaskSpec
    dataset: Path
    split: str
    relevant: bool
    solution: dict
    _sources: dict[str, str]

    @property
    def oracle(self):
        return asset(self.dataset, self.task.success.oracle)

    @property
    def locks(self):
        return {
            "task": content_sha256(self.task),
            "fixture": content_sha256({name: _sha(text) for name, text in self._sources.items()}),
            "oracle": _sha(_text(self.oracle)),
            "solution": content_sha256(self.solution),
            "label": content_sha256([self.split, self.relevant]),
        }

    def copy_fixture(self, target):
        target.mkdir(parents=True, exist_ok=False)
        for name, text in self._sources.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))


def _cases():
    development = {task.id: task for task in load_tasks() if task.id in DEVELOPMENT_IDS}
    answers = json.loads(_text(asset(DATA_ROOT, "offline-solutions.json")))["solutions"]
    cases = []
    for identity in DEVELOPMENT_IDS:
        task = development[identity]
        cases.append(SkillCase(task, DATA_ROOT, "development", False, answers[identity],
                               _files(asset(DATA_ROOT, task.fixture))))

    # Inherit the same public CMake layout and six validation steps as the Qt benchmark.
    template = development[DEVELOPMENT_IDS[0]]
    cmake_files = {name: cases[0]._sources[name] for name in ("CMakeLists.txt", "cmake/Subject.cmake")}
    shared = _files(asset(ROOT, "shared"))
    solution = {"files": {"src/subject.cpp": _text(asset(ROOT, "answers/list_model.cpp"))}}
    for row in json.loads(_text(asset(ROOT, "cases.json"))):
        title, request = row["title"], row["request"]
        fixture = "fixtures/" + row["id"]
        data = template.model_dump()
        data.update(id=row["id"], fixture=fixture, title=title, request=request)
        data["initial_state"]["description"] = title
        data["success"].update(oracle="oracles/list_model.cpp", expected_behavior=request)
        task = TaskSpec.model_validate(data)
        sources = {**cmake_files, **shared, **_files(asset(ROOT, fixture))}
        sources["PROJECT.txt"] = f"{title}\n\n{request}\n\n允许修改：src/subject.cpp。\n"
        cases.append(SkillCase(task, ROOT, row["split"], row["relevant"], solution, sources))
    return cases


def load_cases():
    cases = _cases()
    lock = json.loads(_text(asset(ROOT, "lock.json")))
    actual = [{"id": case.task.id, **case.locks} for case in cases]
    if lock != {"version": 1, "cases": actual}:
        raise ValueError("Skill experiment corpus differs from the registered v1 lock")
    return cases
