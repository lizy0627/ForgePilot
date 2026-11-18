"""CLI adapter. It displays events; it does not implement the Agent loop."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread

from .context import TaskState
from .errors import ForgePilotError
from .models.accounting import close_accounting_logging, configure_accounting_logging
from .process import executable
from .security import Redactor
from .mission_events import safe_event_data
from .task_service import TaskService
from .trace import TraceEvent
from .settings import add_build_arguments, add_connection_arguments, build_config_from_args, add_storage_arguments
from .sqlite_store import SqliteEventStore
from .recording import TaskJournal
from .settings import add_code_intelligence_arguments, code_intelligence_from_args
from .settings import add_mcp_arguments, mcp_from_args
from .settings import model_gateway_from_args
from .rag import (RagFileStore, RagManager, add_rag_arguments,
                  rag_runtime_availability_from_args)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(Redactor((os.environ.get("FORGEPILOT_API_KEY", ""),)).text(message))


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description="ForgePilot CLI MVP: approved changes, CMake/CTest validation, task diff and explicit restore")
    result.add_argument("repository_path", help="Local Git repository root")
    result.add_argument("prompt", help="Analysis request (quote text containing spaces)")
    add_connection_arguments(result)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--analysis-only", action="store_true", help="Phase 1 analysis without plan/review")
    mode.add_argument("--plan-only", action="store_true", help="Generate/review a plan without executing writes")
    mode.add_argument("--modify-only", action="store_true", help="Phase 3 writes without Build/Test")
    add_build_arguments(result)
    add_storage_arguments(result)
    add_code_intelligence_arguments(result)
    add_mcp_arguments(result)
    add_rag_arguments(result)
    return result


def display_event(event: TraceEvent) -> None:
    if event.kind in {"task_state_changed", "tool_started", "tool_finished", "task_error", "file_changed", "file_restore", "git_baseline", "build_command_finished", "reflection", "validation_started", "validation_finished", "mcp_status"}:
        print(f"[{event.sequence} {event.kind}] {json.dumps(event.data, ensure_ascii=False)}", flush=True)


def _public_json(redactor: Redactor, value, *, indent=None) -> str:
    return json.dumps(safe_event_data(redactor, value), ensure_ascii=False, indent=indent)


class ConsoleInput:
    """A single daemon owns stdin, including after cancellation and re-approval."""
    def __init__(self):
        self.queue = Queue(maxsize=1)
        self.started = False
        self.eof = False

    def _read(self):
        while True:
            try:
                line = sys.stdin.readline(4_097)
                if len(line) > 4_096:
                    while line and not line.endswith("\n"):
                        line = sys.stdin.readline(4_097)
                    line = "input_too_long\n"
            except (OSError, ValueError):
                line = ""
            self.queue.put(line)
            if line == "":
                return

    async def read(self):
        if not self.started:
            Thread(target=self._read, daemon=True).start()
            self.started = True
        while not self.eof:
            try:
                value = self.queue.get_nowait()
                self.eof = value == ""
                return value
            except Empty:
                await asyncio.sleep(0.05)
        return ""


async def review_plan(service: TaskService, result, redactor: Redactor, read_line):
    print(redactor.text(result.analysis_result or ""), flush=True)
    print(f"Plan task_id={result.task_id} version={result.plan_version}", flush=True)
    print(_public_json(redactor, result.plan.model_dump(mode="json"), indent=2), flush=True)
    if result.build_result:
        print("批准包含修改后的 Configure → Build → CTest；将执行目标仓库的 CMake 和测试程序。", flush=True)
        print("BuildSelection=" + _public_json(redactor, result.build_result["selection"], indent=2), flush=True)
    if result.task_diff and result.task_diff["files"]:
        print("已有任务修改保留；本次新增范围尚未授权。TaskDiff=" + _public_json(redactor, result.task_diff), flush=True)
    else:
        print("尚未修改任何源码。", flush=True)
    print("批准格式：approve <版本> / 批准 <版本>；拒绝：reject <版本> / 拒绝 <版本>；取消：cancel / 取消。", flush=True)
    while True:
        try:
            line = await read_line()
            if line == "":
                print("输入已结束，取消审批；没有默认批准。", flush=True)
                return service.cancel_review(result.task_id)
            parts = line.strip().casefold().split()
            if parts in (["cancel"], ["取消"]):
                return service.cancel_review(result.task_id)
            if len(parts) == 2 and parts[0] in {"approve", "批准", "reject", "拒绝"} and parts[1].isascii() and parts[1].isdigit():
                version = int(parts[1]) if len(parts[1]) <= 9 else 0
                if version != result.plan_version:
                    raise ForgePilotError("stale_plan", "Decision must match the displayed plan version")
                action = service.approve if parts[0] in {"approve", "批准"} else service.reject
                return action(result.task_id, version)
            print("输入无效；请明确输入 approve <版本>、reject <版本> 或 cancel。", flush=True)
        except asyncio.CancelledError:
            return service.cancel_review(result.task_id)
        except ForgePilotError as exc:
            print(redactor.text(str(exc)), flush=True)


async def analyze(args: argparse.Namespace, *, transport=None, read_line=None) -> int:
    key = os.environ.get("FORGEPILOT_API_KEY", "")
    redactor = Redactor((key,))
    service = None
    store = None
    trace = None
    rag_manager = None
    try:
        gateway = model_gateway_from_args(args, task_only=True, transport=transport)
        redactor = gateway.redactor.with_secrets((key,))
        rag_runtime = rag_runtime_availability_from_args(args, api_key=key)
        rg = executable(args.rg, "rg")
        git = executable(args.git, "git")
        build_config = None
        if not (args.analysis_only or args.plan_only or args.modify_only):
            build_config = build_config_from_args(args)
        async with gateway:
            llm = gateway.for_route("task")
            try:
                configure_accounting_logging(str(Path(args.database).resolve().with_name("model-accounting.jsonl")))
            except OSError:
                raise ForgePilotError("metrics_storage", "Model accounting file is not writable") from None
            store = SqliteEventStore(args.database, redactor=redactor)
            vector_path = (Path(store.path).resolve().parent / "qdrant"
                           if store.path != ":memory:" else None)
            rag_manager = RagManager(
                rag_runtime, RagFileStore(store), vector_path=vector_path,
                redactor=redactor)
            trace = TaskJournal(store, lambda: service.snapshot(persist_diff=True), redactor, on_event=display_event)
            service = TaskService(llm, rg_executable=rg, git_executable=git,
                                  trace=trace, redactor=redactor, build_config=build_config,
                                  code_intelligence_config=code_intelligence_from_args(args),
                                  mcp_config=mcp_from_args(args), rag_manager=rag_manager)
            reader = read_line or ConsoleInput().read
            run = service.analyze if args.analysis_only else service.plan if args.plan_only else service.start
            result = await run(args.repository_path, args.prompt)
            trace.flush()
            while result.state == TaskState.WAITING_APPROVAL:
                result = await review_plan(service, result, redactor, reader)
                if result.state == TaskState.MODIFYING:
                    result = await service.execute(result.task_id)
                trace.flush()
            print(redactor.text(result.final_result or result.error or "No analysis result"))
            if result.build_result is not None:
                print("BuildResult=" + _public_json(redactor, result.build_result), flush=True)
                print("ValidationHistory=" + _public_json(redactor, result.validation_history), flush=True)
                print("RepairRecords=" + _public_json(redactor, result.repair_records), flush=True)
            if result.task_diff is not None:
                print("TaskDiff=" + _public_json(redactor, service.task_diff(result.task_id)), flush=True)
                if result.task_diff["files"]:
                    print("修改已保留。输入 restore / 恢复 显式恢复，keep / 保留 或 EOF 退出；快照仅在本进程内有效。", flush=True)
                    while True:
                        try:
                            decision = (await reader()).strip().casefold()
                            if decision in {"restore", "恢复"}:
                                restored = await service.restore(result.task_id)
                                trace.flush()
                                print("Restore=" + _public_json(redactor, restored), flush=True)
                                print("TaskDiff=" + _public_json(redactor, service.task_diff(result.task_id)), flush=True)
                                break
                            if decision in {"", "keep", "保留"}:
                                break
                            print("请输入 restore 或 keep；没有自动恢复。", flush=True)
                        except asyncio.CancelledError:
                            print("已保留修改；没有自动恢复。", flush=True)
                            break
                        except ForgePilotError as exc:
                            print(redactor.text(str(exc)), flush=True)
            print(_public_json(redactor, {"state": result.state.value, "steps": result.steps,
                              "tool_calls": result.tool_calls, "usage": result.token_usage,
                              "duration_ms": result.duration_ms, "outcome": result.outcome,
                              "plan_version": result.plan_version,
                              "repair_round": result.repair_round, "failure_kind": result.failure_kind,
                              "approved_plan_version": result.approved_plan_version,
                              "approved_files": sorted(result.approved_files)}))
            return 0 if result.state == TaskState.COMPLETED else 130 if result.state == TaskState.CANCELLED else 1
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 2
    finally:
        if service is not None:
            service.close()
        if trace is not None:
            trace.flush()
        if rag_manager is not None:
            await rag_manager.aclose()
        if store is not None:
            store.close()
        close_accounting_logging()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        # Keep the original environment smoke entrypoint and its exact assertion.
        print("ForgePilot Agent Runtime Ready")
        return 0
    args = parser().parse_args(argv)
    try:
        return asyncio.run(analyze(args))
    except KeyboardInterrupt:
        print("cancelled: Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
