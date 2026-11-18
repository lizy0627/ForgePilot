"""Offline, bounded timing probes for mission persistence, SSE, RAG and Artifact reads.

Run after other test/build workloads settle. This measures local fixture paths,
not production Qdrant or a rendered UI frame. Results are observations, never
pass/fail thresholds. The existing profile_hotpaths.py covers repository search
and large snapshot/redaction separately.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from forgepilot.artifacts import validate_artifact
from forgepilot.mission_events import MissionEventStream, publish_mission_event
from forgepilot.orchestration import GraphDefinition, NodeDefinition, NodeType, validate_graph
from forgepilot.orchestration.store import GraphStore
from forgepilot.rag import CodeChunk, QdrantVectorStore, RagConfig, RetrievalService
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore

TASK_ID = "10000000-0000-4000-a000-000000000001"


def summary(samples: list[float], scope: str) -> dict:
    ordered = sorted(samples)
    return {
        "scope": scope,
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[(95 * len(ordered) + 99) // 100 - 1], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def persistence_sse_artifact(temp: Path) -> dict:
    store = SqliteEventStore(temp / "mission.sqlite3", artifact_root=temp / "artifacts")
    try:
        graph = GraphDefinition("profile.v1", 1, (
            NodeDefinition("work", NodeType.DETERMINISTIC, "work"),
        ))
        GraphStore(store).create_run(TASK_ID, validate_graph(graph))
        publish_mission_event(store, TASK_ID, "task.created", {"request": "offline fixture"})
        stream = MissionEventStream(store)
        latest = store._db.execute(
            "SELECT MAX(sequence) FROM graph_events WHERE task_id=?", (TASK_ID,),
        ).fetchone()[0]
        subscriber = stream.subscribe(TASK_ID, f"{TASK_ID}:{latest}")
        persist_ms, read_ms, available_ms = [], [], []
        try:
            for index in range(30):
                started = perf_counter()
                publish_mission_event(store, TASK_ID, "tool.completed", {"index": index})
                committed = perf_counter()
                events, _ = subscriber.read()
                read = perf_counter()
                assert len(events) == 1 and events[0].data["index"] == index
                subscriber.after = int(events[0].event_id.rsplit(":", 1)[1])
                persist_ms.append((committed - started) * 1000)
                read_ms.append((read - committed) * 1000)
                available_ms.append((read - started) * 1000)
        finally:
            subscriber.close()

        fixture = json.loads((ROOT / "client/tests/fixtures/mission_artifacts.json").read_text(encoding="utf-8"))
        artifact = validate_artifact(fixture["items"][0])
        store.artifacts.put(artifact)
        load_ms = []
        for _ in range(30):
            started = perf_counter()
            loaded = store.artifacts.get(artifact.artifact_id)
            load_ms.append((perf_counter() - started) * 1000)
            assert loaded.content_sha256 == artifact.content_sha256
        return {
            "sqlite_event_persist": summary(persist_ms, "one WAL/FULL-synchronous mission event transaction, local temporary database"),
            "sse_backend_read": summary(read_ms, "MissionSubscription.read of one committed event; excludes HTTP and UI"),
            "sse_backend_publish_to_read": summary(available_ms, "publish to in-process SSE subscriber availability; excludes HTTP and UI"),
            "artifact_load": summary(load_ms, "one inline TaskSpec via SqliteArtifactStore.get with integrity and schema checks"),
        }
    finally:
        store.close()


class OfflineEmbeddings:
    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


async def rag_retrieval(temp: Path) -> dict:
    repository_root = temp / "rag-repository"
    repository_root.mkdir()
    for index in range(200):
        text = ("int helper() { return 1; }\n" * 10)
        if index == 47:
            text += "int find_widget() { return 47; }\n"
        (repository_root / f"file{index:03}.cpp").write_text(text, encoding="utf-8")
    repository = Repository(repository_root, "rg")
    config = RagConfig(enabled=True, embedding_model="offline-fixture",
                       embedding_base_url="http://127.0.0.1:9/v1",
                       retrieval_top_k=8, context_max_chars=24000)
    service = RetrievalService("offline-profile", repository, config, None, None)
    samples = []
    for _ in range(20):
        started = perf_counter()
        results = await service.retrieve("find_widget", mode="lexical")
        samples.append((perf_counter() - started) * 1000)
        assert results and any(result.path == "file047.cpp" for result in results)
    lexical = summary(samples, "RAG lexical retrieval over 200 local C++ files; no Qdrant, embedding provider or clangd")

    chunks = []
    vectors = []
    for source in sorted(repository_root.glob("*.cpp")):
        content = source.read_text(encoding="utf-8")
        chunks.append(CodeChunk(repository_id="offline-profile", path=source.name, language="cpp",
                                start_line=1, end_line=len(content.splitlines()), content=content,
                                file_hash=sha256(source.read_bytes()).hexdigest()))
        vectors.append([1.0, 0.0] if source.name == "file047.cpp" else [0.0, 1.0])
    indexed = {}
    async with QdrantVectorStore(temp / "qdrant") as vector_store:
        assert await vector_store.upsert_chunks(chunks, vectors) == 200
        service = RetrievalService("offline-profile", repository, config, OfflineEmbeddings(), vector_store)
        for mode in ("vector", "hybrid"):
            observed = []
            for _ in range(20):
                started = perf_counter()
                results = await service.retrieve("find_widget", mode=mode)
                observed.append((perf_counter() - started) * 1000)
                assert results and any(result.path == "file047.cpp" for result in results)
            indexed[mode] = summary(
                observed,
                f"RAG {mode} retrieval over 200 locally indexed Qdrant chunks; in-process deterministic embeddings, no network or clangd",
            )
    return {"lexical": lexical, "indexed_vector": indexed["vector"],
            "indexed_hybrid": indexed["hybrid"]}


def backend_startup() -> dict:
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/release_smoke.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=45, check=True,
    )
    match = re.search(r"^backend_ready_ms=([0-9.]+)$", result.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("release_smoke.py did not report backend readiness time")
    return {"scope": "cold Python backend process launch to /openapi.json response; one sample, excludes Qt UI and Qdrant",
            "samples": 1, "observed_ms": float(match.group(1))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new JSON output path (refuses overwrite)")
    arguments = parser.parse_args()
    if arguments.output and arguments.output.exists():
        parser.error("output already exists")
    with tempfile.TemporaryDirectory(prefix="forgepilot-mission-profile-") as directory:
        temp = Path(directory)
        measurements = persistence_sse_artifact(temp)
        measurements["rag"] = asyncio.run(rag_retrieval(temp))
    measurements["backend_startup"] = backend_startup()
    document = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "measurements": measurements,
        "unmeasured": ["HTTP SSE wire latency", "production-scale Qdrant index query", "full Qt application cold startup"],
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
