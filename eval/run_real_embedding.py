"""Explicit real-Embedding retrieval smoke test; credentials stay in the environment."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from forgepilot.process import executable
from forgepilot.rag import (OpenAICompatibleEmbeddingProvider, QdrantVectorStore,
                            RagConfig, RagFileStore, RepositoryIndexer,
                            RetrievalQuery, RetrievalService, repository_id_for)
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot_eval.retrieval import load_retrieval_tasks


async def run(args) -> dict:
    model = os.environ.get("FORGEPILOT_EMBEDDING_MODEL")
    base_url = os.environ.get("FORGEPILOT_EMBEDDING_BASE_URL")
    key = (os.environ.get("FORGEPILOT_EMBEDDING_API_KEY")
           or os.environ.get("FORGEPILOT_API_KEY"))
    if not model or not base_url or not key:
        raise ValueError("real_embedding_configuration_missing")
    repository = await Repository.open(
        str(args.repository.resolve()), rg_executable=executable(args.rg, "rg"),
        git_executable=executable(args.git, "git"))
    tasks = [task for task in load_retrieval_tasks(args.tasks.resolve())
             if any(path.casefold().endswith((".cpp", ".h", ".hpp"))
                    for path in task.expected_paths)][:args.query_count]
    if len(tasks) < args.query_count:
        raise ValueError("not_enough_cpp_queries")
    config = RagConfig(enabled=True, embedding_model=model,
                       embedding_base_url=base_url, retrieval_top_k=10)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url, key, model, batch_size=config.embedding_batch_size,
        timeout=args.timeout)
    events = SqliteEventStore()
    with tempfile.TemporaryDirectory(prefix="ForgePilot-real-embedding-") as temporary:
        vectors = QdrantVectorStore(Path(temporary) / "qdrant")
        try:
            repository_id = repository_id_for(repository.root)
            files = RagFileStore(events)
            indexer = RepositoryIndexer(
                repository_id, repository, config, provider, vectors, files)
            indexed = await indexer.index_repository()
            if indexed.status not in {"ready", "degraded"} or indexed.failed:
                raise RuntimeError("real_embedding_index_failed")
            retrieval = RetrievalService(
                repository_id, repository, config, provider, vectors,
                file_store=files)
            rows = []
            for task in tasks:
                started = perf_counter()
                results = await retrieval.retrieve(RetrievalQuery(
                    repository_id=repository_id, query=task.query,
                    top_k=10, context_max_chars=config.context_max_chars))
                rows.append({
                    "id": task.id,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                    "expected_paths": list(task.expected_paths),
                    "results": [{"path": item.path, "line": item.line,
                                 "end_line": item.end_line, "symbol": item.symbol}
                                for item in results],
                    "context_chars": sum(len(item.content) for item in results),
                    "source_status": [item.model_dump() for item in retrieval.last_trace.sources],
                })
            document = {
                "evaluation_type": "real_embedding_e2e",
                "real_embedding_request_completed": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "provider_token_usage": None,
                "provider_token_usage_note": "The OpenAI-compatible Embeddings response does not expose normalized usage in this client.",
                "indexed_files": indexed.processed - indexed.failed,
                "indexed_chunks": indexed.chunks,
                "queries": rows,
            }
            serialized = json.dumps(document, ensure_ascii=False, indent=2)
            if key in serialized:
                raise RuntimeError("credential_output_guard")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
            return document
        finally:
            await vectors.aclose()
            await provider.aclose()
            events.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--tasks", type=Path, default=ROOT / "eval/retrieval_tasks.json")
    parser.add_argument("--output", type=Path, default=ROOT / "logs/retrieval-eval/real-embedding-e2e.json")
    parser.add_argument("--query-count", type=int, choices=range(2, 11), default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--rg")
    parser.add_argument("--git")
    args = parser.parse_args(argv)
    try:
        document = asyncio.run(run(args))
    except ValueError as exc:
        if str(exc) == "real_embedding_configuration_missing":
            print("real Embedding E2E skipped: explicit Embedding model, base URL, and runtime key are required")
            return 3
        print("real Embedding E2E failed: invalid configuration or fixture")
        return 2
    except Exception:
        print("real Embedding E2E failed: provider, index, or retrieval did not complete")
        return 2
    print(str(args.output))
    print(f"indexed_files={document['indexed_files']} indexed_chunks={document['indexed_chunks']} queries={len(document['queries'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
