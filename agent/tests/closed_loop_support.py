"""Offline model scripts around a real Git/CMake/CTest/hidden-oracle fixture."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from conftest import call, reply
from forgepilot.artifacts import create_artifact
from forgepilot.artifacts.models import ExecutionPlan, TaskSpec
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.orchestration.models import NodeStatus
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.toolchain import ToolchainProfile
from forgepilot.workspace_manager import WorkspaceManager
from test_workspace_manager import git


ORIGINAL = "int value(int input) { return input; }\n"
FIXED = "int value(int input) { return input < 0 ? 0 : input; }\n"
COMPILE_BROKEN = "int value(int input) { return missing_identifier; }\n"
TEST_BROKEN = "int value(int input) { return input + 2; }\n"
ORACLE_BROKEN = "int value(int input) { return input == -1 ? 0 : input; }\n"
FEATURE = '#include "range.hpp"\nint value(int input) { return twice(input); }\n'
FEATURE_HEADER = "inline int twice(int value) { return value * 2; }\n"
FEATURE_TEST = "#include <cassert>\nint value(int);\nint main() { assert(value(3) == 6); }\n"
CMAKE = """cmake_minimum_required(VERSION 3.24)
project(DeliveryFixture LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
enable_testing()
add_library(value src/value.cpp)
add_executable(public_test tests/public.cpp)
target_link_libraries(public_test PRIVATE value)
add_test(NAME public_behavior COMMAND public_test)
"""
FEATURE_CMAKE = CMAKE + """add_executable(feature_test tests/feature.cpp)
target_link_libraries(feature_test PRIVATE value)
add_test(NAME feature_behavior COMMAND feature_test)
"""
USAGE = {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}


def response(content=None, calls=None):
    return reply(content, calls=calls, usage=USAGE)


def done():
    return response(json.dumps({"status": "completed", "summary": "Model claims completion; require validation"}))


def patch(before=ORIGINAL, after=FIXED, path="src/value.cpp", step="STEP-1"):
    return response(calls=[call("repository_apply_patch", json.dumps({
        "path": path, "expected_hash": sha256(before.encode()).hexdigest(),
        "edits": [{"old_text": before, "new_text": after}], "plan_step_id": step,
        "acceptance_criteria_ids": ["AC-1"],
    }))])


def create(path, content, step="STEP-1"):
    return response(calls=[call("repository_create_file", json.dumps({
        "path": path, "content": content, "plan_step_id": step,
        "acceptance_criteria_ids": ["AC-1"],
    }))])


def review_response(*, approve=True):
    return response(json.dumps({
        "verdict": "approve" if approve else "request_changes",
        "acceptance_criteria": [{"criterion_id": f"AC-{index}",
            "status": "met" if approve else "not_met",
            "evidence": "Final diff and deterministic validation inspected"} for index in range(1, 4)],
        "blocking_issues": [] if approve else [{"issue_id": "RI-1", "description": "Add explanatory boundary comment", "path": "src/value.cpp"}],
        "non_blocking_suggestions": [], "test_review": "Public and hidden tests pass without weakening",
        "security_review": "No new data exposure or unsafe writes", "recommend_delivery": approve,
        "summary": "Independent review approves delivery" if approve else "Boundary clarification required",
    }))


class FakeProvider:
    def __init__(self, responses=(), *, hook=None, block=False):
        self.responses = list(responses)
        self.seen = []
        self.hook, self.block = hook, block
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def chat(self, messages, tools=None):
        self.seen.append((deepcopy(messages), tuple(item.name for item in tools or ())))
        self.started.set()
        if self.hook:
            self.hook(len(self.seen))
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if not self.responses:
            raise AssertionError("Unexpected additional model call")
        return self.responses.pop(0)

    async def aclose(self):
        pass


@dataclass
class LoopCase:
    task_id: str
    root: Path
    worktree: Path
    oracle: Path
    manager: WorkspaceManager
    events: SqliteEventStore
    gateway: ModelGateway
    validator: object
    service: object
    spec: object
    plan: object
    approval: object
    providers: dict

    def artifacts(self, kind):
        rows = self.events._db.execute(
            "SELECT artifact_id FROM artifacts WHERE task_id=? AND artifact_type=? ORDER BY rowid",
            (self.task_id, kind)).fetchall()
        return tuple(self.events.artifacts.get(row[0]) for row in rows)

    def model_calls(self):
        return tuple(dict(row) for row in self.events._db.execute(
            "SELECT node_id,role,model,prompt_version,input_hash,response_hash,duration_ms,"
            "input_tokens,output_tokens,total_tokens,error_code,retry_count,fallback_model "
            "FROM model_calls WHERE task_id=? ORDER BY created_at,call_id", (self.task_id,)))

    async def close(self):
        await self.gateway.aclose()
        self.events.close()

    def save_evidence(self, label, snapshot):
        destination = os.environ.get("FORGEPILOT_CLOSED_LOOP_EVIDENCE")
        if not destination:
            return
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        bundles = self.artifacts("DeliveryBundle")
        payload = {
            "mode": "offline fake models, real CMake/CTest/oracle",
            "task_type": self.spec.content.task_type, "task_id": self.task_id,
            "graph_status": snapshot.status.value,
            "nodes": [{"node_id": node.node_id, "status": node.status.value,
                "metrics": {"duration_ms": node.result.metrics.duration_ms,
                            "model_calls": node.result.metrics.model_calls,
                            "tool_calls": node.result.metrics.tool_calls,
                            "total_tokens": node.result.metrics.total_tokens} if node.result else None}
                for node in snapshot.nodes],
            "model_calls": self.model_calls(),
            "checkpoints": [{"node_id": item.node_id, "checkpoint_type": item.checkpoint_type,
                "commit": item.head_commit, "file_hashes": dict(item.file_hashes),
                "artifact_ids": [key for key, _ in item.output_artifacts]}
                for item in self.service.runner.checkpoints.list_for_task(self.task_id)],
            "validation": [item.model_dump(mode="json") for item in self.artifacts("ValidationReport")],
            "review": [item.model_dump(mode="json") for item in self.artifacts("ReviewReport")],
            "delivery": [item.model_dump(mode="json") for item in bundles],
            "changes": [item.model_dump(mode="json") for item in self.artifacts("ChangeSet")],
        }
        (target / f"{label}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if bundles and bundles[-1].content.patch_path:
            shutil.copyfile(bundles[-1].content.patch_path, target / f"{label}.patch")


async def make_loop_case(tmp_path, git_exe, *, task_type="bug_fix", implement=None,
                         repair=(), reviews=None, review_provider=None, slow_test=False,
                         crlf_source=False):
    # Imports remain lazy so helper collection does not depend on another worker's in-progress files.
    from forgepilot.agents.delivery import DeliveryWorkflow
    from forgepilot.validation import ProtectedOracle, ValidationService

    root = tmp_path / "C++ source 项目"
    root.mkdir()
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src/value.cpp").write_text(ORIGINAL, encoding="utf-8", newline="\r\n" if crlf_source else "")
    (root / "CMakeLists.txt").write_text(CMAKE, encoding="utf-8", newline="")
    expected = "value(-1) == 0" if task_type == "bug_fix" else "value(0) == 0"
    slow = '#include <thread>\n#include <chrono>\n#include <fstream>\n#include <process.h>\n' if slow_test else ""
    delay = 'std::ofstream("started.txt") << _getpid(); std::this_thread::sleep_for(std::chrono::seconds(30)); ' if slow_test else ""
    (root / "tests/public.cpp").write_text(
        f"{slow}int value(int);\nint main() {{ {delay}return ({expected}) ? 0 : 1; }}\n", encoding="utf-8", newline="")
    (root / "README.txt").write_text("User-owned documentation\n", encoding="utf-8")
    git(git_exe, root, "init", "-q", "-b", "main")
    git(git_exe, root, "config", "user.name", "Offline Fixture")
    git(git_exe, root, "config", "user.email", "fixture@example.invalid")
    git(git_exe, root, "config", "core.autocrlf", "false")
    git(git_exe, root, "add", "--all")
    git(git_exe, root, "commit", "-q", "-m", "baseline")

    oracle = tmp_path / "host protected oracle"
    oracle.mkdir()
    (oracle / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.24)
project(HiddenOracle LANGUAGES CXX)
enable_testing()
add_executable(hidden hidden.cpp "${FORGEPILOT_WORKTREE}/src/value.cpp")
add_test(NAME protected_behavior COMMAND hidden)
''', encoding="utf-8", newline="")
    oracle_expect = "value(-5) == 0 && value(4) == 4" if task_type == "bug_fix" else "value(7) == 14 && value(-3) == -6"
    (oracle / "hidden.cpp").write_text(
        f"// SECRET_ORACLE_ANSWER_NEVER_MODEL_CONTEXT\nint value(int);\nint main() {{ return ({oracle_expect}) ? 0 : 1; }}\n", encoding="utf-8", newline="")

    task_id = str(uuid4())
    manager = WorkspaceManager(tmp_path / "host data", git_executable=git_exe)
    record = manager.create(root, task_id)
    events = SqliteEventStore(tmp_path / "closed-loop.sqlite3", workspace_backend=manager,
                             artifact_root=tmp_path / "artifact store")
    cmake = shutil.which("cmake")
    ctest = shutil.which("ctest")
    assert cmake and ctest, "Real CMake and CTest are required"
    profile = ToolchainProfile(cmake, ctest)
    # MSBuild's compiler probe appends a long scratch path. Keep only generated
    # products at a short sibling path; source still exercises Unicode/spaces.
    validator = ValidationService(manager, profile, build_root=tmp_path.parent / ("v-" + task_id[:8]),
        oracle=ProtectedOracle(source_dir=oracle), generator="Visual Studio 17 2022", architecture="x64")
    spec = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="draft_task_spec",
        content=TaskSpec.model_validate({
            "task_type": task_type, "title": "Clamp negative values" if task_type == "bug_fix" else "Add doubling behavior",
            "objective": "Implement and independently verify the approved behavior",
            "repository": {"base_ref": "HEAD", "toolchain_profile": profile.profile_id},
            "problem": {"observed_behavior": "Values are returned unchanged",
                "expected_behavior": "Negative values clamp to zero" if task_type == "bug_fix" else "Values double through a reusable helper",
                "reproduction_steps": ["Build and run the behavior test"]},
            "acceptance_criteria": [
                {"id": "AC-1", "description": "Regression behavior passes protected edge cases", "verification": "protected_test"},
                {"id": "AC-2", "description": "CMake build succeeds", "verification": "build"},
                {"id": "AC-3", "description": "Every public CTest passes", "verification": "ctest"}],
            "allowed_scope": {"path_patterns": ["src/**", "tests/**", "CMakeLists.txt"], "max_changed_files": 5},
            "forbidden_scope": [".git/**", "build/**", ".forgepilot/**"],
            "test_policy": {"existing_tests_read_only": True, "allow_new_tests": True},
            "data_policy": {"cloud_model_allowed": False, "external_mcp_allowed": False},
            "assumptions": [], "risks": ["New behavior may affect callers"], "risk_level": "low",
        }))
    planned = ["src/value.cpp"] if task_type == "bug_fix" else ["src/value.cpp", "src/range.hpp", "tests/feature.cpp", "CMakeLists.txt"]
    plan = create_artifact(task_id=task_id, producer="orchestrator.v1", producer_node="generate_plan",
        parent_artifact_ids=(spec.artifact_id,), content=ExecutionPlan.model_validate({
            "task_spec_artifact_id": spec.artifact_id, "summary": "Implement the bounded behavior and verify it",
            "steps": [{"step_id": "STEP-1", "owner": "implementer.v1", "depends_on": [],
                "expected_files": planned, "action": "Update behavior and add required regression coverage",
                "verification": ["protected_test", "build", "ctest"], "risk": "Behavior may affect callers",
                "adds_tests": task_type == "feature", "acceptance_criteria_ids": ["AC-1", "AC-2", "AC-3"]}],
        }))
    events.artifacts.put(spec)
    events.artifacts.put(plan)
    if implement is None:
        implement = [patch(), done()]
    providers = {"implement": FakeProvider(implement), "repair": FakeProvider(repair),
                 "review": review_provider or FakeProvider(reviews if reviews is not None else [review_response()])}
    configuration = ModelConfiguration(profiles=tuple(ModelProfile(profile_id=key,
        base_url="http://127.0.0.1:1/v1", api_key_ref="env:FORGEPILOT_API_KEY", model=f"fake-{key}",
        family="independent" if key == "review" else "writer", max_output_tokens=2048)
        for key in providers), routes=(ModelRoute(route_id="implementation", primary="implement"),
            ModelRoute(route_id="repair", primary="repair"), ModelRoute(route_id="review", primary="review")))
    gateway = ModelGateway(configuration, secret_resolver=lambda _: "offline-only",
                           provider_factory=lambda profile, _: providers[profile.profile_id])
    service = DeliveryWorkflow(events, manager, gateway, validator=validator)
    paused = await service.prepare(task_id, spec.artifact_id, plan.artifact_id)
    assert paused.status is NodeStatus.AWAITING_HUMAN, paused
    return LoopCase(task_id, root, Path(record["worktree_path"]), oracle, manager, events, gateway,
        validator, service, spec, plan, service.review(task_id), providers)
