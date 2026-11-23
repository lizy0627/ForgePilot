"""Offline skill-selection experiment, using the production analyst graph and a scripted writer."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version as package_version
import json
from pathlib import Path
import platform
import shutil
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from forgepilot.agents.analysis import AnalysisRequest, AnalysisService, validate_skill_bindings, validate_skill_routes
from forgepilot.agents.contracts import validate_task_spec
from forgepilot.agents.registry import AgentRegistry
from forgepilot.artifacts.canonical import content_sha256
from forgepilot.changes import ChangeSet
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.orchestration.graph import validate_graph
from forgepilot.orchestration.models import NodeStatus
from forgepilot.orchestration.workspace import StrictFakeWorkspaceBackend, WorkspaceSnapshot
from forgepilot.repository import Repository
from forgepilot.skill_selection import skill_reference_payload
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.trace import MemoryTrace

from .grader import check_scope
from .manifest import DATA_ROOT, inventory
from .offline import call, message
from .reviewed_qt_skill import publish_qt_skill


ANALYST_STAGES = ("root_cause_analysis", "impact_test_analysis")
WRITE_TOOL_LIMIT = 8
ARM_TIMEOUT = 600


def locked_spec(case):
    task = case.task
    return validate_task_spec({
        "task_type": "bug_fix", "title": task.title, "objective": task.request,
        "repository": {"base_ref": case.locks["fixture"], "toolchain_profile": "cmake-ctest-v1"},
        "problem": {"observed_behavior": task.initial_state.description, "expected_behavior": task.request,
                    "reproduction_steps": ["Run the locked fixture's public CTest."]},
        "acceptance_criteria": [
            {"id": "AC-1", "description": "The candidate project builds", "verification": "build"},
            {"id": "AC-2", "description": "Public regression tests pass", "verification": "ctest"},
            {"id": "AC-3", "description": "The protected behavior check passes", "verification": "protected_test"}],
        "allowed_scope": {"path_patterns": task.allowed_files, "max_changed_files": len(task.allowed_files)},
        "forbidden_scope": ["tests/**", ".git/**"], "test_policy": {"allow_new_tests": False},
        "data_policy": {}, "assumptions": [], "risks": ["Keep existing test coverage."], "risk_level": "low",
    })


def offline_config():
    return ModelConfiguration(profiles=(ModelProfile(profile_id="script", model="fixed-qt-analysts-v1",
        family="offline", base_url="http://127.0.0.1:1/v1", api_key_ref="env:UNUSED_OFFLINE",
        max_output_tokens=2048),), routes=tuple(ModelRoute(route_id=route, primary="script")
        for route in ("analysis", "orchestration")), enforce_review_family_separation=False)


class ScriptedAnalysis:
    """The analyst receives only messages and tool output. It never opens the grading assets."""
    def __init__(self):
        self.references = {}
        self.analyst_inputs = {}

    async def chat(self, messages, tools=None):
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        spec = payload["task_spec"]
        files = spec["content"]["allowed_scope"]["path_patterns"]
        if "evidence" in payload:
            if "skill_references" in payload:
                raise ValueError("guidance_reached_planner")
            criteria = spec["content"]["acceptance_criteria"]
            result = {"task_spec_artifact_id": spec["artifact_id"], "summary": "Offline fixture plan",
                "steps": [{"step_id": "STEP-1", "owner": "implementer", "depends_on": [],
                    "expected_files": files, "action": "Apply the fixed offline change.",
                    "verification": [item["verification"] for item in criteria],
                    "risk": "Retain existing test coverage", "adds_tests": False,
                    "acceptance_criteria_ids": [item["id"] for item in criteria]}]}
        else:
            stage = ANALYST_STAGES[0] if "root-cause-analyst.v1" in messages[0]["content"] else ANALYST_STAGES[1]
            # Compare the actual wire TaskSpec content, system prompt and tools. Artifact
            # envelopes have per-run IDs and creation times unrelated to the assigned skill.
            reference_keys = ("skill_references", "skill_reference_usage")
            references = {k: payload[k] for k in reference_keys if k in payload}
            inputs = {k: v for k, v in payload.items() if k not in reference_keys}
            inputs["task_spec"] = spec["content"]
            digest = content_sha256({"input": inputs, "prompt": messages[0]["content"],
                                     "tools": [t.model_dump(mode="json") for t in tools or []]})
            if (self.references.setdefault(stage, references) != references
                    or self.analyst_inputs.setdefault(stage, digest) != digest):
                raise ValueError("analyst_input_changed_within_run")
            outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not outputs:
                return message(calls=[call("repository_read_range", {"path": files[0]}, "observe-source")])
            source = outputs[-1]
            result = {"summary": "Offline source observation", "items": [{"evidence_id": "EV-1",
                "claim": "The candidate source was read from the current fixture.",
                "path": source["path"], "start_line": 1, "end_line": 1, "file_hash": source["sha256"],
                "symbol": source["path"], "source": "local_repository", "confidence": 1.0,
                "reason": "The read returned the current file and its content hash."}]}
        return message(json.dumps(result, ensure_ascii=False))

    async def aclose(self):
        pass


async def analyze(case, store, repository, row):
    selected = store.skills.resolve_selection(row["selected_skills"])
    spec = locked_spec(case)
    request = AnalysisRequest(request=spec.objective, repository=spec.repository, data_policy=spec.data_policy)
    provider = ScriptedAnalysis()
    async with ModelGateway(offline_config(), secret_resolver=lambda _: "offline-only",
                            provider_factory=lambda *_: provider) as gateway:
        service = AnalysisService(store, repository, gateway)
        validate_skill_routes(service, selected, spec.data_policy)
        graph = service.graph(request, locked_task_spec=spec, skills=selected)
        validate_skill_bindings(graph, selected)
        task_id = str(uuid4())
        service.runner.store.create_run(task_id, validate_graph(graph), policy_hash=service.runner.policy.definition_hash)
        record = {
            "workspace_id": task_id, "task_id": task_id, "repository_path": str(repository.root),
            "worktree_path": str(repository.root), "base_ref": spec.repository.base_ref,
            "head_commit": spec.repository.base_ref, "status": "active",
        }
        store.workspace_backend.register(WorkspaceSnapshot(task_id, task_id, spec.repository.base_ref,
                                                          inventory(repository.root)), record)
        store.missions.admit_mission(task_id, record, {"request": spec.objective}, selected_skills=selected)
        graph_run = await service._run_worker(task_id, service.runner.run, task_id, graph)
        usage = store.skills.mission_usage(task_id)
        row["stage_receipts"] = usage["injections"]
        row["analyst_input_sha256"] = provider.analyst_inputs
        row["selection_payload_sha256"] = content_sha256(provider.references)
        row["analysis_status"] = graph_run.status.value
        row["analysis_errors"] = {n.node_id: n.result.error_summary for n in graph_run.nodes
                                  if n.result and n.result.error_summary}
        if graph_run.status != NodeStatus.AWAITING_HUMAN:
            raise ValueError("analysis_failed")
        row["investigation_tool_calls"] = sum(n.result.metrics.tool_calls for n in graph_run.nodes
                                              if n.result and n.node_id in ANALYST_STAGES)
        row["tool_calls"] = sum(n.result.metrics.tool_calls for n in graph_run.nodes if n.result)
        expected_receipts = []
        for stage in ANALYST_STAGES:
            receiving = [s for s in selected if stage in s.content.stages]
            expected = skill_reference_payload(receiving, stage) if receiving else {}
            if provider.references.get(stage) != expected:
                raise ValueError("provider_selection_mismatch")
            expected_receipts.extend(dict(s.identity(), node_id=stage, attempt=1) for s in receiving)
        if usage != {"selected_skills": row["selected_skills"], "injections": expected_receipts}:
            raise ValueError("selection_receipt_mismatch")
        service.approve(service.review(task_id), approved_by="offline-evaluator")
        return task_id


def run_conditions(case, common):
    return {**common, "task_id": case.task.id, "task_spec_sha256": content_sha256(locked_spec(case)),
            "task_sha256": case.locks["task"], "fixture_sha256": case.locks["fixture"],
            "solution_sha256": case.locks["solution"], "oracle_sha256": case.locks["oracle"]}


async def execute_arm(case, row, seed, validator, directory):
    started = perf_counter()
    try:
        async with asyncio.timeout(ARM_TIMEOUT):
            repo = directory / "candidate"
            case.copy_fixture(repo)
            before = inventory(repo)
            if content_sha256(before) != row["conditions"]["fixture_sha256"]:
                raise ValueError("candidate_baseline_mismatch")
            shutil.copyfile(seed, directory / "arm.sqlite3")
            repository = Repository(repo, rg_executable="rg")
            with closing(SqliteEventStore(directory / "arm.sqlite3", artifact_root=directory / "artifacts",
                                          workspace_backend=StrictFakeWorkspaceBackend())) as store:
                task_id = await analyze(case, store, repository, row)
            if before != inventory(repo):
                raise ValueError("read_only_analysis_changed_candidate")

            def require_scope(path):
                if path not in case.task.preauthorized_files or path not in case.task.allowed_files:
                    raise ValueError("script_outside_approved_scope")

            change = ChangeSet(repository, require_scope, MemoryTrace(), task_id)
            change.capture_scope()
            files = case.solution["files"]
            if len(files) * 2 > WRITE_TOOL_LIMIT:
                raise ValueError("script_tool_budget")
            for path, body in files.items():
                require_scope(path)
                row["tool_calls"] += 1
                await repository.read_file(path)
                row["tool_calls"] += 1
                await change.write_file(path, body)
            row["repair_rounds"] = 0
            after = inventory(repo)
            row["scope"] = check_scope(before, after, case.task)
            if not row["scope"]["valid"] or not row["scope"]["changed"]:
                raise ValueError("candidate_scope_mismatch")
            if case.locks["oracle"] != row["conditions"]["oracle_sha256"]:
                raise ValueError("oracle_changed_before_grading")
            validation = await validator.run(case, repo, directory / "validation")
            if case.locks["oracle"] != row["conditions"]["oracle_sha256"]:
                raise ValueError("oracle_changed_during_grading")
            row["verification"] = validation
            intact = inventory(repo, ignore_generated=True) == after
            row["candidate_unchanged_by_grading"] = intact
            passed = validation["passed"] and intact
            row.update(first_pass_success=passed, success=passed,
                       reason="verified_scripted_candidate" if passed else "validation_failed")
    except Exception as exc:
        # The evaluator only persists bounded diagnostics from its own local code and validators.
        row.update(success=False, reason=f"{type(exc).__name__}: {str(exc)[:400]}")
    finally:
        row["latency_ms"] = round((perf_counter() - started) * 1000, 3)


def persist(document, path):
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    staging.replace(path)


async def run_campaign(cases, validator, output, repetitions=3, jobs=1):
    if repetitions < 1 or jobs < 1:
        raise ValueError("repetitions and jobs must be positive")
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="forgepilot-skill-pairs-") as scratch:
        scratch = Path(scratch)
        seed = scratch / "seed.sqlite3"
        with closing(SqliteEventStore(seed, artifact_root=scratch / "seed-artifacts")) as store:
            version = publish_qt_skill(store.skills)
        skill = version.model_dump(include={"skill_id", "version", "content_sha256"})
        relevant_agents = ("root-cause-analyst.v1", "impact-test-analyst.v1", "orchestrator.v1")
        registry = AgentRegistry()
        common = {
            "model": offline_config().model_dump(mode="json"), "topology": "locked-analysis-multi+scripted-writer",
            "checkpoint_backend": "StrictFakeWorkspaceBackend (read-only analysis)",
            "budget": {"paid_tokens": 0, "arm_timeout_seconds": ARM_TIMEOUT, "writer_tools": WRITE_TOOL_LIMIT,
                       "repair_rounds": 0, "analysis": {k: registry.get(k).limits.model_dump() for k in relevant_agents}},
            "validator": validator.conditions, "skill_content_sha256": skill["content_sha256"],
            "runtime_sha256": content_sha256(inventory(DATA_ROOT.parent / "agent" / "forgepilot")),
            "evaluator_sha256": content_sha256({p: sha256((DATA_ROOT / p).read_bytes()).hexdigest() for p in (
                "forgepilot_eval/skill_pairs.py", "forgepilot_eval/skill_pair_native.py", "forgepilot_eval/grader.py",
                "forgepilot_eval/manifest.py", "forgepilot_eval/offline.py", "forgepilot_eval/reviewed_qt_skill.py",
                "skill_cases/__init__.py")}),
            "python": platform.python_version(), "jobs": jobs,
            "packages": {p: package_version(p) for p in ("pydantic", "jsonschema", "httpx")},
        }
        document = {"started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
                    "evidence_label": "offline", "paid_model_calls": 0, "skill": skill, "repetitions": repetitions,
                    "tasks": [{"id": c.task.id, "split": c.split} for c in cases], "rows": []}
        work = []
        for case in cases:
            for index in range(1, repetitions + 1):
                for arm in (("off", "on") if index % 2 else ("on", "off")):
                    conditions = run_conditions(case, common)
                    row = {"id": case.task.id, "run_index": index, "arm": arm, "conditions": conditions,
                           "conditions_sha256": content_sha256(conditions),
                           "selected_skills": [skill] if arm == "on" else [], "reason": "not_run",
                           "first_pass_success": None, "success": None, "repair_rounds": None, "tool_calls": None,
                           "investigation_tool_calls": None, "input_tokens": None, "latency_ms": None}
                    document["rows"].append(row)
                    work.append((case, row))
        results = output / "results.json"
        persist(document, results)
        slots = asyncio.Semaphore(jobs)

        async def execute(case, row):
            async with slots:
                row["reason"] = "running"
                with TemporaryDirectory(dir=scratch) as directory:
                    await execute_arm(case, row, seed, validator, Path(directory))
                persist(document, results)
                print(f"{row['id']} #{row['run_index']} {row['arm']}: {row['reason']}", flush=True)

        try:
            await asyncio.gather(*(execute(case, row) for case, row in work))
            document["finished_at"] = datetime.now(timezone.utc).isoformat()
        finally:
            for row in document["rows"]:
                if row["reason"] == "running":
                    row["reason"] = "interrupted"
            persist(document, results)
        return document
