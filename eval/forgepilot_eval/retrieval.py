"""Reproducible offline evaluation of the production retrieval pipeline.

This module deliberately measures retrieval only.  Its deterministic feature
hasher is an evaluation fixture, not a substitute for a production embedding
model and never produces an Agent task-success claim.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import tempfile
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forgepilot.process import executable
from forgepilot.rag import (QdrantVectorStore, RagConfig, RagFileStore,
                            RepositoryIndexer, RetrievalQuery, RetrievalService)
from forgepilot.rag.integration import repository_id_for
from forgepilot.rag.models import checked_relative_path
from forgepilot.repository import EXCLUDED, Repository
from forgepilot.sqlite_store import SqliteEventStore


DATA_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DATA_ROOT.parent
REQUIRED_CATEGORIES = frozenset({
    "exact_symbol", "semantic_natural_language", "cross_file_call",
    "implementation_test_association", "cmake_qml_config", "chinese_requirement",
})
MODES = ("lexical", "vector", "hybrid")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[\u3400-\u9fff]+")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")


class RetrievalTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    query: str = Field(min_length=1, max_length=16_384)
    expected_paths: tuple[str, ...] = Field(min_length=1, max_length=16)
    expected_symbols: tuple[str, ...] = Field(default=(), max_length=16)
    category: str

    @field_validator("expected_paths", mode="before")
    @classmethod
    def paths_from_json(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("expected_symbols", mode="before")
    @classmethod
    def symbols_from_json(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("expected_paths")
    @classmethod
    def safe_paths(cls, values):
        checked = tuple(checked_relative_path(value) for value in values)
        if len({value.casefold() for value in checked}) != len(checked):
            raise ValueError("expected_paths must be unique")
        return checked

    @field_validator("expected_symbols")
    @classmethod
    def bounded_symbols(cls, values):
        if any(not value.strip() or value != value.strip() or len(value) > 256
               for value in values):
            raise ValueError("expected_symbols must be bounded nonblank text")
        return values

    @model_validator(mode="after")
    def known_category(self):
        if self.category not in REQUIRED_CATEGORIES:
            raise ValueError("unknown retrieval category")
        return self


class DeterministicEmbeddingProvider:
    """Local feature hashing for repeatable pipeline tests; no network or model."""

    name = "deterministic_feature_hash_v1"

    def __init__(self, dimension: int = 384) -> None:
        if type(dimension) is not int or not 64 <= dimension <= 4_096:
            raise ValueError("dimension must be between 64 and 4096")
        self.dimension = dimension

    @staticmethod
    def _features(text: str) -> Counter[str]:
        features: Counter[str] = Counter()
        for raw in _WORD.findall(text):
            folded = raw.casefold()
            if re.fullmatch(r"[\u3400-\u9fff]+", raw):
                units = [*raw, *(raw[index:index + 2] for index in range(len(raw) - 1))]
            else:
                units = [folded]
                parts = [part.casefold() for part in re.split(r"_+", raw) if part]
                for part in parts:
                    units.extend(piece.casefold() for piece in _CAMEL.findall(part))
                    if len(part) >= 4:
                        units.extend(part[index:index + 3]
                                     for index in range(len(part) - 2))
            features.update(unit for unit in units if unit)
        return features

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, count in self._features(text).items():
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % self.dimension
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[slot] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    async def embed_documents(self, texts):
        return [self.embed(text) for text in texts]

    async def embed_query(self, text):
        return self.embed(text)


def load_retrieval_tasks(path: Path | None = None) -> list[RetrievalTask]:
    manifest = path or DATA_ROOT / "retrieval_tasks.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if set(data) != {"version", "name", "tasks"} or data["version"] != 1:
        raise ValueError("unsupported retrieval task manifest")
    tasks = [RetrievalTask.model_validate(item) for item in data["tasks"]]
    if len(tasks) < 20 or len({task.id for task in tasks}) != len(tasks):
        raise ValueError("retrieval benchmark needs at least 20 unique tasks")
    if {task.category for task in tasks} != REQUIRED_CATEGORIES:
        raise ValueError("retrieval benchmark must cover every required category")
    return tasks


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def summarize_mode(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("cannot summarize an empty retrieval run")
    latencies = [row["latency_ms"] for row in rows]
    return {
        "task_count": len(rows),
        "recall_at_5": sum(row["recall_at_5"] for row in rows) / len(rows),
        "recall_at_10": sum(row["recall_at_10"] for row in rows) / len(rows),
        "mrr_at_10": sum(row["reciprocal_rank_at_10"] for row in rows) / len(rows),
        "average_latency_ms": statistics.fmean(latencies),
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "average_chunk_count": statistics.fmean(row["chunk_count"] for row in rows),
        "average_context_chars": statistics.fmean(row["context_chars"] for row in rows),
    }


def _score(task: RetrievalTask, results, latency_ms: float) -> dict:
    expected = {path.casefold() for path in task.expected_paths}
    ranked_paths = [result.path for result in results]

    def recall(limit):
        found = {path.casefold() for path in ranked_paths[:limit]} & expected
        return len(found) / len(expected)

    rank = next((index for index, path in enumerate(ranked_paths[:10], 1)
                 if path.casefold() in expected), None)
    return {
        "id": task.id,
        "category": task.category,
        "expected_paths": list(task.expected_paths),
        "returned_paths": ranked_paths,
        "recall_at_5": recall(5),
        "recall_at_10": recall(10),
        "reciprocal_rank_at_10": 0.0 if rank is None else 1.0 / rank,
        "latency_ms": latency_ms,
        "chunk_count": len(results),
        "context_chars": sum(len(result.content) for result in results),
    }


def _markdown(document: dict) -> str:
    lines = [
        "# ForgePilot offline retrieval evaluation", "",
        "This report measures repository retrieval only. It does not run an LLM, "
        "does not measure Agent task success, and does not establish production "
        "embedding quality.", "",
        f"Corpus: `{document['metadata']['repository_revision']}`; tasks: "
        f"{document['metadata']['task_count']}; embedding: "
        f"`{document['metadata']['embedding']}`.", "",
        "| Mode | Recall@5 | Recall@10 | MRR@10 | Avg ms | P50 ms | P95 ms | Avg chunks | Avg context chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in document["metadata"]["modes"]:
        value = document["metrics"][mode]
        lines.append(
            f"| {mode} | {value['recall_at_5']:.3f} | {value['recall_at_10']:.3f} | "
            f"{value['mrr_at_10']:.3f} | {value['average_latency_ms']:.3f} | "
            f"{value['p50_latency_ms']:.3f} | {value['p95_latency_ms']:.3f} | "
            f"{value['average_chunk_count']:.2f} | {value['average_context_chars']:.2f} |")
    lines += ["", "## Per-task misses", ""]
    misses = 0
    for mode in document["metadata"]["modes"]:
        for row in document["tasks"][mode]:
            if row["recall_at_10"] < 1.0:
                misses += 1
                lines.append(
                    f"- `{mode}` / `{row['id']}`: Recall@10={row['recall_at_10']:.3f}; "
                    f"expected `{', '.join(row['expected_paths'])}`.")
    if not misses:
        lines.append("- None.")
    lines += ["", "Latency is local wall-clock retrieval time after indexing. The deterministic "
              "feature hasher exists only to make CI repeatable; use a separately identified real "
              "embedding/model run before making quality or Agent-success claims.", ""]
    return "\n".join(lines)


async def evaluate(repository_path: Path, tasks: list[RetrievalTask], modes: tuple[str, ...],
                   *, rg_executable: str, git_executable: str,
                   task_manifest_sha256: str | None = None) -> dict:
    repository = await Repository.open(
        str(repository_path), rg_executable=rg_executable, git_executable=git_executable)
    for task in tasks:
        for path in task.expected_paths:
            repository._path(path)
        for symbol in task.expected_symbols:
            if not any(symbol in repository._read_snapshot(path)[0]
                       for path in task.expected_paths):
                raise ValueError(f"unconfirmed expected symbol in task {task.id}")

    embedding = DeterministicEmbeddingProvider()
    events = SqliteEventStore()
    temporary_parent = repository.root / "logs"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="ForgePilot-retrieval-eval-", dir=temporary_parent) as temporary:
        vectors = QdrantVectorStore(Path(temporary) / "qdrant")
        try:
            config = RagConfig(
                enabled=True,
                # The evaluator writes its reports under artifacts/. Exclude
                # prior run outputs and the generated oversized OpenAPI file
                # from the indexed source corpus.
                exclude=(*EXCLUDED, "artifacts", "protocol/openapi.json"),
                embedding_model=embedding.name,
                embedding_base_url="https://offline.invalid/v1",
                retrieval_top_k=10,
                context_max_chars=24_000,
            )
            repository_id = repository_id_for(repository.root)
            file_store = RagFileStore(events)
            indexer = RepositoryIndexer(
                repository_id, repository, config, embedding, vectors, file_store)
            index_status = await indexer.index_repository()
            if index_status.failed:
                raise RuntimeError("offline retrieval corpus indexing failed")
            service = RetrievalService(
                repository_id, repository, config, embedding, vectors,
                file_store=file_store)
            task_rows = {}
            metrics = {}
            for mode in modes:
                rows = []
                for task in tasks:
                    query = RetrievalQuery(
                        repository_id=repository_id, query=task.query,
                        top_k=10, context_max_chars=24_000)
                    started = perf_counter()
                    results = await service.retrieve(query, mode=mode)
                    latency = round((perf_counter() - started) * 1_000, 6)
                    rows.append(_score(task, results, latency))
                task_rows[mode] = rows
                metrics[mode] = summarize_mode(rows)
            revision, dirty = await _git_state(repository, git_executable)
            corpus = {state.path: state.content_hash for state in file_store.list(repository_id)
                      if state.status == "indexed"}
            return {
                "metadata": {
                    "evaluation_type": "offline_retrieval",
                    "real_model_results_produced": False,
                    "agent_success_measured": False,
                    "embedding": embedding.name,
                    "repository_revision": revision,
                    "repository_dirty": dirty,
                    "corpus_sha256": hashlib.sha256(json.dumps(
                        corpus, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                    "task_manifest_sha256": task_manifest_sha256,
                    "task_count": len(tasks),
                    "modes": list(modes),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "indexed_files": index_status.processed - index_status.failed,
                    "indexed_chunks": index_status.chunks,
                },
                "metrics": metrics,
                "tasks": task_rows,
            }
        finally:
            await vectors.aclose()
            events.close()


async def _git_state(repository: Repository, git_executable: str) -> tuple[str, bool]:
    process = await asyncio.create_subprocess_exec(
        git_executable, "-C", str(repository.root), "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    stdout, _ = await process.communicate()
    revision = ("unknown" if process.returncode else
                stdout.decode("ascii", errors="replace").strip()[:64])
    status = await asyncio.create_subprocess_exec(
        git_executable, "-C", str(repository.root), "status", "--porcelain",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    status_output, _ = await status.communicate()
    return revision, bool(status.returncode or status_output)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="ForgePilot offline retrieval evaluator")
    result.add_argument("--mode", choices=(*MODES, "all"), default="all")
    result.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    result.add_argument("--tasks", type=Path, default=DATA_ROOT / "retrieval_tasks.json")
    result.add_argument("--json-out", type=Path, default=REPOSITORY_ROOT / "logs/retrieval-eval/results.json")
    result.add_argument("--markdown-out", type=Path, default=REPOSITORY_ROOT / "logs/retrieval-eval/report.md")
    result.add_argument("--rg")
    result.add_argument("--git")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        tasks = load_retrieval_tasks(args.tasks.resolve())
        modes = MODES if args.mode == "all" else (args.mode,)
        document = asyncio.run(evaluate(
            args.repository.resolve(), tasks, modes,
            rg_executable=executable(args.rg, "rg"),
            git_executable=executable(args.git, "git"),
            task_manifest_sha256=hashlib.sha256(args.tasks.resolve().read_bytes()).hexdigest()))
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
        args.markdown_out.write_text(_markdown(document), encoding="utf-8")
        print(str(args.json_out))
        print(str(args.markdown_out))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"retrieval evaluation failed: {type(exc).__name__}")
        return 2

