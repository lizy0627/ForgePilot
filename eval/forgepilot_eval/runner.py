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
from forgepilot.presets import BuildConfig
from forgepilot.process import executable, run_process
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace

from .grader import Grader, check_scope, grade_analysis, initial_matches
from .manifest import DATA_ROOT, asset, inventory, load_tasks
from .metrics import write_reports
from .offline import OfflineModel


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


def empty_row(task):
    return {"id": task.id, "category": task.category, "success": False, "reason": "not_run", "state": "not_run",
            "build_status": "not_applicable" if task.analysis else "not_run",
            "test_status": "not_applicable" if task.analysis else "not_run", "test_count": 0,
            "tool_calls": None, "total_tokens": None, "latency_ms": None, "repair_rounds": None,
            "repository": None, "artifact_directory": None}


async def approve(service, pending, task, mode, read_line):
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
        decision.update(approved=(await read_line()).strip() == f"approve {pending.plan_version}", reason="human_review")
    if decision["approved"]:
        # Same task-bound, version-bound API used by CLI/HTTP; no mutable grant shortcuts.
        service.approve(pending.task_id, pending.plan_version)
    else:
        service.cancel_review(pending.task_id)
    return decision


class Benchmark:
    def __init__(self, args, *, model_factory=None, read_line=None):
        self.args = args
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
        self.read_line = read_line or ConsoleInput().read
        self.solutions = None
        if args.mode == "offline":
            self.solutions = json.loads(asset(self.dataset, "offline-solutions.json").read_text(encoding="utf-8"))["solutions"]
        elif not all(config_presence().values()):
            raise ValueError("Real LLM configuration is incomplete; no real benchmark results were produced. Use explicit --mode offline for evaluator validation.")

    def model(self, task):
        if self.model_factory: return self.model_factory(task)
        if self.args.mode == "offline": return OfflineModel(task, self.solutions[task.id])
        return OpenAICompatibleClient(os.environ["FORGEPILOT_BASE_URL"], os.environ["FORGEPILOT_API_KEY"],
                                      os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ["FORGEPILOT_MODEL"],
                                      timeout=self.args.llm_timeout)

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
        return values

    async def run_task(self, task, run_directory, source_hashes):
        row = empty_row(task)
        directory = run_directory / task.id
        directory.mkdir()
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
            llm = self.model(task)
            config = None if task.analysis else BuildConfig(self.cmake, self.ctest, configure_preset="eval",
                build_preset="eval-build", test_preset="eval-test", configure_timeout=120, build_timeout=180, test_timeout=60)
            service = TaskService(llm, rg_executable=self.rg, git_executable=self.git, trace=trace,
                                  redactor=self.redactor, build_config=config)
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
                decision = await approve(service, result, task, self.args.approval, self.read_line)
                decisions.append(decision)
                if not decision["approved"]:
                    row["reason"] = decision["reason"]
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
            if inventory(self.dataset) != source_hashes: raise ValueError("Protected dataset changed")
            if not scope["valid"]:
                row["reason"] = "source_scope_or_required_changes"
            elif row["reason"] == "not_run":
                if task.analysis:
                    row["success"] = result.state == "completed" and grade_analysis(result.final_result, task.success.expected)
                    row["reason"] = "verified_analysis" if row["success"] else "analysis_or_runtime_failed"
                else:
                    verification = await self.grader.run(task, repo, temp_root / "final")
                    row.update(build_status=verification["build_status"], test_status=verification["test_status"], test_count=verification["test_count"])
                    repair_ok = result.repair_round >= task.success.minimum_repair_rounds
                    if task.success.minimum_repair_rounds:
                        repair_ok = repair_ok and any(any(s == "failed" for s in h["stages"].values()) for h in result.validation_history[:-1])
                    row["success"] = (result.state == "completed" and result.outcome == "modification_validated"
                        and verification["build_status"] == "passed" and verification["test_status"] == "passed"
                        and verification["test_count"] > 0 and verification["protected_unchanged"] and repair_ok)
                    row["reason"] = "verified_executable" if row["success"] else "oracle_runtime_or_repair_failed"
            if inventory(repo, ignore_generated=True) != after or inventory(self.dataset) != source_hashes:
                row.update(success=False, reason="protected_or_candidate_sources_modified_during_grading", test_status="failed")
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
            if result is not None:
                row.update(state=result.state.value, tool_calls=result.tool_calls, total_tokens=result.token_usage["total_tokens"],
                           latency_ms=round(latency, 3), repair_rounds=None if task.analysis else result.repair_round,
                           agent_task_id=result.task_id, usage=result.token_usage, steps=result.steps,
                           agent_error=result.error, agent_outcome=result.outcome)
                save(directory / "agent-result.json", asdict(result), self.redactor)
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
        sources = inventory(self.dataset)
        metadata = {"run_id": run_id, "mode": self.args.mode, "model": "offline-fixed-script" if self.args.mode == "offline" else (os.environ.get("FORGEPILOT_TASK_MODEL") or os.environ["FORGEPILOT_MODEL"]),
            "endpoint": None if self.args.mode == "offline" else os.environ["FORGEPILOT_BASE_URL"],
            "started_at": now(), "finished_at": None, "selected_tasks": [t.id for t in self.tasks], "dataset_version": 1,
            "dataset_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
            "source_hashes": sources, "environment": await self.environment(), "approval_mode": self.args.approval,
            "configuration_presence": config_presence(), "real_llm_results_produced": self.args.mode == "real",
            "limits": {"agent_steps": 30, "repair_rounds": 3, "task_seconds": self.args.task_timeout,
                       "llm_seconds": self.args.llm_timeout, "temperature": "provider_default", "mcp": "disabled"}}
        rows = [empty_row(t) for t in self.tasks]
        try:
            for index, task in enumerate(self.tasks):
                print(f"[{index+1}/{len(self.tasks)}] {task.id} ({self.args.mode})", flush=True)
                rows[index] = await self.run_task(task, output, sources)
                print(f"  success={rows[index]['success']} reason={rows[index]['reason']}", flush=True)
                write_reports(output, metadata, rows, self.redactor)
                if inventory(self.dataset) != sources:
                    metadata["stopped_reason"] = "protected_dataset_changed"
                    break
        except asyncio.CancelledError:
            metadata["stopped_reason"] = "cancelled"
            partial = output / task.id / "result.json"
            if partial.is_file(): rows[index] = json.loads(partial.read_text(encoding="utf-8"))
            raise
        finally:
            metadata["finished_at"] = now()
            metadata["real_llm_results_produced"] = self.args.mode == "real" and any(r.get("steps", 0) > 0 for r in rows)
            summary = write_reports(output, metadata, rows, self.redactor)
            print("Report: " + str(output / "report.md"), flush=True)
        return output, rows, summary


def parser():
    p = SafeArgumentParser(description="ForgePilot isolated 15-task benchmark; no automatic source permission bypass")
    p.add_argument("--mode", choices=("real", "offline"), default="real")
    p.add_argument("--approval", choices=("manual", "manifest"), default="manual")
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
        benchmark = Benchmark(args)
        _, rows, _ = asyncio.run(benchmark.run())
        return 0 if all(r["success"] for r in rows) else 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        text = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),)).text(text), file=sys.stderr)
        return 2
