"""Embedded Qdrant Local Mode adapter; no server or indexing lifecycle."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
import math
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid5

try:
    from qdrant_client import QdrantClient, models as qmodels
except Exception:  # An optional disabled subsystem must not break Agent imports/startup.
    QdrantClient = None
    qmodels = None

from ..errors import ForgePilotError
from .models import (CodeChunk, RetrievedChunk, checked_relative_path,
                     checked_repository_id)


_POINT_NAMESPACE = UUID("2a4c5687-c7a5-4a71-bd63-9f6e7217d906")


def vector_store_path() -> Path:
    """Keep vectors beside the established application database, never in a repository."""
    # Local import avoids making SqliteEventStore's schema import depend on this
    # optional adapter while the rag package is being initialized.
    from ..sqlite_store import database_path
    return database_path().parent / "qdrant"


@runtime_checkable
class VectorStore(Protocol):
    async def upsert_chunks(self, chunks: Sequence[CodeChunk],
                            vectors: Sequence[Sequence[float]]) -> int: ...
    async def search(self, repository_id: str, query_vector: Sequence[float], *,
                     limit: int = 8) -> list[RetrievedChunk]: ...
    async def delete_file(self, repository_id: str, path: str) -> int: ...
    async def clear_repository(self, repository_id: str) -> int: ...
    async def count(self, repository_id: str) -> int: ...


class QdrantVectorStore:
    """One cosine collection shared safely by repository-scoped payload filters."""

    COLLECTION = "forgepilot_code_chunks"

    def __init__(self, path: str | Path | None = None, *, collection_name: str = COLLECTION) -> None:
        if not isinstance(collection_name, str) or not collection_name.strip() or len(collection_name) > 255:
            raise ForgePilotError("rag_vector_config", "Invalid vector collection name")
        if QdrantClient is None or qmodels is None:
            raise ForgePilotError("rag_vector_unavailable",
                                  "Embedded vector store support is unavailable")
        configured = vector_store_path() if path is None else Path(path)
        try:
            resolved = configured.expanduser().resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            # Calls are serialized below, but asyncio.to_thread may select a
            # different worker for each call, so Qdrant's SQLite thread check
            # must not bind the client to its constructor thread.
            self._client = QdrantClient(
                path=str(resolved), force_disable_check_same_thread=True)
        except (OSError, ValueError, RuntimeError):
            raise ForgePilotError("rag_vector_unavailable",
                                  "Cannot open the embedded vector store") from None
        self.path = resolved
        self.collection_name = collection_name
        self._lock = asyncio.Lock()
        self._thread_lock = Lock()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ForgePilotError("rag_vector_unavailable", "Embedded vector store is closed")

    @staticmethod
    def _vector(vector: Sequence[float]) -> list[float]:
        if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence) or not vector:
            raise ForgePilotError("rag_vector_input", "Vector must be a nonempty numeric sequence")
        checked = []
        for value in vector:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ForgePilotError("rag_vector_input", "Vector values must be finite numbers")
            checked.append(float(value))
        return checked

    @staticmethod
    def _repository_filter(repository_id: str) -> qmodels.Filter:
        return qmodels.Filter(must=[qmodels.FieldCondition(
            key="repository_id", match=qmodels.MatchValue(value=repository_id))])

    @classmethod
    def _file_filter(cls, repository_id: str, path: str) -> qmodels.Filter:
        return qmodels.Filter(must=[
            qmodels.FieldCondition(key="repository_id",
                                   match=qmodels.MatchValue(value=repository_id)),
            qmodels.FieldCondition(key="path", match=qmodels.MatchValue(value=path)),
        ])

    def _collection_dimension(self) -> int | None:
        if not self._client.collection_exists(self.collection_name):
            return None
        vectors = self._client.get_collection(self.collection_name).config.params.vectors
        if not isinstance(vectors, qmodels.VectorParams):
            raise ForgePilotError("rag_index_rebuild_required",
                                  "Vector collection layout changed; rebuild the RAG index")
        if vectors.distance != qmodels.Distance.COSINE:
            raise ForgePilotError("rag_index_rebuild_required",
                                  "Vector distance is not cosine; rebuild the RAG index")
        return vectors.size

    def _ensure_dimension(self, dimension: int, *, create: bool) -> bool:
        existing = self._collection_dimension()
        if existing is None:
            if not create:
                return False
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(
                    size=dimension, distance=qmodels.Distance.COSINE),
            )
            return True
        if existing != dimension:
            raise ForgePilotError(
                "rag_index_rebuild_required",
                "Embedding dimension does not match stored vectors; rebuild the RAG index",
            )
        return True

    @staticmethod
    def _payload(chunk: CodeChunk) -> dict:
        return {
            "repository_id": chunk.repository_id,
            "path": chunk.path,
            "language": chunk.language,
            "symbol": chunk.symbol,
            "symbol_type": chunk.symbol_type,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "file_hash": chunk.file_hash,
            "content": chunk.content,
        }

    @staticmethod
    def _chunk(payload: object) -> CodeChunk:
        if not isinstance(payload, dict):
            raise ValueError("Missing vector payload")
        return CodeChunk(
            repository_id=payload["repository_id"],
            path=payload["path"],
            language=payload["language"],
            symbol=payload.get("symbol"),
            symbol_type=payload.get("symbol_type"),
            start_line=payload["start_line"],
            end_line=payload["end_line"],
            file_hash=payload["file_hash"],
            content=payload["content"],
        )

    def _upsert_chunks(self, chunks: list[CodeChunk], vectors: list[list[float]]) -> int:
        self._ensure_dimension(len(vectors[0]), create=True)
        points = [qmodels.PointStruct(
            id=str(uuid5(_POINT_NAMESPACE, chunk.chunk_id)),
            vector=vector,
            payload=self._payload(chunk),
        ) for chunk, vector in zip(chunks, vectors, strict=True)]
        self._client.upsert(self.collection_name, points=points, wait=True)
        return len(points)

    async def upsert_chunks(self, chunks: Sequence[CodeChunk],
                            vectors: Sequence[Sequence[float]]) -> int:
        self._ensure_open()
        if (isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence)
                or isinstance(vectors, (str, bytes)) or not isinstance(vectors, Sequence)):
            raise ForgePilotError("rag_vector_input", "Chunks and vectors must be sequences")
        checked_chunks = [CodeChunk.model_validate(chunk) for chunk in chunks]
        checked_vectors = [self._vector(vector) for vector in vectors]
        if len(checked_chunks) != len(checked_vectors):
            raise ForgePilotError("rag_vector_input", "Each chunk requires exactly one vector")
        if not checked_chunks:
            return 0
        dimension = len(checked_vectors[0])
        if any(len(vector) != dimension for vector in checked_vectors):
            raise ForgePilotError("rag_vector_input", "All vectors in a batch must have one dimension")
        async with self._lock:
            self._ensure_open()
            return await self._call(self._upsert_chunks, checked_chunks, checked_vectors)

    def _search(self, repository_id: str, vector: list[float], limit: int) -> list[RetrievedChunk]:
        if not self._ensure_dimension(len(vector), create=False):
            return []
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=self._repository_filter(repository_id),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [RetrievedChunk(chunk=self._chunk(point.payload), score=float(point.score), rank=rank)
                for rank, point in enumerate(response.points, start=1)]

    async def search(self, repository_id: str, query_vector: Sequence[float], *,
                     limit: int = 8) -> list[RetrievedChunk]:
        self._ensure_open()
        repository_id = checked_repository_id(repository_id)
        vector = self._vector(query_vector)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ForgePilotError("rag_vector_input", "Search limit must be between 1 and 100")
        async with self._lock:
            self._ensure_open()
            return await self._call(self._search, repository_id, vector, limit)

    def _delete(self, condition: qmodels.Filter) -> int:
        if not self._client.collection_exists(self.collection_name):
            return 0
        count = self._client.count(
            self.collection_name, count_filter=condition, exact=True).count
        if count:
            self._client.delete(
                self.collection_name,
                points_selector=qmodels.FilterSelector(filter=condition),
                wait=True,
            )
        return count

    async def delete_file(self, repository_id: str, path: str) -> int:
        self._ensure_open()
        repository_id = checked_repository_id(repository_id)
        path = checked_relative_path(path)
        async with self._lock:
            self._ensure_open()
            return await self._call(self._delete, self._file_filter(repository_id, path))

    async def clear_repository(self, repository_id: str) -> int:
        self._ensure_open()
        repository_id = checked_repository_id(repository_id)
        async with self._lock:
            self._ensure_open()
            return await self._call(self._delete, self._repository_filter(repository_id))

    def _count(self, repository_id: str) -> int:
        if not self._client.collection_exists(self.collection_name):
            return 0
        return self._client.count(
            self.collection_name,
            count_filter=self._repository_filter(repository_id),
            exact=True,
        ).count

    async def count(self, repository_id: str) -> int:
        self._ensure_open()
        repository_id = checked_repository_id(repository_id)
        async with self._lock:
            self._ensure_open()
            return await self._call(self._count, repository_id)

    async def _call(self, function, *args):
        def serialized():
            # asyncio task cancellation cannot stop an already-running Python
            # thread. Keep the underlying client serialized until that call
            # really returns, even if its awaiting task has been cancelled.
            with self._thread_lock:
                return function(*args)
        try:
            return await asyncio.to_thread(serialized)
        except ForgePilotError:
            raise
        except Exception:
            raise ForgePilotError("rag_vector_storage",
                                  "Embedded vector store operation failed") from None

    async def aclose(self) -> None:
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            try:
                await self._call(self._client.close)
            except Exception:
                raise ForgePilotError("rag_vector_storage",
                                      "Embedded vector store could not be closed") from None
            finally:
                self._closed = True
