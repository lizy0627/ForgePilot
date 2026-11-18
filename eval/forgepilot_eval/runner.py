"""Repeatable isolated benchmark through the existing TaskService approval interface."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from time import perf_counter
from uuid import uuid4
from pydantic import BaseModel

from forgepilot.cli import ConsoleInput, SafeArgumentParser
from forgepilot.llm import OpenAICompatibleClient
from forgepilot.models.profiles import ModelConfiguration, ModelProfile, ModelRoute
from forgepilot.presets import BuildConfig
from forgepilot.process import executable, run_process
from forgepilot.rag import RagConfig, RagFileStore, RagManager, RagRuntimeSettings
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace

from .grader import Grader, check_scope, grade_analysis, initial_matches
from .campaign_budget import BudgetPlan, SharedTokenBudget
from .manifest import DATA_ROOT, asset, inventory, load_tasks, verify_real_lock
from .metrics import write_reports
from .offline import OfflineModel


class TokenBudget:
    """Fail closed when provider usage is absent; charge every completed request."""
    def __init__(self, limit):
        self.limit = limit
        self.spent = 0

    @property
    def remaining(self):
        return self.limit - self.spent

    def charge(self, usage):
        amount = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(amount) is not int or amount < 0:
            raise ValueError("Real benchmark requires provider total_tokens usage on every response")
        self.spent += amount
        if self.spent > self.limit:
            raise ValueError("Real benchmark token budget exceeded")


class BudgetedModel:
    def __init__(self, client, campaign, per_run_limit, *, shared_budget_path=None,
                 shared_budget_plan=None, budget_run_id=None):
        self.client = client
        self.campaign = campaign
        self.run = TokenBudget(per_run_limit)
        self.redactor = client.redactor
        self.shared_budget_path = shared_budget_path
        self.shared_budget_plan = shared_budget_plan
        self.budget_run_id = budget_run_id

    def _shared(self, method, *args):
        if self.shared_budget_path is not None:
            with SharedTokenBudget(self.shared_budget_path, self.shared_budget_plan) as ledger:
                return getattr(ledger, method)(*args)

    async def chat(self, messages, tools=None):
        remaining = min(self.campaign.remaining, self.run.remaining)
        if remaining <= 0:
            raise ValueError("Real benchmark token budget exhausted before request")
        # Provider-reported input is only known after a reply. Output is bounded
        # prospectively; a single request can still exceed the aggregate cap.
        self.client.max_output_tokens = min(4096, remaining)
        try:
            reply = await self.client.chat(messages, tools)
        except BaseException:
            # A timed-out or interrupted provider request may still be billable.
            # Its usage is unknowable, so no later paid request is allowed.
            self.campaign.spent = self.campaign.limit
            if self.shared_budget_path is not None:
                try:
                    self._shared("mark_uncertain", "provider_request_failed", self.budget_run_id)
                except Exception:
                    # An active reservation also blocks the next paid run.
                    pass
            raise
        try:
            if self.shared_budget_path is not None:
                amount = reply.usage.get("total_tokens") if isinstance(reply.usage, dict) else None
                self._shared("record_usage", self.budget_run_id, amount)
            self.campaign.charge(reply.usage)
            self.run.charge(reply.usage)
        except ValueError:
            # Missing accounting makes every subsequent paid request unsafe.
            self.campaign.spent = self.campaign.limit
            if self.shared_budget_path is not None:
                try:
                    with SharedTokenBudget(self.shared_budget_path, self.shared_budget_plan) as ledger:
                        if ledger.snapshot()["state"] == "ready":
                            ledger.mark_uncertain("provider_overrun", self.budget_run_id)
                except Exception:
                    pass
            raise
        return reply

    async def aclose(self):
        await self.client.aclose()


def configuration_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def source_revision(directory):
    """Hash actual source bytes, including uncommitted edits, for an A/B lock."""
    return configuration_digest(inventory(directory))


def benchmark_model_configuration(args, model):
    """Build the one frozen route table shared by both topology adapters.

    This is a pure configuration check: it never resolves a credential or
    creates a provider. Missing or overlapping families leave formal A/B
    unavailable rather than guessing independence from model identifiers.
    """
    if args.mode != "real":
        return None
    endpoint = os.environ.get("FORGEPILOT_BASE_URL")
    review_model = os.environ.get("FORGEPILOT_REVIEW_MODEL")
    task_family = os.environ.get("FORGEPILOT_TASK_MODEL_FAMILY")
    review_family = os.environ.get("FORGEPILOT_REVIEW_MODEL_FAMILY")
    if not all((endpoint, model, review_model, task_family, review_family)):
        return None
    if model == review_model:
        raise ValueError("Formal A/B Reviewer must use a different verified model family")
    common = dict(base_url=endpoint, api_key_ref="env:FORGEPILOT_API_KEY",
                  timeout_seconds=args.llm_timeout,
                  max_output_tokens=min(4096, args.max_tokens_per_run),
                  temperature=0.1)
    return ModelConfiguration(profiles=(
        ModelProfile(profile_id="benchmark-main", model=model, family=task_family, **common),
        ModelProfile(profile_id="benchmark-review", model=review_model, family=review_family, **common),
    ), routes=tuple(ModelRoute(route_id=name,
        primary="benchmark-review" if name == "review" else "benchmark-main")
        for name in ("analysis", "implementation", "orchestration", "repair", "review", "task")),
        enforce_review_family_separation=True)


def model_routing_lock(configuration):
    if configuration is None:
        return None
    profiles = [{"profile_id": profile.profile_id, "model": profile.model,
                 "family": profile.family,
                 "endpoint_sha256": hashlib.sha256(profile.base_url.encode()).hexdigest(),
                 "credential_ref": profile.api_key_ref,
                 "temperature": profile.temperature,
                 "max_output_tokens": profile.max_output_tokens,
                 "timeout_seconds": profile.timeout_seconds,
                 "supports_tool_calling": profile.supports_tool_calling}
                for profile in sorted(configuration.profiles, key=lambda item: item.profile_id)]
    routes = [{"route_id": route.route_id, "role": route.role,
               "primary": route.primary, "fallback": route.fallback}
              for route in sorted(configuration.routes,
                                  key=lambda item: (item.route_id, item.role or ""))]
    return {"profiles": profiles, "routes": routes,
            "enforce_review_family_separation": configuration.enforce_review_family_separation}


def comparison_lock(tasks, sources, args, environment, model, *, model_configuration=None):
    """Topology is intentionally excluded so unequal A/B conditions are detectable."""
    endpoint = os.environ.get("FORGEPILOT_BASE_URL") if args.mode == "real" else None
    project_root = DATA_ROOT.parent
    task_lock = None
    if args.dataset == "real":
        verify_real_lock(DATA_ROOT)
        task_lock = json.loads(asset(DATA_ROOT, "real_tasks/lock.json").read_text(encoding="utf-8"))["sha256"]
    agent_definitions = project_root / "agent" / "forgepilot" / "agents"
    # Effective wire settings of the legacy benchmark client; no credential or
    # raw endpoint enters the saved profile. A future multi adapter must prove it
    # uses this same primary profile before producing comparable real evidence.
    model_configuration = (model_configuration if model_configuration is not None else
                           benchmark_model_configuration(args, model))
    routing = model_routing_lock(model_configuration)
    main = model_configuration.router().resolve("task", "generalist").primary if model_configuration else None
    primary_profile = {"provider": "openai-compatible", "model": model,
                       "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest() if endpoint else None,
                       "credential_ref": "env:FORGEPILOT_API_KEY" if args.mode == "real" else None,
                       "timeout_seconds": main.timeout_seconds if main else args.llm_timeout,
                       "temperature": main.temperature if main else "provider_default",
                       "max_output_tokens": main.max_output_tokens if main else min(4096, args.max_tokens_per_run)}
    toolchain = {"tools": environment["tools"], "versions": environment["versions"],
                 "generator": args.generator, "qt_kit": args.qt_kit}
    payload = {"schema": 1, "dataset": args.dataset, "tasks": [t.model_dump(mode="json") for t in tasks],
               "source_hashes": sources, "task_lock_sha256": task_lock, "model": model,
               "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest() if endpoint else None,
               "model_profile": primary_profile,
               "model_profile_sha256": configuration_digest(primary_profile),
               "model_routing": routing,
               "model_routing_sha256": configuration_digest(routing) if routing else None,
               "code_commit": environment.get("code_commit"), "code_dirty": environment.get("code_dirty"),
               "max_total_tokens": args.max_total_tokens,
               "shared_budget_plan_sha256": BudgetPlan.formal().sha256,
               "max_tokens_per_run": args.max_tokens_per_run, "repetitions": args.repeats,
               "tools": environment["tools"], "tool_versions": environment["versions"],
               "runtime_revision": source_revision(project_root / "agent" / "forgepilot"),
               "evaluator_revision": source_revision(DATA_ROOT / "forgepilot_eval"),
               "prompt_sha256": hashlib.sha256((agent_definitions / "prompts.py").read_bytes()).hexdigest(),
               "agent_definition_sha256": hashlib.sha256(
                   (agent_definitions / "registry.py").read_bytes() +
                   (agent_definitions / "definitions.py").read_bytes()).hexdigest(),
               "task_spec_sha256": configuration_digest([t.model_dump(mode="json") for t in tasks]),
               "toolchain_sha256": configuration_digest(toolchain),
               "task_timeout": args.task_timeout, "llm_timeout": args.llm_timeout,
               "approval_timeout": args.approval_timeout,
               "generator": args.generator, "qt_kit": args.qt_kit, "rag": args.rag,
               "approval": args.approval, "agent_steps": 30, "repair_rounds": 3,
               "validation": "fresh_public_ctest_and_protected_oracle_v2"}
    return {"hash": configuration_digest(payload), "configuration": payload}


def verify_ab_pair(single, multi):
    if single.get("agent_topology") != "single" or multi.get("agent_topology") != "multi":
        raise ValueError("A/B pair requires single and multi topologies")
    for result in (single, multi):
        payload = result.get("comparison_configuration")
        if not isinstance(payload, dict) or configuration_digest(payload) != result.get("config_hash"):
            raise ValueError("A/B configuration hash does not match saved configuration")
    if single.get("config_hash") != multi.get("config_hash") or not single.get("config_hash"):
        raise ValueError("A/B configuration hash mismatch")
    return True


def now():
    return datetime.now(timezone.utc).isoformat()


def config_presence():
    values = {name: bool(os.environ.get(name)) for name in ("FORGEPILOT_BASE_URL", "FORGEPILOT_API_KEY")}
    values["FORGEPILOT_TASK_MODEL"] = bool(
        os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ.get("FORGEPILOT_MODEL"))
    return values


def save(path, data, redactor):
    def plain(value):
        if isinstance(value, BaseModel): return plain(value.model_dump(mode="json"))
        if isinstance(value, dict): return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)): return [plain(v) for v in sorted(value)] if isinstance(value, set) else [plain(v) for v in value]
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redactor.data(plain(data)), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def empty_row(task, run_index=1):
    return {"id": task.id, "category": task.category, "run_index": run_index,
            "success": False, "first_pass_success": False, "repair_success": False,
            "reason": "not_run", "state": "not_run",
            "build_status": "not_applicable" if task.analysis else "not_run",
            "test_status": "not_applicable" if task.analysis else "not_run", "test_count": 0,
            "ctest_status": "not_applicable" if task.analysis else "not_run",
            "oracle_status": "not_applicable" if task.analysis else "not_run",
            "integrity_status": "not_run", "scope_valid": None, "config_hash": None,
            "approval_not_granted": False, "gates": {},
            "safety_violations": None,
            "tool_calls": None, "total_tokens": None, "latency_ms": None,
            "agent_latency_ms": None, "repair_rounds": None,
            "repository": None, "artifact_directory": None}


async def approve(service, pending, task, mode, read_line, *, timeout_seconds=None):
    proposed = [f.path for f in pending.plan.files_to_modify]
    allowed = {p.casefold() for p in task.allowed_files}
    decision = {"task_id": pending.task_id, "plan_version": pending.plan_version, "plan": pending.plan.model_dump(mode="json"),
                "allowed_files": task.allowed_files, "preauthorized_files": task.preauthorized_files,
                "mode": mode, "approved": False}
    if not {p.casefold() for p in proposed} <= allowed:
        decision["reason"] = "authorization_scope"
    elif mode == "manifest":
        if task.preauthorized_files is None or not {p.casefold() for p in proposed} <= {p.casefold() for p in task.preauthorized_files}:
            decision["reason"] = "preauthorization_missing_or_exceeded"
        else: decision.update(approved=True, reason="manifest_explicit_file_scope")
    else:
        print(json.dumps(service.redactor.data(decision), ensure_ascii=False, indent=2), flush=True)
        print(f"Approve exactly this plan: approve {pending.plan_version}; any other answer/EOF stops.", flush=True)
        try:
            response = (await asyncio.wait_for(read_line(), timeout_seconds)
                        if timeout_seconds is not None else await read_line())
            accepted = response.strip() == f"approve {pending.plan_version}"
            decision.update(approved=accepted, reason="human_review" if accepted else "human_review_denied")
        except TimeoutError:
            decision.update(approved=False, reason="approval_timeout")
    if decision["approved"]:
        # Same task-bound, version-bound API used by CLI/HTTP; no mutable grant shortcuts.
        service.approve(pending.task_id, pending.plan_version)
    else:
        service.cancel_review(pending.task_id)
    return decision


class Benchmark:
    def __init__(self, args, *, model_factory=None, read_line=None):
        self.args = args
        if args.agent_topology != "single":
            raise ValueError("Multi-Agent benchmark adapter is not yet connected; no A/B result was produced")
        if args.dataset != "micro":
            raise ValueError("Real-task runner is not yet connected; locked manifests are not executable results")
        if args.mode == "real" and (not args.allow_paid_real or args.max_total_tokens is None):
            raise ValueError("Paid real benchmark requires --allow-paid-real and --max-total-tokens")
        if args.mode == "real":
            # Candidate-controlled CMake/CTest code currently shares the evaluator's
            # filesystem and network identity. A path outside the worktree alone
            # cannot keep protected tests secret from that code.
            raise ValueError("Real benchmark isolation unavailable: candidate processes require a verified restricted identity or VM before paid evaluation")
        if args.mode == "real":
            campaign = f"{args.dataset}:{args.agent_topology}"
            plan = BudgetPlan.formal()
            if not args.shared_budget_ledger or args.max_total_tokens != plan.cap(campaign):
                raise ValueError("Formal real benchmark requires a shared budget ledger and the locked campaign cap")
            if not plan.capacity_report(max_tokens_per_run=args.max_tokens_per_run)[campaign]["guaranteed_full_run_capacity"]:
                raise ValueError("Per-run token limit exceeds the locked campaign's full-run capacity")
        if args.mode == "offline" and args.rag == "on":
            raise ValueError("RAG-on Agent evaluation requires --mode real; offline scripts are not Agent quality evidence")
        self.dataset = DATA_ROOT
        self.tasks = load_tasks(self.dataset)
        if args.task:
            selected = set(args.task)
            if not selected <= {t.id for t in self.tasks}: raise ValueError("Unknown task id")
            self.tasks = [t for t in self.tasks if t.id in selected]
        self.redactor = Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),))
        self.cmake = executable(args.cmake, "cmake")
        self.ctest = executable(args.ctest or str(Path(self.cmake).with_name("ctest.exe")), "ctest")
        self.git, self.rg = executable(args.git, "git"), executable(args.rg, "rg")
        self.ninja = executable(args.ninja, "ninja") if args.generator == "Ninja" else None
        if any(t.qt for t in self.tasks):
            if not args.qt_kit or not (Path(args.qt_kit).is_absolute() and (Path(args.qt_kit) / "lib/cmake/Qt6/Qt6Config.cmake").is_file()):
                raise ValueError("Qt tasks require the existing absolute --qt-kit / FORGEPILOT_QT_KIT")
        self.grader = Grader(self.dataset, self.cmake, self.ctest, generator=args.generator, ninja=self.ninja, qt_kit=args.qt_kit)
        self.model_factory = model_factory
        self.token_budget = TokenBudget(args.max_total_tokens) if args.mode == "real" else None
        self.shared_budget_path = args.shared_budget_ledger if args.mode == "real" else None
        self.shared_budget_plan = BudgetPlan.formal() if args.mode == "real" else None
        self.budget_campaign = f"{args.dataset}:{args.agent_topology}"
        self.read_line = read_line or ConsoleInput().read
        self.solutions = None
        self.rag_manager = None
        self.rag_store = None
        if args.mode == "offline":
            self.solutions = json.loads(asset(self.dataset, "offline-solutions.json").read_text(encoding="utf-8"))["solutions"]
        elif not all(config_presence().values()):
            raise ValueError("Real LLM configuration is incomplete; no real benchmark results were produced. Use explicit --mode offline for evaluator validation.")

    def model(self, task, budget_run_id=None):
        if self.model_factory: return self.model_factory(task)
        if self.args.mode == "offline": return OfflineModel(task, self.solutions[task.id])
        client = OpenAICompatibleClient(os.environ["FORGEPILOT_BASE_URL"], os.environ["FORGEPILOT_API_KEY"],
                                      os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ["FORGEPILOT_MODEL"],
                                      timeout=self.args.llm_timeout)
        return BudgetedModel(client, self.token_budget, self.args.max_tokens_per_run,
                             shared_budget_path=self.shared_budget_path,
                             shared_budget_plan=self.shared_budget_plan,
                             budget_run_id=budget_run_id)

    async def environment(self):
        values = {"python": sys.version, "executable": sys.executable, "prefix": sys.prefix,
                  "os": platform.platform(), "machine": platform.machine(),
                  "packages": {p: importlib.metadata.version(p) for p in ("httpx", "pydantic", "fastapi")},
                  "generator": self.args.generator, "configuration": "Debug", "qt_kit": self.args.qt_kit,
                  "tools": {"cmake": self.cmake, "ctest": self.ctest, "git": self.git, "rg": self.rg, "ninja": self.ninja}}
        versions = {}
        for name in ("cmake", "ctest", "git", "rg", "ninja"):
            path = values["tools"][name]
            if path:
                result = await run_process([path, "--version"], Path(tempfile.gettempdir()))
                versions[name] = {"exit_code": result.returncode, "output": result.stdout.decode("utf-8", errors="replace")[:2048]}
        if self.args.qt_kit:
            path = Path(self.args.qt_kit) / "bin/qmake.exe"
            result = await run_process([str(path), "-query", "QT_VERSION"], Path(tempfile.gettempdir()))
            versions["qt"] = {"exit_code": result.returncode, "output": result.stdout.decode("utf-8", errors="replace").strip()}
        try:
            compiler = executable(None, "cl")
            result = await run_process([compiler], Path(tempfile.gettempdir()))
            versions["msvc"] = {"path": compiler, "exit_code": result.returncode,
                                "output": (result.stdout + result.stderr).decode("utf-8", errors="replace")[:2048]}
        except Exception:
            versions["msvc"] = {"status": "not_in_current_process_path"}
        values["versions"] = versions
        root = DATA_ROOT.parent
        revision = await run_process([self.git, "rev-parse", "HEAD"], root)
        status = await run_process([self.git, "status", "--porcelain=v1", "--untracked-files=all"], root)
        if revision.returncode or status.returncode:
            raise ValueError("Cannot record benchmark code commit and dirty state")
        values["code_commit"] = revision.stdout.decode("ascii", errors="strict").strip()
        values["code_dirty"] = bool(status.stdout.strip())
        return values

    async def run_task(self, task, run_directory, source_hashes, run_index=1):
        run_started = perf_counter()
        row = empty_row(task, run_index)
        row["config_hash"] = getattr(self, "config_hash", None)
        directory = run_directory / task.id / f"run-{run_index:02d}"
        directory.mkdir(parents=True)
        # Candidate copies live under an OS-created unique temp directory, never the user's checkout.
        temp_root = Path(tempfile.mkdtemp(prefix=f"ForgePilot-{task.id}-"))
        repo = temp_root / "repository"
        source = asset(self.dataset, task.fixture)
        inventory(source)
        shutil.copytree(source, repo)
        (repo / ".gitignore").write_text(".forgepilot/\n", encoding="utf-8")
        if not task.analysis: self.grader.write_presets(repo)
        row.update(repository=str(repo), artifact_directory=str(directory), temp_root=str(temp_root), started_at=now())
        save(directory / "task.json", task.model_dump(mode="json"), self.redactor)
        trace, service, llm = MemoryTrace(self.redactor), None, None
        budget_run_id = None
        result = None
        decisions = []
        latency = 0.0
        before = inventory(repo, ignore_generated=True)
        baseline, verification, scope = None, None, None
        try:
            for argv in ([self.git, "init", "-q", str(repo)], [self.git, "-C", str(repo), "add", "--", "."]):
                git_result = await run_process(argv, temp_root)
                if git_result.returncode: raise ValueError("Cannot initialize isolated benchmark Git repository")
            if not task.analysis:
                baseline = await self.grader.run(task, repo, temp_root / "baseline", baseline=True)
                save(directory / "baseline.json", baseline, self.redactor)
                if not initial_matches(task, baseline):
                    row["reason"] = "initial_state_mismatch_or_environment_failure"
                    return row
            if inventory(self.dataset) != source_hashes: raise ValueError("Protected dataset changed")
            if inventory(repo, ignore_generated=True) != before: raise ValueError("Baseline modified source")
            if self.shared_budget_path is not None:
                proposed_id = f"{self.config_hash}:{task.id}:{run_index}:{uuid4().hex}"
                with SharedTokenBudget(self.shared_budget_path, self.shared_budget_plan) as ledger:
                    ledger.begin_run(self.budget_campaign, proposed_id, self.args.max_tokens_per_run)
                budget_run_id = proposed_id
                row["budget_run_id"] = budget_run_id
            llm = self.model(task, budget_run_id)
            config = None if task.analysis else BuildConfig(self.cmake, self.ctest, configure_preset="eval",
                build_preset="eval-build", test_preset="eval-test", configure_timeout=120, build_timeout=180, test_timeout=60)
            service = TaskService(llm, rg_executable=self.rg, git_executable=self.git, trace=trace,
                                  redactor=self.redactor, build_config=config,
                                  rag_manager=self.rag_manager)
            async def segment(operation):
                nonlocal latency
                started = perf_counter()
                try: return await operation
                finally: latency += (perf_counter() - started) * 1000
            # Budget is shared across all Agent segments, excluding human wait. Runtime keeps its 30 steps.
            remaining = self.args.task_timeout
            operation = service.analyze if task.analysis else service.start
            start = perf_counter()
            async with asyncio.timeout(remaining) as deadline:
                result = await segment(operation(str(repo), task.request))
            remaining -= perf_counter() - start
            while result.state == "waiting_approval":
                decision = await approve(service, result, task, self.args.approval, self.read_line,
                                         timeout_seconds=self.args.approval_timeout)
                decisions.append(decision)
                if not decision["approved"]:
                    row["reason"] = decision["reason"]
                    row["approval_not_granted"] = self.args.approval == "manual" and decision["reason"] in {
                        "human_review_denied", "approval_timeout"}
                    result = service.snapshot()
                    break
                if remaining <= 0:
                    service.cancel(); result = service.snapshot(); row["reason"] = "task_timeout"; break
                start = perf_counter()
                async with asyncio.timeout(remaining) as deadline:
                    result = await segment(service.execute(result.task_id))
                remaining -= perf_counter() - start
            if deadline.expired() or remaining <= 0: row["reason"] = "task_timeout"
            after = inventory(repo, ignore_generated=True)
            scope = check_scope(before, after, task)
            row["scope_valid"] = scope["valid"]
            if inventory(self.dataset) != source_hashes: raise ValueError("Protected dataset changed")
            if not scope["valid"]:
                row["reason"] = "source_scope_or_required_changes"
            elif row["reason"] == "not_run":
                if task.analysis:
                    row["gates"] = {"flow": result.state == "completed", "scope": scope["valid"],
                                    "analysis_oracle": grade_analysis(result.final_result, task.success.expected),
                                    "integrity": inventory(self.dataset) == source_hashes}
                    row["success"] = all(row["gates"].values())
                    row["reason"] = "verified_analysis" if row["success"] else "analysis_or_runtime_failed"
                else:
                    verification = await self.grader.run(task, repo, temp_root / "final")
                    row.update(build_status=verification["build_status"], test_status=verification["test_status"],
                               ctest_status=verification["public_test_status"],
                               test_count=verification["test_count"], oracle_status=verification["oracle_status"])
                    repair_ok = result.repair_round >= task.success.minimum_repair_rounds
                    if task.success.minimum_repair_rounds:
                        repair_ok = repair_ok and any(any(s == "failed" for s in h["stages"].values()) for h in result.validation_history[:-1])
                    row["gates"] = {"flow": result.state == "completed" and result.outcome == "modification_validated",
                                    "scope": scope["valid"], "build": verification["configure_status"] == "passed"
                                        and verification["build_status"] == "passed",
                                    "ctest": verification["public_test_status"] == "passed"
                                        and verification["public_test_count"] > 0,
                                    "oracle": verification["oracle_status"] == "passed"
                                        and verification["oracle_test_count"] > 0,
                                    "test_integrity": isinstance(result.test_integrity, dict)
                                        and result.test_integrity.get("status") == "passed",
                                    "integrity": verification["protected_unchanged"]
                                        and inventory(self.dataset) == source_hashes,
                                    "repair_contract": repair_ok}
                    row["success"] = all(row["gates"].values())
                    row["reason"] = "verified_executable" if row["success"] else "oracle_runtime_or_repair_failed"
            if inventory(repo, ignore_generated=True) != after or inventory(self.dataset) != source_hashes:
                row.update(success=False, reason="protected_or_candidate_sources_modified_during_grading", test_status="failed")
                row["gates"]["integrity"] = False
            row["integrity_status"] = ("passed" if row["gates"].get("integrity")
                                       and row["gates"].get("test_integrity", True) else "failed")
        except asyncio.CancelledError:
            row["reason"] = "cancelled"
            raise
        except TimeoutError:
            row["reason"] = "task_timeout"
        except Exception as exc:
            # Do not serialize raw arbitrary exception values (credentials/paths may be embedded).
            row.update(success=False, reason="evaluation_error:" + type(exc).__name__)
        finally:
            if service is not None:
                service.close()
                result = service.snapshot(persist_diff=True)
            if llm is not None:
                close = getattr(llm, "aclose", None)
                if close is not None: await close()
            if budget_run_id is not None:
                with SharedTokenBudget(self.shared_budget_path, self.shared_budget_plan) as ledger:
                    if ledger.snapshot()["state"] == "ready":
                        ledger.finish_run(budget_run_id, state="completed" if row["success"] else "failed")
            if result is not None:
                row.update(state=result.state.value, tool_calls=result.tool_calls, total_tokens=result.token_usage["total_tokens"],
                           agent_latency_ms=round(latency, 3), repair_rounds=None if task.analysis else result.repair_round,
                           agent_task_id=result.task_id, usage=result.token_usage, steps=result.steps,
                           agent_error=result.error, agent_outcome=result.outcome)
                save(directory / "agent-result.json", asdict(result), self.redactor)
                row["first_pass_success"] = row["success"] and result.repair_round == 0
                row["repair_success"] = row["success"] and result.repair_round > 0
            row["latency_ms"] = round((perf_counter() - run_started) * 1000, 3)
            row["finished_at"] = now()
            save(directory / "approvals.json", decisions, self.redactor)
            save(directory / "trace.json", [asdict(e) for e in trace.events], self.redactor)
            save(directory / "verification.json", verification, self.redactor)
            save(directory / "scope.json", scope, self.redactor)
            save(directory / "result.json", row, self.redactor)
        return row

    async def run(self):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        output = Path(self.args.output_root).resolve() / (self.args.mode + "-" + run_id)
        output.mkdir(parents=True, exist_ok=False)
        if self.args.rag == "on":
            embedding_model = os.environ.get("FORGEPILOT_EMBEDDING_MODEL")
            embedding_base_url = os.environ.get("FORGEPILOT_EMBEDDING_BASE_URL")
            embedding_api_key = (os.environ.get("FORGEPILOT_EMBEDDING_API_KEY")
                                 or os.environ.get("FORGEPILOT_API_KEY"))
            if not embedding_model or not embedding_base_url or not embedding_api_key:
                raise ValueError("RAG-on evaluation needs embedding model, base URL and runtime credential")
            settings = RagRuntimeSettings(
                config=RagConfig(
                    enabled=True, embedding_model=embedding_model,
                    embedding_base_url=embedding_base_url),
                embedding_api_key=embedding_api_key)
            self.rag_store = SqliteEventStore(output / "rag-state.sqlite3", redactor=self.redactor)
            self.rag_manager = RagManager(
                settings, RagFileStore(self.rag_store), vector_path=output / "qdrant")
            if not self.rag_manager.index_available:
                self.rag_store.close()
                self.rag_store = None
                raise ValueError("RAG-on evaluation could not initialize embeddings or Qdrant")
        sources = inventory(self.dataset)
        environment = await self.environment()
        model_name = ("injected-fake-model" if self.model_factory is not None else
                      "offline-fixed-script" if self.args.mode == "offline" else
                      (os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ["FORGEPILOT_MODEL"]))
        lock = comparison_lock(self.tasks, sources, self.args, environment, model_name)
        self.config_hash = lock["hash"]
        metadata = {"run_id": run_id, "mode": self.args.mode, "model": model_name,
            "endpoint_sha256": lock["configuration"]["endpoint_sha256"],
            "real_task_lock_sha256": lock["configuration"]["task_lock_sha256"],
            "model_profile_sha256": lock["configuration"]["model_profile_sha256"],
            "model_routing_sha256": lock["configuration"]["model_routing_sha256"],
            "code_commit": environment.get("code_commit"), "code_dirty": environment.get("code_dirty"),
            "prompt_sha256": lock["configuration"]["prompt_sha256"],
            "agent_definition_sha256": lock["configuration"]["agent_definition_sha256"],
            "task_spec_sha256": lock["configuration"]["task_spec_sha256"],
            "toolchain_sha256": lock["configuration"]["toolchain_sha256"],
            "started_at": now(), "finished_at": None, "selected_tasks": [t.id for t in self.tasks], "dataset_version": 1,
            "dataset_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
            "source_hashes": sources, "environment": environment, "approval_mode": self.args.approval,
            "configuration_presence": config_presence(), "real_llm_results_produced": False,
            "agent_topology": self.args.agent_topology, "config_hash": lock["hash"],
            "comparison_configuration": lock["configuration"], "repetitions": self.args.repeats,
            "task_categories": {t.id: t.category for t in self.tasks},
            "evidence_label": "fake" if self.model_factory is not None else "real" if self.args.mode == "real" else "offline",
            "rag": self.args.rag, "evaluation_type": "fake_agent_harness" if self.model_factory is not None else
                "real_agent" if self.args.mode == "real" else "offline_agent_harness",
            "limits": {"agent_steps": 30, "repair_rounds": 3, "task_seconds": self.args.task_timeout,
                       "llm_seconds": self.args.llm_timeout, "approval_seconds": self.args.approval_timeout,
                       "temperature": "provider_default", "mcp": "disabled",
                       "max_total_tokens": self.args.max_total_tokens, "max_tokens_per_run": self.args.max_tokens_per_run}}
        plan = [(t, i) for t in self.tasks for i in range(1, self.args.repeats + 1)]
        rows = [empty_row(t, i) for t, i in plan]
        for row in rows: row["config_hash"] = self.config_hash
        try:
            for index, (task, run_index) in enumerate(plan):
                print(f"[{index+1}/{len(plan)}] {task.id} run {run_index} ({self.args.mode})", flush=True)
                try:
                    rows[index] = await self.run_task(task, output, sources, run_index)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    rows[index].update(reason="evaluation_error:" + type(exc).__name__, finished_at=now())
                    failure = output / task.id / f"run-{run_index:02d}"
                    save(failure / "result.json", rows[index], self.redactor)
                print(f"  success={rows[index]['success']} reason={rows[index]['reason']}", flush=True)
                write_reports(output, metadata, rows, self.redactor)
                if inventory(self.dataset) != sources:
                    metadata["stopped_reason"] = "protected_dataset_changed"
                    break
                if rows[index].get("approval_not_granted"):
                    # The stdin reader may still hold a late answer. Never pair it
                    # with another task's review, even if the plan version matches.
                    metadata["stopped_reason"] = "manual_approval_not_granted"
                    break
                if self.token_budget is not None and self.token_budget.remaining <= 0 and index + 1 < len(plan):
                    metadata["stopped_reason"] = "token_budget_exhausted_or_unknown"
                    break
        except asyncio.CancelledError:
            metadata["stopped_reason"] = "cancelled"
            partial = output / task.id / f"run-{run_index:02d}" / "result.json"
            if partial.is_file(): rows[index] = json.loads(partial.read_text(encoding="utf-8"))
            raise
        finally:
            metadata["finished_at"] = now()
            metadata["real_llm_results_produced"] = (self.args.mode == "real" and self.model_factory is None
                                                      and any(r.get("total_tokens") is not None for r in rows))
            if self.token_budget is not None: metadata["tokens_charged"] = self.token_budget.spent
            summary = write_reports(output, metadata, rows, self.redactor)
            print("Report: " + str(output / "report.md"), flush=True)
            if self.rag_manager is not None:
                await self.rag_manager.aclose()
            if self.rag_store is not None:
                self.rag_store.close()
        return output, rows, summary


def parser():
    p = SafeArgumentParser(description="ForgePilot isolated 15-task benchmark; no automatic source permission bypass")
    p.add_argument("--mode", choices=("real", "offline"), default="offline")
    p.add_argument("--dataset", choices=("micro", "real"), default="micro")
    p.add_argument("--agent-topology", choices=("single", "multi"), default="single")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--allow-paid-real", action="store_true")
    p.add_argument("--max-total-tokens", type=int)
    p.add_argument("--shared-budget-ledger", help="Persistent token ledger shared by all four formal campaigns")
    p.add_argument("--max-tokens-per-run", type=int, default=20000)
    p.add_argument("--rag", choices=("off", "on"), default="off",
                   help="Run the real Agent benchmark with code RAG disabled or enabled")
    p.add_argument("--approval", choices=("manual", "manifest"), default="manual")
    p.add_argument("--approval-timeout", type=float, default=300)
    p.add_argument("--task", action="append", help="Select a task; repeat for a subset. Report denominators use selected tasks.")
    p.add_argument("--output-root", default=str(DATA_ROOT.parent / "logs/benchmark"))
    for name in ("cmake", "ctest", "git", "rg", "ninja", "qt-kit"):
        p.add_argument("--" + name, default=os.environ.get("FORGEPILOT_" + name.upper().replace("-", "_")))
    p.add_argument("--generator", choices=("Visual Studio 17 2022", "Ninja"), default="Visual Studio 17 2022")
    p.add_argument("--task-timeout", type=float, default=900)
    p.add_argument("--llm-timeout", type=float, default=60)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 0 < args.task_timeout <= 7200 or not 0 < args.llm_timeout <= 600:
            raise ValueError("Timeouts must be finite and within supported limits")
        if not 0 < args.approval_timeout <= 7200:
            raise ValueError("Approval timeout must be finite and within supported limits")
        if not 1 <= args.repeats <= 3 or not 1 <= args.max_tokens_per_run <= 1_000_000:
            raise ValueError("Invalid repetition or per-run token limit")
        if args.max_total_tokens is not None and not 1 <= args.max_total_tokens <= 20_000_000:
            raise ValueError("Invalid total token limit")
        if args.mode == "real" and args.repeats != 3:
            raise ValueError("Formal real benchmark requires exactly three runs per task")
        benchmark = Benchmark(args)
        _, rows, summary = asyncio.run(benchmark.run())
        stable = summary["Task Success Rate"]["passed"]
        required = 14 if len(benchmark.tasks) == 15 and args.repeats == 3 else len(benchmark.tasks)
        return 0 if stable >= required and all("finished_at" in row for row in rows) else 1
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 130
    except Exception as exc:
        text = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),)).text(text), file=sys.stderr)
        return 2
