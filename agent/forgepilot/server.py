"""python -m forgepilot.server: single-process, loopback-only v1 service."""

import asyncio
import os
from pathlib import Path
import sys

import uvicorn

from .api import create_app
from .cli import SafeArgumentParser
from .errors import ForgePilotError
from .llm import OpenAIClientPool
from .process import executable
from .security import Redactor
from .settings import (add_build_arguments, add_connection_arguments, build_config_from_args,
                       add_storage_arguments, models_from_args)
from .sqlite_store import SqliteEventStore
from .task_service import TaskService
from .chat_service import ChatService
from .stream_metrics import close_metrics_logging, configure_metrics_logging
from .settings import add_code_intelligence_arguments, code_intelligence_from_args
from .settings import add_mcp_arguments
from .rag import add_rag_arguments, rag_runtime_from_args


def parser():
    result = SafeArgumentParser(description="ForgePilot local HTTP/SSE runtime (one process, loopback only)")
    result.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    add_connection_arguments(result)
    add_build_arguments(result)
    add_storage_arguments(result)
    add_code_intelligence_arguments(result)
    add_mcp_arguments(result)
    add_rag_arguments(result)
    return result


async def _serve_managed(app, options, stop_requested, on_ready):
    """Private desktop lifecycle hook; no HTTP shutdown or permission bypass."""
    server = uvicorn.Server(uvicorn.Config(app, **options))

    async def watch():
        announced = False
        while True:
            if stop_requested():
                server.should_exit = True
            if server.started and not announced:
                on_ready()
                announced = True
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def main(argv=None, *, stop_requested=None, on_ready=lambda: None):
    args = parser().parse_args(argv)
    key = os.environ.get("FORGEPILOT_API_KEY", "")
    redactor = Redactor((key,))
    try:
        if not args.base_url or not key:
            raise ForgePilotError("llm_config", "Set FORGEPILOT_BASE_URL and FORGEPILOT_API_KEY")
        models = models_from_args(args)
        # Foundation only: validate the disabled-by-default settings and keep any
        # credential runtime-only. Indexing/retrieval is intentionally not wired yet.
        rag_runtime_from_args(args, api_key=key)
        model_clients = OpenAIClientPool(args.base_url, key, timeout=args.timeout)
        if not 1 <= args.port <= 65535:
            raise ForgePilotError("server_config", "Port must be between 1 and 65535")
        def factory(trace):
            # Plain Q&A must start even when development tools are absent/misconfigured.
            rg, git = executable(args.rg, "rg"), executable(args.git, "git")
            config = build_config_from_args(args)
            llm = model_clients.lease(models.task)
            return TaskService(llm, rg_executable=rg, git_executable=git, trace=trace,
                               redactor=redactor, build_config=config,
                               code_intelligence_config=code_intelligence_from_args(args))
        def chat_factory():
            return ChatService(model_clients.lease(models.chat),
                rg_executable=args.rg,git_executable=args.git,redactor=redactor,
                max_tool_concurrency=args.chat_tool_concurrency)
        def summary_factory():
            # Summary generation has no tools, but retains the same redaction boundary.
            return ChatService(model_clients.lease(models.summary),
                rg_executable=args.rg,git_executable=args.git,redactor=redactor)
        # Request-scoped services get non-owning model handles. The shared clients
        # are created and closed on the worker loop with the application lifecycle.
        metrics_file = args.chat_metrics_file or str(
            Path(args.database).resolve().with_name("chat-performance.jsonl"))
        try:
            configure_metrics_logging(metrics_file)
        except OSError:
            raise ForgePilotError("metrics_storage", "Chat performance metrics file is not writable") from None
        try:
            store=SqliteEventStore(args.database,redactor=redactor)
            try:
                app = create_app(factory, store=store, chat_factory=chat_factory,
                                 summary_factory=summary_factory, mcp_disabled=args.disable_mcp,
                                 legacy_mcp_config=args.mcp_config,
                                 model_client_closer=model_clients.aclose)
                options = dict(host=args.host, port=args.port, workers=1,
                               access_log=False, log_level="warning", timeout_graceful_shutdown=10)
                if stop_requested is None:
                    uvicorn.run(app, **options)
                else:
                    asyncio.run(_serve_managed(app, options, stop_requested, on_ready))
            finally:
                store.close()
        finally:
            close_metrics_logging()
        return 0
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
