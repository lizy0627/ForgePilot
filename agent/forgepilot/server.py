"""python -m forgepilot.server: single-process, loopback-only v1 service."""

import asyncio
import os
import sys

import uvicorn

from .api import create_app
from .cli import SafeArgumentParser
from .errors import ForgePilotError
from .llm import OpenAICompatibleClient
from .process import executable
from .security import Redactor
from .settings import add_build_arguments, add_connection_arguments, build_config_from_args, add_storage_arguments
from .sqlite_store import SqliteEventStore
from .task_service import TaskService
from .chat_service import ChatService
from .settings import add_code_intelligence_arguments, code_intelligence_from_args
from .settings import add_mcp_arguments


def parser():
    result = SafeArgumentParser(description="ForgePilot local HTTP/SSE runtime (one process, loopback only)")
    result.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    add_connection_arguments(result)
    add_build_arguments(result)
    add_storage_arguments(result)
    add_code_intelligence_arguments(result)
    add_mcp_arguments(result)
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
        if not args.base_url or not args.model or not key:
            raise ForgePilotError("llm_config", "Set FORGEPILOT_BASE_URL, FORGEPILOT_API_KEY and FORGEPILOT_MODEL")
        if not 1 <= args.port <= 65535:
            raise ForgePilotError("server_config", "Port must be between 1 and 65535")
        def factory(trace):
            # Plain Q&A must start even when development tools are absent/misconfigured.
            rg, git = executable(args.rg, "rg"), executable(args.git, "git")
            config = build_config_from_args(args)
            llm = OpenAICompatibleClient(args.base_url, key, args.model, timeout=args.timeout)
            return TaskService(llm, rg_executable=rg, git_executable=git, trace=trace,
                               redactor=redactor, build_config=config,
                               code_intelligence_config=code_intelligence_from_args(args))
        def chat_factory():
            return ChatService(OpenAICompatibleClient(args.base_url,key,args.model,timeout=args.timeout),
                rg_executable=args.rg,git_executable=args.git,redactor=redactor)
        # Each LLM client and TaskService is constructed and closed on the worker loop.
        store=SqliteEventStore(args.database,redactor=redactor)
        try:
            app = create_app(factory, store=store, chat_factory=chat_factory, mcp_disabled=args.disable_mcp,
                             legacy_mcp_config=args.mcp_config)
            options = dict(host=args.host, port=args.port, workers=1,
                           access_log=False, log_level="warning", timeout_graceful_shutdown=10)
            if stop_requested is None:
                uvicorn.run(app, **options)
            else:
                asyncio.run(_serve_managed(app, options, stop_requested, on_ready))
        finally:
            store.close()
        return 0
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
