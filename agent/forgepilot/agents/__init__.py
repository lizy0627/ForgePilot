"""Versioned Agent contracts; read-only execution is in agents.analysis."""
from .definitions import (
    AGENT_SCHEMA_VERSION, ROLE_CAPABILITIES, ROLE_ROUTES, AgentDefinition, AgentLimits,
)
from .registry import AgentRegistry, BUILTIN_AGENT_DEFINITIONS

__all__ = [
    "AGENT_SCHEMA_VERSION", "ROLE_CAPABILITIES", "ROLE_ROUTES", "AgentDefinition",
    "AgentLimits", "AgentRegistry", "BUILTIN_AGENT_DEFINITIONS",
]
