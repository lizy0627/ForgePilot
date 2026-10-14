"""Optional code RAG contracts. Indexing and retrieval are intentionally out of scope."""

from .config import (RagConfig, RagRuntimeSettings, add_rag_arguments, protect_embedding_api_key,
                     rag_config_from_args, rag_runtime_from_args, unprotect_embedding_api_key)
from .models import (CodeChunk, IndexFileState, RetrievalQuery, RetrievedChunk, RagIndexStatus,
                     make_chunk_id)
from .store import RagFileStore, create_rag_schema

__all__ = (
    "CodeChunk", "IndexFileState", "RetrievalQuery", "RetrievedChunk", "RagIndexStatus",
    "RagConfig", "RagRuntimeSettings", "RagFileStore", "create_rag_schema", "make_chunk_id",
    "add_rag_arguments", "rag_config_from_args", "rag_runtime_from_args",
    "protect_embedding_api_key", "unprotect_embedding_api_key",
)
