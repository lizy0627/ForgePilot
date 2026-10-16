"""Optional code RAG contracts, adapters and incremental indexing."""

from .config import (RagConfig, RagRuntimeAvailability, RagRuntimeSettings, add_rag_arguments,
                     protect_embedding_api_key, rag_config_from_args,
                     rag_runtime_availability_from_args, rag_runtime_from_args,
                     unprotect_embedding_api_key)
from .embedding import (EmbeddingProvider, EmbeddingProviderAvailability,
                        OpenAICompatibleEmbeddingProvider, embedding_provider_from_runtime)
from .indexer import RepositoryIndexer
from .integration import RagManager, RagRepositoryServices, repository_id_for
from .admin import RagIndexCoordinator
from .models import (CodeChunk, IndexFileState, IndexProgress, RetrievalQuery, RetrievedChunk,
                     RetrievalResult, RagIndexStatus, make_chunk_id)
from .retrieval import QueryBuilder, QueryTerms, RetrievalService
from .store import RagFileStore, create_rag_schema
from .vector_store import QdrantVectorStore, VectorStore, vector_store_path

__all__ = (
    "CodeChunk", "IndexFileState", "IndexProgress", "RetrievalQuery", "RetrievedChunk",
    "RetrievalResult", "RagIndexStatus", "RepositoryIndexer", "RetrievalService",
    "RagManager", "RagRepositoryServices", "RagIndexCoordinator", "repository_id_for",
    "QueryBuilder", "QueryTerms",
    "RagConfig", "RagRuntimeAvailability", "RagRuntimeSettings", "RagFileStore",
    "EmbeddingProvider", "EmbeddingProviderAvailability", "OpenAICompatibleEmbeddingProvider",
    "QdrantVectorStore", "VectorStore", "create_rag_schema", "make_chunk_id", "vector_store_path",
    "add_rag_arguments", "embedding_provider_from_runtime", "rag_config_from_args",
    "rag_runtime_availability_from_args", "rag_runtime_from_args",
    "protect_embedding_api_key", "unprotect_embedding_api_key",
)
