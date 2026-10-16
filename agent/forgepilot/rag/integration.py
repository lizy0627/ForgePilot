"""Process-owned optional RAG resources and repository-scoped services."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from .config import RagConfig, RagRuntimeAvailability, RagRuntimeSettings
from .embedding import embedding_provider_from_runtime
from .indexer import RepositoryIndexer
from .retrieval import RetrievalService
from .store import RagFileStore
from .vector_store import QdrantVectorStore


def repository_id_for(root: Path) -> str:
    """Derive a stable opaque identity without exposing the absolute path."""
    normalized = os.path.normcase(os.path.normpath(str(root.resolve())))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RagRepositoryServices:
    indexer: RepositoryIndexer
    retrieval: RetrievalService


class RagManager:
    """Share optional embedding/Qdrant resources across request-scoped tasks."""

    def __init__(self, runtime: RagRuntimeAvailability | RagRuntimeSettings | None,
                 file_store: RagFileStore, *, embedding_provider=None, vector_store=None,
                 vector_path: str | Path | None = None) -> None:
        settings = runtime.settings if isinstance(runtime, RagRuntimeAvailability) else runtime
        self.config = (settings.config if settings is not None else
                       runtime.config if isinstance(runtime, RagRuntimeAvailability)
                       and runtime.config is not None else RagConfig())
        self.file_store = file_store
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self._active_indexes: set[str] = set()
        self.reason = (runtime.reason if isinstance(runtime, RagRuntimeAvailability)
                       and runtime.settings is None else None)
        if not self.config.enabled:
            return
        if self.embedding_provider is None:
            availability = embedding_provider_from_runtime(
                runtime if isinstance(runtime, RagRuntimeAvailability) else settings)
            self.embedding_provider = availability.provider
            if not availability.available:
                self.reason = availability.reason or "initialization_failed"
        if self.vector_store is None:
            try:
                self.vector_store = QdrantVectorStore(vector_path)
            except Exception:
                # Literal/clangd retrieval remains useful even if Local Mode
                # Qdrant cannot be opened.
                self.reason = "vector_unavailable"

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def index_available(self) -> bool:
        return self.embedding_provider is not None and self.vector_store is not None

    def for_repository(self, repository, *, code_intelligence=None) -> RagRepositoryServices | None:
        if not self.enabled:
            return None
        repository_id = repository_id_for(repository.root)
        indexer = RepositoryIndexer(
            repository_id, repository, self.config, self.embedding_provider,
            self.vector_store, self.file_store)
        retrieval = RetrievalService(
            repository_id, repository, self.config, self.embedding_provider,
            self.vector_store, code_intelligence=code_intelligence,
            file_store=self.file_store, stale_handler=indexer.schedule_refresh)
        return RagRepositoryServices(indexer=indexer, retrieval=retrieval)

    def begin_index(self, repository_id: str) -> bool:
        """Claim one repository on the shared runtime loop without blocking Agent work."""
        if repository_id in self._active_indexes:
            return False
        self._active_indexes.add(repository_id)
        return True

    def finish_index(self, repository_id: str) -> None:
        self._active_indexes.discard(repository_id)

    async def aclose(self) -> None:
        for resource in (self.embedding_provider, self.vector_store):
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
