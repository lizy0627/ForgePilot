"""Trusted launch configuration shared by CLI and the local HTTP entrypoint."""

import os
from pathlib import Path

from .presets import BuildConfig
from .process import executable
from .sqlite_store import database_path
from .code_intelligence import CodeIntelligenceConfig
from .mcp_config import load_mcp_config


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


def add_connection_arguments(parser):
    parser.add_argument("--base-url", default=os.environ.get("FORGEPILOT_BASE_URL"))
    parser.add_argument("--model", default=os.environ.get("FORGEPILOT_MODEL"))
    parser.add_argument("--rg", default=os.environ.get("FORGEPILOT_RG"), help="Absolute rg executable path")
    parser.add_argument("--git", default=os.environ.get("FORGEPILOT_GIT"), help="Absolute git executable path")
    parser.add_argument("--timeout", type=float, default=60.0, help="LLM wall timeout, seconds (0, 600]")


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
