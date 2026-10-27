"""Compose graph workflows from the same host-owned services used by API v1."""
from pathlib import Path

from .errors import ForgePilotError


def build_mission_workflow(task_host, *, oracle_source=None, cloud_model_allowed=False):
    """Lazy composition: OpenAPI and v1 startup need no graph/model/build tools.

    The compatibility TaskService supplies configuration and a non-owning gateway
    handle. Graph execution uses the existing policy, workspace and validator
    implementations; HTTP input can never provide executables or commands.
    """
    from .agents.delivery import DeliveryWorkflow
    from .agents.registry import AgentRegistry
    from .artifacts.models import DataPolicy
    from .process import executable
    from .toolchain import ToolchainProfile
    from .trace import MemoryTrace
    from .validation import ProtectedOracle, ValidationService
    from .workspace_manager import WorkspaceManager

    legacy = task_host.factory(MemoryTrace())
    gateway = getattr(getattr(legacy, "llm", None), "gateway", None)
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
    config = legacy.build_config
    if config is not None and any((config.configure_preset, config.build_preset, config.test_preset)):
        raise ForgePilotError("build_config",
            "Mission validation does not support CMake presets; configure generator, architecture, "
            "make program and configuration instead")
    events = task_host.store
    oracle = None
    if oracle_source is not None:
        source = Path(oracle_source)
        if not source.is_absolute() or not source.is_dir():
            raise ForgePilotError("build_config", "Mission oracle must be an absolute trusted directory")
        oracle = ProtectedOracle(source)
    # ArtifactStore has already chosen a safe durable root for this database.
    root = events.artifacts.root / "mission-workspaces"
    workspace = WorkspaceManager(root, git_executable=legacy.git_executable,
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
        redactor=legacy.redactor)
    workflow = DeliveryWorkflow(events, workspace, gateway, validator=validator,
                                rg_executable=legacy.rg_executable)
    workflow.mission_data_policy = DataPolicy(cloud_model_allowed=cloud_model_allowed)
    return workflow
