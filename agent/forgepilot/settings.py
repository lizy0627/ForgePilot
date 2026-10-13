"""Trusted launch configuration shared by CLI and the local HTTP entrypoint."""

import os
from dataclasses import dataclass
from pathlib import Path

from .presets import BuildConfig
from .process import executable
from .sqlite_store import database_path
from .code_intelligence import CodeIntelligenceConfig
from .mcp_config import load_mcp_config
from .errors import ForgePilotError


@dataclass(frozen=True)
class ModelSelection:
    chat: str
    task: str
    summary: str


def checked_model_id(value, purpose):
    """Validate a model identifier without ever including it in an error."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ForgePilotError("llm_config", f"Invalid {purpose} model configuration")
    value = value.strip()
    if not value:
        return None
    if len(value) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ForgePilotError("llm_config", f"Invalid {purpose} model configuration")
    return value


def resolve_models(*, model=None, chat_model=None, task_model=None, summary_model=None):
    """Resolve purpose-specific models while preserving FORGEPILOT_MODEL fallback."""
    fallback = checked_model_id(model, "fallback")
    chat = checked_model_id(chat_model, "chat") or fallback
    task = checked_model_id(task_model, "task") or fallback
    summary = checked_model_id(summary_model, "summary") or chat or fallback
    missing = [name for name, value in (("chat", chat), ("task", task), ("summary", summary)) if not value]
    if missing:
        raise ForgePilotError("llm_config", "Missing model configuration for: " + ", ".join(missing))
    return ModelSelection(chat=chat, task=task, summary=summary)


def models_from_args(args):
    return resolve_models(model=args.model, chat_model=args.chat_model,
                          task_model=args.task_model, summary_model=args.summary_model)


def task_model_from_args(args):
    model = checked_model_id(args.task_model, "task") or checked_model_id(args.model, "fallback")
    if not model:
        raise ForgePilotError("llm_config", "Missing model configuration for: task")
    return model


def add_mcp_arguments(parser):
    parser.add_argument("--mcp-config", default=os.environ.get("FORGEPILOT_MCP_CONFIG"),
                        help="Explicit absolute trusted single-server JSON configuration; disabled by default")
    parser.add_argument("--disable-mcp", action="store_true", help="Disable MCP even if a configuration is set")


def mcp_from_args(args):
    return None if args.disable_mcp else load_mcp_config(args.mcp_config)


def add_code_intelligence_arguments(parser):
    parser.add_argument("--clangd", default=os.environ.get("FORGEPILOT_CLANGD"), help="Trusted absolute clangd executable; unavailable falls back to rg")
    parser.add_argument("--compile-commands-dir", default=os.environ.get("FORGEPILOT_COMPILE_COMMANDS_DIR"),
                        help="Existing compilation database directory inside the target repository; never a model argument")


def code_intelligence_from_args(args):
    return CodeIntelligenceConfig(clangd=args.clangd, compile_commands_dir=args.compile_commands_dir)


def add_storage_arguments(parser):
    parser.add_argument("--database", default=str(database_path()), help="Local SQLite task history path (one writer process)")
    parser.add_argument("--chat-metrics-file", default=os.environ.get("FORGEPILOT_CHAT_METRICS_FILE"),
                        help="Content-free chat performance JSONL (default: beside the database)")


def add_connection_arguments(parser):
    parser.add_argument("--base-url", default=os.environ.get("FORGEPILOT_BASE_URL"))
    parser.add_argument("--model", default=os.environ.get("FORGEPILOT_MODEL"))
    parser.add_argument("--chat-model", default=os.environ.get("FORGEPILOT_CHAT_MODEL"))
    parser.add_argument("--task-model", default=os.environ.get("FORGEPILOT_TASK_MODEL"))
    parser.add_argument("--summary-model", default=os.environ.get("FORGEPILOT_SUMMARY_MODEL"))
    parser.add_argument("--rg", default=os.environ.get("FORGEPILOT_RG"), help="Absolute rg executable path")
    parser.add_argument("--git", default=os.environ.get("FORGEPILOT_GIT"), help="Absolute git executable path")
    parser.add_argument("--timeout", type=float, default=60.0, help="LLM wall timeout, seconds (0, 600]")
    parser.add_argument("--chat-tool-concurrency", type=int, choices=range(1, 9),
                        default=os.environ.get("FORGEPILOT_CHAT_TOOL_CONCURRENCY", "3"),
                        help="Maximum concurrent explicitly safe read-only chat tools (1-8)")


def add_build_arguments(parser):
    parser.add_argument("--cmake", default=os.environ.get("FORGEPILOT_CMAKE"), help="Absolute CMake executable")
    parser.add_argument("--ctest", default=os.environ.get("FORGEPILOT_CTEST"), help="Absolute CTest executable (default: sibling of CMake)")
    for name in ("configure-preset", "build-preset", "test-preset", "generator", "architecture", "make-program", "configuration"):
        parser.add_argument("--" + name, help="Trusted build selection; never supplied by the model")
    for phase, seconds in (("configure", 120), ("build", 300), ("test", 120)):
        parser.add_argument(f"--{phase}-timeout", type=float, default=seconds, help="Process tree wall timeout, seconds (0, 3600]")


def build_config_from_args(args):
    cmake = executable(args.cmake, "cmake")
    sibling = Path(cmake).with_name("ctest.exe")
    ctest = executable(args.ctest or (str(sibling) if sibling.is_file() else None), "ctest")
    return BuildConfig(cmake=cmake, ctest=ctest, **{name: getattr(args, name) for name in (
        "configure_preset", "build_preset", "test_preset", "generator", "architecture", "make_program",
        "configuration", "configure_timeout", "build_timeout", "test_timeout")})
