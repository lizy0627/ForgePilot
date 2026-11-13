"""Process-owned optional RAG resources and repository-scoped services."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from .config import RagConfig, RagRuntimeAvailability, RagRuntimeSettings
from .embedding import embedding_provider_from_runtime
from .indexer import RepositoryIndexer, _RepositoryIndexState
from .retrieval import RetrievalService
from .store import RagFileStore
from .vector_store import QdrantVectorStore
from ..security import Redactor


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
                 vector_path: str | Path | None = None,
                 redactor: Redactor | None = None) -> None:
        settings = runtime.settings if isinstance(runtime, RagRuntimeAvailability) else runtime
        self.config = (settings.config if settings is not None else
                       runtime.config if isinstance(runtime, RagRuntimeAvailability)
                       and runtime.config is not None else RagConfig())
        self.file_store = file_store
        parent_redactor = redactor or Redactor()
        self.redactor = (parent_redactor.with_secrets((settings.embedding_api_key,))
                         if settings is not None else parent_redactor)
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self._active_indexes: set[str] = set()
        self._index_states: dict[str, _RepositoryIndexState] = {}
        self._closed = False
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
            self.vector_store, self.file_store, redactor=self.redactor,
            index_state=self._index_state(repository_id))
        retrieval = RetrievalService(
            repository_id, repository, self.config, self.embedding_provider,
            self.vector_store, code_intelligence=code_intelligence,
            file_store=self.file_store, stale_handler=indexer.schedule_refresh,
            redactor=self.redactor)
        return RagRepositoryServices(indexer=indexer, retrieval=retrieval)

    def _index_state(self, repository_id: str) -> _RepositoryIndexState:
        state = self._index_states.get(repository_id)
        if state is None:
            state = _RepositoryIndexState(refresh_enabled=not self._closed)
            self._index_states[repository_id] = state
        return state

    def begin_index(self, repository_id: str) -> bool:
        """Claim one repository on the shared runtime loop without blocking Agent work."""
        if self._closed or repository_id in self._active_indexes:
            return False
        self._active_indexes.add(repository_id)
        self._index_state(repository_id).refresh_enabled = False
        return True

    def finish_index(self, repository_id: str) -> None:
        self._active_indexes.discard(repository_id)
        self._index_state(repository_id).refresh_enabled = not self._closed

    async def cancel_refreshes(self, repository_id: str) -> None:
        """Join refreshes after the repository claim has stopped new registration."""
        tasks = list(self._index_state(repository_id).refresh_tasks.values())
        for task in tasks:
            # Clear and shutdown may join the same shielded vector mutation.
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for state in self._index_states.values():
            state.refresh_enabled = False
        await asyncio.gather(*(self.cancel_refreshes(repository_id)
                               for repository_id in self._index_states))
        for resource in (self.embedding_provider, self.vector_store):
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
