"""Compose graph workflows from explicit, trusted, non-owning dependencies."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ForgePilotError

if TYPE_CHECKING:
    from .code_intelligence import CodeIntelligenceConfig
    from .rag.integration import RagManager
    from .agents.delivery import DeliveryWorkflow
    from .mcp import MCPClient
    from .mcp_config import MCPConfig, MCPHttpConfig
    from .mcp_http import MCPHttpClient
    from .models.gateway import ModelGateway
    from .presets import BuildConfig
    from .security import Redactor
    from .sqlite_store import SqliteEventStore


@dataclass(frozen=True)
class MissionDependencies:
    """Borrow shared resources; their owners retain connection/close responsibility."""

    gateway: ModelGateway
    events: SqliteEventStore
    rg_executable: str
    git_executable: str
    build_config: BuildConfig | None
    redactor: Redactor
    mcp_connection_factory: Callable[[MCPConfig | MCPHttpConfig], MCPClient | MCPHttpClient]
    rag_manager: RagManager | None = None
    intelligence_config: CodeIntelligenceConfig | None = None


def build_mission_workflow(dependencies: MissionDependencies, *, oracle_source=None, cloud_model_allowed=False,
                           mcp_snapshots=()) -> DeliveryWorkflow:
    """Lazy composition: OpenAPI and v1 startup need no graph/model/build tools.

    The composition root supplies trusted configuration and a borrowed gateway.
    Graph execution uses the existing policy, workspace and validator
    implementations; HTTP input can never provide executables or commands.
    """
    from .agents.delivery import DeliveryWorkflow
    from .agents.registry import AgentRegistry
    from .artifacts.models import DataPolicy
    from .mcp_manager import MCPManager
    from .process import executable
    from .toolchain import ToolchainProfile
    from .validation import ProtectedOracle, ValidationService
    from .workspace_manager import WorkspaceManager

    gateway = dependencies.gateway
    if gateway is None:
        raise ForgePilotError("mission_unavailable", "Missions require the configured shared ModelGateway")
    try:
        # Validate every built-in role before allocating workspace directories.
        # Incomplete legacy configurations still serve API v1 unchanged.
        AgentRegistry(router=gateway.router)
    except ForgePilotError:
        raise ForgePilotError("llm_config",
            "Missions require configured orchestration, analysis, implementation, review and repair "
            "model routes with Tool Calling support") from None
    config = dependencies.build_config
    events = dependencies.events
    oracle = None
    if oracle_source is not None:
        source = Path(oracle_source)
        if not source.is_absolute() or not source.is_dir():
            raise ForgePilotError("build_config", "Mission oracle must be an absolute trusted directory")
        oracle = ProtectedOracle(source)
    # ArtifactStore has already chosen a safe durable root for this database.
    root = events.artifacts.root / "mission-workspaces"
    workspace = WorkspaceManager(root, git_executable=dependencies.git_executable,
                                 artifacts=events.artifacts)
    profile = ToolchainProfile(
        cmake=config.cmake if config else executable(None, "cmake"),
        ctest=config.ctest if config else executable(None, "ctest"),
    )
    validator = ValidationService(workspace, profile, build_root=root / "validation",
        oracle=oracle, configuration=(config.configuration or "Debug") if config else "Debug",
        generator=config.generator if config else None,
        architecture=config.architecture if config else None,
        make_program=config.make_program if config else None,
        timeout=max(config.configure_timeout, config.build_timeout, config.test_timeout) if config else 300,
        redactor=dependencies.redactor)
    # The host admits exact saved server snapshots. Adapters are handed only to
    # fresh Analyst registries; all other roles have no MCP tool instance.
    mcp_manager = MCPManager(tuple(mcp_snapshots), redactor=dependencies.redactor,
        connection_factory=dependencies.mcp_connection_factory)
    analysis_kwargs = {"approved_mcp": mcp_manager.adapters(),
                       "approved_server_ids": tuple(s.server_id for s in mcp_manager.snapshots)}
    workflow = DeliveryWorkflow(events, workspace, gateway, validator=validator,
                                rg_executable=dependencies.rg_executable,
                                analysis_kwargs=analysis_kwargs)
    workflow.mission_rag_manager = dependencies.rag_manager
    workflow.mission_intelligence_config = dependencies.intelligence_config
    workflow.mission_mcp_manager = mcp_manager
    workflow.mission_build_config = config
    workflow.mission_data_policy = DataPolicy(cloud_model_allowed=cloud_model_allowed,
                                             external_mcp_allowed=bool(mcp_snapshots))
    return workflow
