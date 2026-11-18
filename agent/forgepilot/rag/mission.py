"""Read-only use of a source repository's index against Mission worktree bytes.

The source index supplies candidates only. RetrievalService opens every candidate
through the task Repository and returns its current hash, line range and content;
literal search always runs in that same worktree, including unindexed files.
There is deliberately no indexer or stale-refresh handler in this adapter.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..artifacts.models import DataPolicy
from ..repository import Repository
from .config import RagConfig
from .embedding import EmbeddingProvider
from .integration import repository_id_for
from .models import RetrievalQuery, RetrievalResult, RetrievalTrace
from .retrieval import RetrievalMode, RetrievalService, RetrievalSourceOutcome
from .vector_store import VectorStore

if TYPE_CHECKING:
    from .integration import RagManager


def _local_embedding_endpoint(base_url: str) -> bool:
    host = urlsplit(base_url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class MissionRetrievalService(RetrievalService):
    """Borrow search resources without granting index writes or cloud permission."""

    def __init__(self, repository_id: str, repository: Repository, config: RagConfig,
                 embedding_provider: EmbeddingProvider | None,
                 vector_store: VectorStore | None, *, data_policy: DataPolicy,
                 code_intelligence=None, file_store=None, redactor=None,
                 unavailable_reason: str | None = None,
                 on_status: Callable[[dict], None] | None = None) -> None:
        # Disabled/unavailable vector resources do not disable worktree text
        # fallback. The public status retains the actual subsystem state.
        super().__init__(repository_id, repository, config.model_copy(update={"enabled": True}), embedding_provider,
            vector_store, code_intelligence=code_intelligence,
            file_store=file_store, redactor=redactor)
        self.data_policy = DataPolicy.model_validate(data_policy)
        self._unavailable_reason = unavailable_reason
        self._on_status = on_status
        self._mission_trace: RetrievalTrace | None = None
        self._used = False

    def _embedding_denied_reason(self) -> str | None:
        if self._unavailable_reason is not None:
            return self._unavailable_reason
        if not self.config.embedding_base_url or not self.config.embedding_model:
            return "embedding_not_configured"
        if (not self.data_policy.cloud_model_allowed
                and not _local_embedding_endpoint(self.config.embedding_base_url)):
            return "embedding_policy_denied"
        return None

    def _snapshot(self, path, snapshots):
        value = super()._snapshot(path, snapshots)
        if value is not None and len(value[0].encode("utf-8")) > self.config.max_file_bytes:
            return None
        return value

    async def _vector(self, task_text, snapshots):
        reason = self._embedding_denied_reason()
        if reason is not None:
            return RetrievalSourceOutcome("vector", "unavailable", reason=reason)
        return await super()._vector(task_text, snapshots)

    async def retrieve(self, task: str | RetrievalQuery, *,
                       planned_files: Sequence[str] = (), path_filters: Sequence[str] = (),
                       languages: Sequence[str] = (),
                       mode: RetrievalMode = "hybrid") -> list[RetrievalResult]:
        results = await super().retrieve(task, planned_files=planned_files,
            path_filters=path_filters, languages=languages, mode=mode)
        trace = self.last_trace
        if trace is not None:
            # A mismatch in this worktree is never permission to rewrite the
            # source repository's shared index with task/branch-specific bytes.
            events = tuple("mission_index_read_only" if item == "incremental_update_unavailable"
                           else item for item in trace.events)
            trace = trace.model_copy(update={"events": events})
            self._trace.set(trace)
            self._mission_trace = trace
        self._used = self._used or bool(results)
        if self._on_status is not None:
            self._on_status(self.status())
        return results

    def status(self) -> dict:
        """Content-free diagnostics available outside the querying asyncio task."""
        trace = self._mission_trace
        reasons = list(trace.events) if trace is not None else []
        if trace is None and (reason := self._embedding_denied_reason()):
            reasons.append(reason)
        return {"requested": True, "enabled": self._unavailable_reason is None, "used": self._used,
                "fallback_reasons": reasons,
                "trace": trace.model_dump(mode="json") if trace is not None else None}


def build_mission_retrieval(manager: RagManager | None, source_repository_root: str | Path,
                            repository: Repository, *, data_policy: DataPolicy,
                            code_intelligence=None,
                            on_status: Callable[[dict], None] | None = None) -> MissionRetrievalService:
    """Bind the source index identity to a task Repository without creating it.

    Call only for a Mission which requested RAG. The caller owns the shared
    manager; disabled/unavailable resources preserve task text fallback.
    """
    reason = "rag_unavailable" if manager is None else "rag_disabled" if not manager.enabled else None
    return MissionRetrievalService(
        repository_id_for(Path(source_repository_root)), repository,
        manager.config if manager is not None else RagConfig(),
        manager.embedding_provider if manager is not None else None,
        manager.vector_store if manager is not None else None,
        data_policy=data_policy, code_intelligence=code_intelligence,
        file_store=manager.file_store if manager is not None else None,
        redactor=manager.redactor if manager is not None else None,
        unavailable_reason=reason, on_status=on_status)
