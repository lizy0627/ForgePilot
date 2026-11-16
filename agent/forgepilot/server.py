"""python -m forgepilot.server: single-process, loopback-only v1 service."""

import asyncio
import os
import secrets
from pathlib import Path
import sys

import uvicorn

from .api import create_app
from .cli import SafeArgumentParser
from .errors import ForgePilotError
from .models.accounting import close_accounting_logging, configure_accounting_logging
from .process import executable
from .security import Redactor
from .settings import (add_build_arguments, add_connection_arguments, build_config_from_args,
                       add_storage_arguments)
from .service_composition import DevelopmentConfig, compose_server_resources
from .sqlite_store import SqliteEventStore
from .task_service import TaskService
from .repository import Repository
from .conversations import ChatService
from .stream_metrics import close_metrics_logging, configure_metrics_logging
from .settings import add_code_intelligence_arguments, code_intelligence_from_args
from .settings import add_mcp_arguments
from .rag import (RagFileStore, RagIndexCoordinator, RagManager, add_rag_arguments,
                  rag_runtime_availability_from_args)


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
    result.add_argument("--mission-oracle", help="Trusted external protected-test CMake project for API v2 missions")
    result.add_argument("--mission-allow-cloud-model", action="store_true",
                        help="Explicitly allow mission repository context to configured cloud model routes")
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
    local_token = os.environ.get("FORGEPILOT_LOCAL_API_TOKEN") or secrets.token_urlsafe(32)
    redactor = Redactor((key, local_token))
    try:
        def development_config():
            # Resolve only when v1 work or a Mission needs development tools.
            rg, git = executable(args.rg, "rg"), executable(args.git, "git")
            return DevelopmentConfig(rg, git, build_config_from_args(args))

        resources = compose_server_resources(args, api_token=local_token,
                                             development=development_config)
        model_clients = resources.gateway
        redactor = resources.redactor
        rag_runtime = rag_runtime_availability_from_args(args, api_key=key)
        if not 1 <= args.port <= 65535:
            raise ForgePilotError("server_config", "Port must be between 1 and 65535")
        rag_manager = None
        def factory(trace):
            # Plain Q&A must start even when development tools are absent/misconfigured.
            config = resources.development()
            llm = model_clients.for_route("task")
            return TaskService(llm, rg_executable=config.rg_executable,
                               git_executable=config.git_executable, trace=trace,
                               redactor=redactor, build_config=config.build_config,
                               code_intelligence_config=code_intelligence_from_args(args),
                               rag_manager=rag_manager)
        def chat_factory():
            return ChatService(model_clients.for_route("chat"),
                rg_executable=args.rg,git_executable=args.git,redactor=redactor,
                max_tool_concurrency=args.chat_tool_concurrency)
        def summary_factory():
            # Summary generation has no tools, but retains the same redaction boundary.
            return ChatService(model_clients.for_route("summary"),
                rg_executable=args.rg,git_executable=args.git,redactor=redactor)
        # Request-scoped services get non-owning model handles. The shared clients
        # are created and closed on the worker loop with the application lifecycle.
        metrics_file = args.chat_metrics_file or str(
            Path(args.database).resolve().with_name("chat-performance.jsonl"))
        try:
            configure_metrics_logging(metrics_file)
            configure_accounting_logging(str(Path(args.database).resolve().with_name("model-accounting.jsonl")))
        except OSError:
            close_metrics_logging()
            close_accounting_logging()
            raise ForgePilotError("metrics_storage", "Model accounting or chat metrics file is not writable") from None
        try:
            store=SqliteEventStore(args.database,redactor=redactor)
            try:
                vector_path = (Path(store.path).resolve().parent / "qdrant"
                               if store.path != ":memory:" else None)
                rag_manager = RagManager(
                    rag_runtime, RagFileStore(store), vector_path=vector_path,
                    redactor=redactor)
                async def open_rag_repository(repository_path):
                    return await Repository.open(
                        repository_path,
                        # Repository indexing uses the existing bounded scanner;
                        # resolving optional rg here would make disabled RAG
                        # status fail before it can report "disabled".
                        rg_executable=args.rg or "rg",
                        git_executable=executable(args.git, "git"),
                    )
                rag_coordinator = RagIndexCoordinator(rag_manager, open_rag_repository)
                async def close_resources():
                    await rag_manager.aclose()
                    await model_clients.aclose()
                app = create_app(factory, store=store, chat_factory=chat_factory,
                                 summary_factory=summary_factory, mcp_disabled=args.disable_mcp,
                                 legacy_mcp_config=args.mcp_config,
                                 model_client_closer=close_resources,
                                 rag_coordinator=rag_coordinator, api_token=local_token)
                from .mission_factory import build_mission_workflow
                app.state.mission_host.factory = lambda events, *, mcp_snapshots=(): build_mission_workflow(
                    resources.mission_dependencies(events,
                        mcp_connection_factory=app.state.task_host.mcp_service.connection_factory,
                        rag_manager=rag_manager, intelligence_config=code_intelligence_from_args(args)),
                    oracle_source=args.mission_oracle,
                    cloud_model_allowed=args.mission_allow_cloud_model,
                    mcp_snapshots=mcp_snapshots)
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
            close_accounting_logging()
        return 0
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
