"""Read-only analysis tools and independently checked evidence locations.

MCP grants are application inputs, never fields supplied by a model. A receipt
identifies the redacted tool response actually shown to the Analyst; it does not
assert that an external source is true or that it is a repository file.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID

from ..artifacts.models import EvidenceBundle
from ..errors import ForgePilotError
from ..policy import LEGACY_ALIASES, READ_TOOLS, Principal
from ..tools.registry import ToolRegistry
from ..tools.repository_tools import register_repository_tools
from ..tools.retrieval_tools import register_retrieval_tool
from ..tools.symbol_tools import register_symbol_tools


ANALYST_ROLES = frozenset({"root_cause_analyst", "impact_test_analyst", "generalist"})


@dataclass(frozen=True, slots=True)
class ExternalEvidenceReceipt:
    server_id: str
    path: str
    file_hash: str
    start_line: int
    end_line: int
    symbol: str

    def location(self) -> dict:
        return {"source": "external", "path": self.path,
                "file_hash": self.file_hash, "start_line": self.start_line,
                "end_line": self.end_line, "symbol": self.symbol}


def validate_evidence(bundle: EvidenceBundle, repository, *, external_receipts=()) -> None:
    """Recheck current local bytes and external receipts at publication/approval.

    Local files are opened through Repository's pinned boundary checks, including
    protected files, traversal, links, hard links, binary and UTF-8 restrictions.
    Truncated files cannot supply a complete-file hash and are rejected.
    """
    bundle = EvidenceBundle.model_validate(bundle)
    receipts = tuple(external_receipts)
    snapshots = {}
    for item in bundle.items:
        if (not item.path or not item.file_hash or not item.symbol
                or item.start_line is None or item.end_line is None):
            raise ForgePilotError("evidence_location", "Evidence requires path, lines, symbol and complete file hash")
        if item.source == "external":
            if not any(isinstance(receipt, ExternalEvidenceReceipt)
                       and receipt.path == item.path and receipt.file_hash == item.file_hash
                       and receipt.symbol == item.symbol
                       and receipt.start_line <= item.start_line <= item.end_line <= receipt.end_line
                       for receipt in receipts):
                raise ForgePilotError("evidence_external", "External evidence has no approved tool response receipt")
            continue
        if item.source not in {"local_repository", "rag"}:
            raise ForgePilotError("evidence_source", "Analysts may cite only repository, RAG or receipted external evidence")
        normalized = repository.normalize_plan_path(item.path)
        if normalized not in snapshots:
            snapshots[normalized] = repository._read_snapshot(normalized)
        text, truncated, current_hash = snapshots[normalized]
        if truncated or current_hash is None or current_hash != item.file_hash:
            raise ForgePilotError("evidence_stale", "Evidence file hash is stale or incomplete")
        if not 1 <= item.start_line <= item.end_line <= len(text.splitlines()):
            raise ForgePilotError("evidence_location", "Evidence lines are outside the current source file")


class AnalysisToolRegistry(ToolRegistry):
    """A fresh per-node registry that cannot acquire write/build/Git capabilities."""

    def __init__(self, repository, *, role="root_cause_analyst", approved_server_ids=(),
                 external_mcp_allowed=False, redactor=None):
        if role not in ANALYST_ROLES | {"orchestrator"}:
            raise ForgePilotError("policy_denied", "Only analysis roles can use this registry")
        try:
            servers = frozenset(str(UUID(str(value))) for value in approved_server_ids)
        except (ValueError, TypeError, AttributeError):
            raise ForgePilotError("policy_denied", "MCP approval must name specific server IDs") from None
        if servers and (role not in ANALYST_ROLES or external_mcp_allowed is not True):
            raise ForgePilotError("policy_denied", "External MCP requires an Analyst and explicit task data permission")
        super().__init__(redactor=redactor, principal=Principal(role, bool(servers)))
        self.repository = repository
        self.approved_server_ids = servers
        self._external_receipts: list[ExternalEvidenceReceipt] = []

    @property
    def external_receipts(self) -> tuple[ExternalEvidenceReceipt, ...]:
        return tuple(self._external_receipts)

    def _require_read_only(self, tool):
        if tool is None or tool.read_only is not True:
            raise ForgePilotError("policy_denied", "Analysis tools must be explicitly read-only")
        name = LEGACY_ALIASES.get(tool.name, tool.name)
        if name.startswith("mcp_"):
            if (self.principal.role not in ANALYST_ROLES or tool.mcp_approved is not True
                    or getattr(tool, "server_id", None) not in self.approved_server_ids):
                raise ForgePilotError("policy_denied", "MCP server has not been explicitly approved for this task")
        elif name not in READ_TOOLS:
            raise ForgePilotError("policy_denied", "Analysis tools are restricted to the repository read allowlist")

    def register(self, tool):
        self._require_read_only(tool)
        super().register(tool)

    def validate_call(self, name, arguments, *, capabilities=None):
        # Recheck at dispatch: a selected adapter cannot become writable later.
        self._require_read_only(self.get(name))
        return super().validate_call(name, arguments, capabilities=capabilities)

    async def execute(self, name, arguments):
        result = await super().execute(name, arguments)
        if not result.success or not name.startswith("mcp_"):
            return result
        tool = self.get(name)
        self._require_read_only(tool)
        text = result.output
        # The receipt hashes exactly the text exposed in content, after redaction
        # and bounding. Never preserve the full response or a model's reasoning.
        while True:
            digest = sha256(text.encode("utf-8")).hexdigest()
            receipt = ExternalEvidenceReceipt(
                server_id=tool.server_id,
                path=f"mcp://{tool.server_id}/{tool.name}/{digest}",
                file_hash=digest, start_line=1, end_line=max(1, len(text.splitlines())),
                symbol=tool.remote_tool_name,
            )
            output = json.dumps({"content": text, "content_is_untrusted": True,
                                 "evidence_location": receipt.location(),
                                 "truncated": text != result.output}, ensure_ascii=False)
            if len(output) <= 16_384:
                break
            text = text[:len(text) * 3 // 4]
        self._external_receipts.append(receipt)
        return result.model_copy(update={"output": output})

    def validate_evidence(self, bundle):
        validate_evidence(bundle, self.repository, external_receipts=self.external_receipts)


def build_analysis_registry(repository, *, role="root_cause_analyst", retrieval=None,
                            intelligence=None, approved_mcp=(), approved_server_ids=(),
                            external_mcp_allowed=False, redactor=None) -> AnalysisToolRegistry:
    registry = AnalysisToolRegistry(repository, role=role,
        approved_server_ids=approved_server_ids, external_mcp_allowed=external_mcp_allowed,
        redactor=redactor)
    register_repository_tools(registry, repository)
    if retrieval is not None:
        register_retrieval_tool(registry, retrieval)
    if intelligence is not None:
        register_symbol_tools(registry, intelligence)
    for tool in approved_mcp:
        registry.register_approved_mcp(tool)
    return registry


__all__ = ["AnalysisToolRegistry", "ExternalEvidenceReceipt", "build_analysis_registry", "validate_evidence"]
