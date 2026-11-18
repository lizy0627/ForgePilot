"""Trusted service composition; no task lifecycle or transport responsibilities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from typing import TYPE_CHECKING

from .mission_factory import MissionDependencies
from .models.gateway import ModelGateway
from .presets import BuildConfig
from .security import Redactor
from .settings import model_gateway_from_args

if TYPE_CHECKING:
    from .mcp import MCPClient
    from .mcp_config import MCPConfig, MCPHttpConfig
    from .mcp_http import MCPHttpClient
    from .sqlite_store import SqliteEventStore


@dataclass(frozen=True)
class DevelopmentConfig:
    rg_executable: str
    git_executable: str
    build_config: BuildConfig | None


@dataclass(frozen=True)
class ServerResources:
    """Borrowed by v1/v2; the existing server lifecycle closes the gateway.

    Development configuration stays lazy so plain chat and schema construction
    do not require Git, ripgrep, CMake or complete Mission model routes.
    """

    gateway: ModelGateway
    redactor: Redactor
    development: Callable[[], DevelopmentConfig]

    def mission_dependencies(
        self, events: SqliteEventStore, *,
        mcp_connection_factory: Callable[[MCPConfig | MCPHttpConfig], MCPClient | MCPHttpClient],
        rag_manager=None, intelligence_config=None,
    ) -> MissionDependencies:
        config = self.development()
        # Retain the old task-route validation after trusted tool configuration,
        # without constructing a TaskService or creating a provider connection.
        self.gateway.for_route("task")
        return MissionDependencies(
            gateway=self.gateway, events=events,
            rg_executable=config.rg_executable, git_executable=config.git_executable,
            build_config=config.build_config, redactor=self.redactor,
            mcp_connection_factory=mcp_connection_factory,
            rag_manager=rag_manager, intelligence_config=intelligence_config,
        )


def compose_server_resources(
    args, *, api_token: str, development: Callable[[], DevelopmentConfig],
) -> ServerResources:
    """Create one lazy model pool from trusted launch settings, shared by v1/v2."""
    gateway = model_gateway_from_args(args)
    redactor = gateway.redactor.with_secrets((os.environ.get("FORGEPILOT_API_KEY", ""), api_token))
    return ServerResources(gateway=gateway, redactor=redactor, development=development)
