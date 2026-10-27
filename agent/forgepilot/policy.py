"""Program-owned, deny-by-default capability policy.

Repository text, model tool arguments and MCP descriptions never create grants.
Only trusted callers select principals and explicitly approved MCP adapters.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from .errors import ForgePilotError


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    VALIDATOR = "validator"
    WORKSPACE_MANAGER = "workspace_manager"


class Capability(StrEnum):
    READ = "repository.read"
    SEARCH = "repository.search"
    SYMBOLS = "repository.symbols"
    REFERENCES = "repository.references"
    RAG = "repository.rag"
    PATCH = "repository.patch"
    VALIDATION_READ = "validation.read"
    MCP_READ = "mcp.read"
    BUILD = "validation.build"
    TEST = "validation.test"
    COMMIT = "workspace.commit"
    DELIVER = "workspace.deliver"


_READ = frozenset({Capability.READ, Capability.SEARCH, Capability.SYMBOLS,
                   Capability.REFERENCES, Capability.VALIDATION_READ})
ROLE_CAPABILITIES = MappingProxyType({
    Role.ORCHESTRATOR: frozenset({Capability.READ, Capability.SEARCH, Capability.RAG}),
    Role.ANALYST: _READ | {Capability.RAG, Capability.MCP_READ},
    Role.IMPLEMENTER: _READ | {Capability.RAG, Capability.PATCH},
    Role.REVIEWER: _READ | {Capability.RAG},
    Role.VALIDATOR: _READ | {Capability.BUILD, Capability.TEST},
    Role.WORKSPACE_MANAGER: frozenset({Capability.READ, Capability.COMMIT, Capability.DELIVER}),
})

READ_TOOLS = MappingProxyType({
    "repository_list_files": Capability.READ,
    "repository_read_range": Capability.READ,
    "repository_search_text": Capability.SEARCH,
    "repository_get_symbol": Capability.SYMBOLS,
    "repository_find_references": Capability.REFERENCES,
    "repository_read_cmake_target": Capability.READ,
    "repository_git_diff": Capability.READ,
    "rag_retrieve": Capability.RAG,
    "artifact_read": Capability.READ,
})
WRITE_TOOLS = MappingProxyType({
    "repository_apply_patch": Capability.PATCH,
    "repository_create_file": Capability.PATCH,
    "repository_delete_new_file": Capability.PATCH,
})
VALIDATOR_TOOLS = MappingProxyType({
    "cmake_configure": Capability.BUILD, "cmake_build": Capability.BUILD,
    "run_tests": Capability.TEST,
})
TOOL_CAPABILITIES = MappingProxyType(dict(READ_TOOLS) | dict(WRITE_TOOLS) | dict(VALIDATOR_TOOLS))
# Compatibility is dispatch-only. These names are never advertised to a model.
LEGACY_ALIASES = MappingProxyType({
    "list_files": "repository_list_files", "read_file": "repository_read_range",
    "search_code": "repository_search_text", "find_symbol": "repository_get_symbol",
    "find_references": "repository_find_references", "retrieve_code_context": "rag_retrieve",
    "git_diff": "repository_git_diff", "git_status": "repository_git_diff",
    "write_file": "repository_create_file", "edit_file": "repository_apply_patch",
})
FORBIDDEN_TOOLS = frozenset({
    "arbitrary_shell", "shell", "raw_powershell", "powershell", "raw_cmd", "cmd",
    "unrestricted_python", "python", "git_commit", "git_push", "commit", "push",
})


def normalize_role(role: str | Role) -> Role | None:
    aliases = {"root_cause_analyst": Role.ANALYST, "impact_test_analyst": Role.ANALYST,
               "repair": Role.IMPLEMENTER}
    try:
        return aliases.get(role) or Role(role)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class Principal:
    role: Role | str
    # Both values originate in the trusted application, never tool arguments.
    mcp_approved: bool = False


class PolicyEngine:
    def permits(self, principal: Principal, capability: Capability | str, *,
                mcp_read_only: bool = False) -> bool:
        role = normalize_role(principal.role)
        try:
            capability = Capability(capability)
        except (ValueError, TypeError):
            return False
        if role is None or capability not in ROLE_CAPABILITIES.get(role, frozenset()):
            return False
        if capability == Capability.MCP_READ:
            return principal.mcp_approved is True and mcp_read_only is True
        return True

    def require(self, principal: Principal, capability: Capability | str, **kwargs) -> None:
        if not self.permits(principal, capability, **kwargs):
            raise ForgePilotError("policy_denied", "Role does not hold the required approved capability")

    def require_tool(self, principal: Principal, name: str, *,
                     mcp_approved: bool = False, mcp_read_only: bool = False) -> None:
        canonical = LEGACY_ALIASES.get(name, name)
        capability = TOOL_CAPABILITIES.get(canonical)
        if capability is None:
            if (name.casefold() in FORBIDDEN_TOOLS or not name.startswith("mcp_")
                    or not mcp_approved):
                raise ForgePilotError("policy_denied", "Tool is outside the reviewed allowlist")
            capability = Capability.MCP_READ
        self.require(principal, capability, mcp_read_only=mcp_read_only)
