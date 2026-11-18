"""Trusted adapter from the five locked public issues to MissionHost.

The adapter never exposes Oracle source to an Agent. Paid execution remains
guarded by the evaluator's isolation and campaign preflight.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from time import perf_counter

from forgepilot.artifacts.canonical import content_sha256
from forgepilot.agents.registry import AgentRegistry
from forgepilot.errors import ForgePilotError
from forgepilot.mission_factory import MissionDependencies
from forgepilot.path_identity import same_path
from forgepilot.toolchain import ResolvedCommand, ToolchainProfile, validate_executable

from .manifest import DATA_ROOT, RealTaskSpec, asset, verify_real_lock
from .isolation import AppContainerDomain, IsolationConfig, IsolationError


class LockedRealToolchainProfile(ToolchainProfile):
    """Only the locked CMake/CTest argv, with an evaluator-owned build path."""

    def __init__(self, task: RealTaskSpec, *, cmake: str, ctest: str,
                 isolation_options=()):
        super().__init__(cmake=cmake, ctest=ctest)
        commands = tuple(tuple(command) for command in task.task_spec.validation_commands)
        if (len(commands) != 3 or tuple(command[0] for command in commands) !=
                ("cmake", "cmake", "ctest") or
                any(token.count("{build_dir}") and token != "{build_dir}"
                    for command in commands for token in command)):
            raise ValueError("Locked real task has unsupported validation commands")
        object.__setattr__(self, "_commands", commands)
        object.__setattr__(self, "_isolation_options", tuple(isolation_options))

    def resolve(self, phase: str, values: dict[str, str], **options) -> ResolvedCommand:
        index = {"cmake_configure": 0, "cmake_build": 1, "run_tests": 2}.get(phase)
        if index is None:
            return super().resolve(phase, values, **options)
        root = Path(values["source_dir"])
        if (not root.is_absolute() or not root.is_dir() or
                not same_path(root.resolve(strict=True), root) or root.is_symlink()):
            raise ForgePilotError("command_denied", "Locked source path is not an ordinary absolute workspace")
        command = self._commands[index]
        executable = validate_executable(self.cmake if index < 2 else self.ctest,
                                         expected=command[0])
        args = tuple(values["build_dir"] if token == "{build_dir}" else token
                     for token in command[1:])
        if index == 0:
            args += self._isolation_options
        if index == 2:
            args += ("--no-tests=error", "--output-junit", values["report_path"])
        return ResolvedCommand(str(executable), args, root)

    def resolve_oracle(self, phase: str, values: dict[str, str], **options) -> ResolvedCommand:
        raise ForgePilotError("command_denied", "Real-task Python Oracle requires the isolated adapter")


class IsolatedCandidateRunner:
    """One AppContainer identity for the public commands of one mission run."""

    def __init__(self, config: IsolationConfig, validation_root: Path,
                 *, domain_factory=AppContainerDomain):
        self.config = config
        self.validation_root = validation_root.resolve(strict=True)
        self.domain = domain_factory(config.launcher)
        config.grant_read_only(self.domain)
        self._source = None
        self._build = None
        self._closed = False

    async def __call__(self, argv, cwd, *, timeout, env):
        source = Path(cwd).resolve(strict=True)
        if self._source is not None and source != self._source:
            raise IsolationError("Candidate command changed source workspace")
        args = list(argv)
        if "-B" in args:
            build = Path(args[args.index("-B") + 1]).resolve(strict=True)
        elif "--build" in args:
            build = Path(args[args.index("--build") + 1]).resolve(strict=True)
        elif "--test-dir" in args:
            build = Path(args[args.index("--test-dir") + 1]).resolve(strict=True)
        else:
            raise IsolationError("Candidate command has no bounded build directory")
        if (build.name != "build" or build.parent.parent != self.validation_root
                or not build.is_dir()):
            raise IsolationError("Candidate build directory escaped the public validation tree")
        self.config.validate_for(source, build)
        if self._source is None:
            self._source = source
            await asyncio.to_thread(self.domain.grant, source, writable=True)
        if self._build is None:
            self._build = build
            await asyncio.to_thread(self.domain.grant, build, writable=True)
        elif build != self._build:
            raise IsolationError("Candidate command changed its public build directory")
        return await self.domain.run(args, source, timeout=timeout, env=self.config.env)

    def close(self):
        if not self._closed:
            self._closed = True
            self.domain.close()


@dataclass(frozen=True)
class LockedMissionArm:
    task: RealTaskSpec
    topology: str
    repository: Path
    manifest_sha256: str

    def __post_init__(self):
        if self.topology not in {"single", "multi"}:
            raise ValueError("Unknown A/B topology")
        if not self.repository.is_absolute() or not self.repository.is_dir():
            raise ValueError("Pinned repository is unavailable")

    @property
    def task_spec(self):
        return self.task.to_artifact_task_spec()

    @property
    def integrity_task_spec(self):
        return self.task.to_integrity_task_spec()

    def comparison_input(self):
        """Topology-free identity used to reject unequal A/B inputs."""
        spec = self.task_spec
        return {"task_id": self.task.id, "base_commit": self.task.base_commit,
                "task_spec_sha256": content_sha256(spec),
                "integrity_sha256": content_sha256(asdict(self.integrity_task_spec)),
                "validation_commands_sha256": content_sha256(self.task.task_spec.validation_commands),
                "manifest_sha256": self.manifest_sha256}

    def approval_authority(self, spec, source, ref, actor):
        """Verify the review-time grant against the same immutable manifest."""
        return (spec == self.task_spec and source == "locked_benchmark_manifest"
                and ref == self.manifest_sha256
                and actor == "benchmark-evaluator/" + self.manifest_sha256)

    async def create_mission(self, missions):
        spec = self.task_spec
        return await missions.create_locked(spec, allow_locked_test_scope=True,
            locked_authorization_ref=self.manifest_sha256,
            repository_path=str(self.repository), request=spec.objective,
            task_type=spec.task_type,
            orchestration_mode="single_agent" if self.topology == "single" else "multi_agent",
            base_ref=self.task.base_commit, toolchain_profile="cmake-ctest-v1",
            language="en", mcp_server_ids=[])

    async def approve_named_tests(self, missions, mission):
        """A separate, visible grant bound to current TaskSpec/Plan/Scope."""
        review = mission.get("review")
        if not isinstance(review, dict):
            raise ValueError("Mission has no review package for named-test approval")
        return await missions.approve_existing_tests(mission["task_id"],
            expected_revision=review["revision"],
            task_spec_hash=review["task_spec_hash"],
            plan_hash=review["plan_hash"], scope_hash=review["scope_hash"],
            exact_paths=self.task.task_spec.integrity_policy.editable_existing_test_files,
            authorization_source="locked_benchmark_manifest",
            authorization_ref=self.manifest_sha256,
            approved_by="benchmark-evaluator/" + self.manifest_sha256)


def assert_paired_arms(single: LockedMissionArm, multi: LockedMissionArm):
    if single.topology != "single" or multi.topology != "multi":
        raise ValueError("A/B pair requires single and multi arms")
    if single.comparison_input() != multi.comparison_input():
        raise ValueError("A/B arms have different locked tasks or validation inputs")
    return single.comparison_input()


def validate_fair_gateway_routes(configuration):
    """One primary model for all writing/analysis roles, distinct review family."""
    router = configuration.router()
    AgentRegistry(router=router, include_generalist=True)
    primary = router.resolve("task", "generalist").primary
    for use_case, role in (("orchestration", "orchestrator"),
                           ("analysis", "root_cause_analyst"),
                           ("analysis", "impact_test_analyst"),
                           ("implementation", "implementer"), ("repair", "repair")):
        if router.resolve(use_case, role).primary.profile_id != primary.profile_id:
            raise ValueError("Single and multi Agent routes have different primary models")
    review = router.resolve("review", "reviewer")
    implementation = router.resolve("implementation", "implementer")
    writer_families = {profile.family.casefold() for profile in
        (implementation.primary, implementation.fallback) if profile}
    if any(profile.family.casefold() in writer_families
           for profile in (review.primary, review.fallback) if profile):
        raise ValueError("Reviewer and writer model families overlap")
    return primary.profile_id


def real_workflow_factory(dependencies: MissionDependencies, arm: LockedMissionArm,
                          isolation: IsolationConfig):
    """Compose the same Mission services and isolated validator for either arm.

    The factory is trusted evaluator code and is never reachable from the HTTP
    CreateMission payload. It owns one candidate AppContainer until run cleanup.
    """
    from forgepilot.mission_factory import build_mission_workflow
    from .real_oracle_adapter import RealOracleAdapter

    if not isinstance(isolation, IsolationConfig):
        raise TypeError("Real mission requires a verified isolation configuration")
    locked = {task.id: task for task in verify_real_lock()}
    if arm.task.id not in locked or arm.task != locked[arm.task.id]:
        raise ValueError("Mission task differs from the locked real manifest")
    lock = json.loads(asset(DATA_ROOT, "real_tasks/lock.json").read_text(encoding="utf-8"))
    if arm.manifest_sha256 != lock["sha256"]:
        raise ValueError("Mission authorization differs from the locked manifest")
    profile = LockedRealToolchainProfile(arm.task, cmake=str(isolation.cmake),
        ctest=str(isolation.ctest), isolation_options=isolation.cmake_options()[2:])
    oracle_script = asset(DATA_ROOT, arm.task.protected_oracle)

    def factory(events, *, single_agent=False):
        if events is not dependencies.events or single_agent != (arm.topology == "single"):
            raise ValueError("Mission workflow does not match the paired arm")
        async def oracle_runner(candidate_root):
            return await RealOracleAdapter(isolation, oracle_script, candidate_root).run()
        workflow = build_mission_workflow(dependencies,
            oracle_source=oracle_script.parent,
            cloud_model_allowed=True, single_agent=single_agent,
            validation_profile=profile, oracle_runner=oracle_runner,
            integrity_task_spec=arm.integrity_task_spec)
        candidate = IsolatedCandidateRunner(isolation, workflow.validator.guard.root)
        workflow.validator.process_runner = candidate
        workflow.mission_candidate_runner = candidate
        return workflow

    return factory


def prepare_locked_arm(task_id, topology, destination, *, root=DATA_ROOT,
                       git="git", initializer=None):
    """Verify lock, fetch only the pinned base, and bind one arm to it."""
    root = Path(root).resolve(strict=True)
    tasks = {item.id: item for item in verify_real_lock(root)}
    if task_id not in tasks:
        raise ValueError("Unknown locked real task")
    task = tasks[task_id]
    destination = Path(destination)
    if initializer is None:
        if root != DATA_ROOT.resolve(strict=True):
            raise ValueError("Copied datasets require an explicit trusted initializer")
        script = asset(root, task.init_script)
        spec = importlib.util.spec_from_file_location("forgepilot_locked_initializer", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        initializer = module.prepare
    repository = Path(initializer(task_id, destination, git=git)).resolve(strict=True)
    if repository != destination.resolve(strict=True):
        raise ValueError("Locked initializer returned a different checkout")
    # The init script verifies HEAD and removes the remote. A test initializer
    # must meet the same identity check before its output enters a mission.
    from forgepilot.process import executable
    from subprocess import run
    git_path = executable(git, "git")
    head = run([git_path, "-C", str(repository), "rev-parse", "HEAD"],
               capture_output=True, check=False, timeout=30)
    if head.returncode or head.stdout.decode("ascii", errors="ignore").strip() != task.base_commit:
        raise ValueError("Prepared checkout differs from the locked base commit")
    lock = json.loads(asset(root, "real_tasks/lock.json").read_text(encoding="utf-8"))
    return LockedMissionArm(task, topology, repository, lock["sha256"])


async def run_offline_locked_arm(arm: LockedMissionArm, *, gateway, isolation: IsolationConfig,
                                 output: Path, git: str, rg: str, task_timeout=900,
                                 run_index=1, config_hash=None):
    """Exercise a locked Mission arm with an injected fake gateway only.

    The CLI paid runner does not call this path. Candidate commands still run in
    AppContainers so fake-provider integration cannot inspect Oracle sources.
    """
    from datetime import datetime, timezone
    from forgepilot.agents.delivery import validation_passed
    from forgepilot.artifacts.models import ValidationReport
    from forgepilot.mission_host import MissionHost
    from forgepilot.presets import BuildConfig
    from forgepilot.sqlite_store import SqliteEventStore
    from forgepilot.task_host import TaskHost
    from forgepilot.task_service import TaskService

    validate_fair_gateway_routes(gateway.router.configuration)
    provider_factory = getattr(gateway, "_provider_factory", None)
    if provider_factory is None or not getattr(provider_factory, "forgepilot_offline_fake", False):
        raise ValueError("Offline locked-arm runner requires a marked fake provider")
    if not isinstance(isolation, IsolationConfig):
        raise TypeError("Offline real-task execution still requires an isolated candidate runner")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    redactor = gateway.redactor
    state_root = Path(tempfile.mkdtemp(prefix="ForgePilot-real-mission-"))
    store = SqliteEventStore(state_root / "mission.sqlite3",
                             artifact_root=state_root / "artifacts", redactor=redactor)
    build = BuildConfig(cmake=str(isolation.cmake), ctest=str(isolation.ctest),
        generator="NMake Makefiles", make_program=str(isolation.make),
        configuration="Debug", configure_timeout=120, build_timeout=180,
        test_timeout=120)
    def legacy(trace):
        return TaskService(gateway.for_route("task"), trace=trace,
                           rg_executable=rg, git_executable=git,
                           build_config=build, redactor=redactor)
    host = TaskHost(legacy, store=store)
    dependencies = MissionDependencies(gateway=gateway, events=store,
        rg_executable=rg, git_executable=git, build_config=build, redactor=redactor,
        mcp_connection_factory=host.mcp_service.connection_factory)
    missions = MissionHost(host, workflow_factory=real_workflow_factory(dependencies, arm, isolation),
                           locked_approval_authority=arm.approval_authority)
    started = perf_counter()
    mission = None
    task_id = None
    failure = None
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        await host.start()
        mission = await arm.create_mission(missions)
        task_id = mission["task_id"]
        approved = False
        async with asyncio.timeout(task_timeout):
            while True:
                mission = missions.get(task_id)
                if mission["status"] == "awaiting_human":
                    active = missions._running.get(task_id)
                    if active is not None and not active.done():
                        await asyncio.sleep(.05)
                        continue
                    if approved or not mission.get("review"):
                        failure = "additional_human_review_required"
                        break
                    await arm.approve_named_tests(missions, mission)
                    review = missions.get(task_id)["review"]
                    await missions.approve(task_id,
                        expected_revision=review["revision"],
                        task_spec_hash=review["task_spec_hash"],
                        plan_hash=review["plan_hash"],
                        scope_hash=review["scope_hash"], decision="approved")
                    approved = True
                    continue
                if mission["status"] in {"succeeded", "failed", "cancelled", "retryable"}:
                    break
                await asyncio.sleep(.05)
    except TimeoutError:
        failure = "task_timeout"
    except Exception as exc:
        failure = "evaluation_error:" + type(exc).__name__
    finally:
        try:
            await missions.close()
        except Exception as exc:
            failure = failure or "isolation_cleanup_error:" + type(exc).__name__
        try:
            mission = missions.get(task_id) if task_id is not None else mission
            graph = missions.graph(task_id) if task_id is not None else None
            artifacts = missions.artifacts(task_id) if task_id is not None else {"items": []}
            observability = missions.observability(task_id) if task_id is not None else {}
            diff = missions.diff(task_id) if task_id is not None else None
            worktree = (missions.graphs.workspace_record(task_id)["worktree_path"]
                        if task_id is not None else None)
        except Exception as exc:
            failure = failure or "evidence_collection_error:" + type(exc).__name__
            graph, artifacts, observability, diff, worktree = None, {"items": []}, {}, None, None
        await host.close()
    def persist(name, value):
        (output / name).write_text(json.dumps(redactor.data(value), ensure_ascii=False,
            indent=2, allow_nan=False), encoding="utf-8")
    persist("mission.json", mission)
    persist("graph.json", graph)
    persist("artifacts.json", artifacts)
    persist("observability.json", observability)
    persist("diff.json", diff)
    artifact_items = artifacts["items"]
    validations = [item for item in artifact_items if item["artifact_type"] == "ValidationReport"]
    reviews = [item for item in artifact_items if item["artifact_type"] == "ReviewReport"]
    changes = [item for item in artifact_items if item["artifact_type"] == "ChangeSet"]
    validation = ValidationReport.model_validate(validations[-1]["content"]) if validations else None
    changed = {item["path"] for item in changes[-1]["content"]["files"]} if changes else set()
    allowed = set(arm.task.task_spec.allowed_files)
    required = set(arm.task.task_spec.required_changed)
    scope = changed <= allowed and required <= changed
    gates = {"flow": mission is not None and mission["status"] == "succeeded",
             "scope": scope,
             "build": validation is not None and validation.status == "passed",
             "ctest": validation is not None and validation.tests_passed > 0 and validation.tests_failed == 0,
             "oracle": validation is not None and validation.hidden_oracle == "passed",
             "integrity": validation is not None and validation.workspace_integrity == "passed",
             "test_integrity": validation is not None and validation.test_integrity == "passed",
             "review": bool(reviews and reviews[-1]["content"]["verdict"] == "approve")}
    success = bool(all(gates.values()) and validation_passed(validation)) if validation else False
    models = observability.get("model_calls", [])
    token_parts = ("input_tokens", "output_tokens", "total_tokens")
    tokens = {part: sum(item[part] for item in models) if all(
        type(item.get(part)) is int for item in models) else None for part in token_parts}
    repair_rounds = len(observability.get("repairs", []))
    node_failures = [node.get("result") or {} for node in graph.get("nodes", [])] if graph else []
    node_failure = next((item.get("error_summary") or item.get("failure_kind")
                         for item in reversed(node_failures)
                         if item.get("error_summary") or item.get("failure_kind")), None)
    row = {"id": arm.task.id, "category": arm.task.kind, "run_index": run_index,
           "agent_topology": arm.topology, "config_hash": config_hash,
           "success": success, "first_pass_success": success and repair_rounds == 0,
           "repair_success": success and repair_rounds > 0,
           "reason": "verified_executable" if success else failure or node_failure or
                     (validation.failure_classification if validation else mission["status"] if mission else "not_run"),
           "state": mission["status"] if mission else "not_run",
           "gates": gates, "scope_valid": scope,
           "build_status": "passed" if gates["build"] else "failed" if validation else "not_run",
           "ctest_status": "passed" if gates["ctest"] else "failed" if validation else "not_run",
           "oracle_status": validation.hidden_oracle if validation else "not_run",
           "test_status": validation.status if validation else "not_run",
           "test_count": validation.tests_passed + validation.tests_failed if validation else 0,
           "integrity_status": "passed" if gates["integrity"] and gates["test_integrity"] else "failed",
           "failure_classification": validation.failure_classification if validation else None,
           "tool_calls": len(observability.get("tools", [])),
           "prompt_tokens": tokens["input_tokens"], "completion_tokens": tokens["output_tokens"],
           "total_tokens": tokens["total_tokens"],
           "repair_rounds": repair_rounds,
           "safety_violations": len(observability.get("security_events", [])),
           "latency_ms": round((perf_counter() - started) * 1000, 3),
           "artifact_directory": str(output),
           "repository": worktree or str(arm.repository),
           "state_directory": str(state_root),
           "started_at": started_at,
           "finished_at": datetime.now(timezone.utc).isoformat()}
    persist("result.json", row)
    return row
