"""HTTP-mocked embeddings and on-disk Qdrant Local Mode boundaries."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.rag import (OpenAICompatibleEmbeddingProvider, QdrantVectorStore,
                            add_rag_arguments, embedding_provider_from_runtime,
                            rag_runtime_availability_from_args, vector_store_path)
from forgepilot.rag.models import CodeChunk


FILE_HASH = hashlib.sha256(b"complete file").hexdigest()


def chunk(repository_id="repository-a", path="src/main.py", *, content="main content"):
    return CodeChunk(
        repository_id=repository_id,
        path=path,
        language="python",
        symbol="main",
        symbol_type="function",
        start_line=1,
        end_line=2,
        content=content,
        file_hash=FILE_HASH,
    )


@pytest.mark.asyncio
async def test_embedding_batches_preserve_order_and_validate_dimension():
    requests = []

    def handler(request):
        assert request.url == httpx.URL("https://provider.invalid/v1/embeddings")
        assert request.headers["authorization"] == "Bearer private-key"
        body = json.loads(request.content)
        requests.append(body)
        data = [{"index": index, "embedding": [float(len(text)), float(index), 1.0]}
                for index, text in enumerate(body["input"])]
        return httpx.Response(200, json={"object": "list", "data": list(reversed(data))})

    async with OpenAICompatibleEmbeddingProvider(
            "https://provider.invalid/v1", "private-key", "embedding-model",
            batch_size=2, transport=httpx.MockTransport(handler)) as provider:
        vectors = await provider.embed_documents(["a", "bb", "ccc"])
        query = await provider.embed_query("query")
        assert vectors == [[1.0, 0.0, 1.0], [2.0, 1.0, 1.0], [3.0, 0.0, 1.0]]
        assert query == [5.0, 0.0, 1.0]
        assert provider.dimension == 3
    assert [request["input"] for request in requests] == [["a", "bb"], ["ccc"], ["query"]]
    assert all(request["model"] == "embedding-model" for request in requests)


@pytest.mark.asyncio
async def test_embedding_rejects_dimension_changes_between_batches():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        dimension = 3 if calls == 1 else 2
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1.0] * dimension},
        ]})

    async with OpenAICompatibleEmbeddingProvider(
            "https://provider.invalid/v1", "private-key", "embedding-model",
            batch_size=1, transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(ForgePilotError, match="rag_embedding_dimension.*rebuild"):
            await provider.embed_documents(["first", "second"])


@pytest.mark.asyncio
async def test_embedding_timeout_http_and_transport_errors_are_redacted():
    secret = "sk-private-embedding-value"

    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json={"data": []})

    async with OpenAICompatibleEmbeddingProvider(
            "https://provider.invalid/v1", secret, "embedding-model", timeout=0.01,
            transport=httpx.MockTransport(slow)) as provider:
        with pytest.raises(ForgePilotError, match="rag_embedding_timeout") as timeout:
            await provider.embed_query("query")
        assert secret not in str(timeout.value)

    def rejected(request):
        return httpx.Response(400, text=f"request and {secret} are invalid")

    async with OpenAICompatibleEmbeddingProvider(
            "https://provider.invalid/v1", secret, "embedding-model",
            transport=httpx.MockTransport(rejected)) as provider:
        with pytest.raises(ForgePilotError, match="rag_embedding_http.*HTTP 400") as failure:
            await provider.embed_query("sensitive request body")
        assert secret not in str(failure.value)
        assert "sensitive request body" not in str(failure.value)

    def broken(request):
        raise RuntimeError(f"transport leaked {secret} and {request.content!r}")

    async with OpenAICompatibleEmbeddingProvider(
            "https://provider.invalid/v1", secret, "embedding-model",
            transport=httpx.MockTransport(broken)) as provider:
        with pytest.raises(ForgePilotError, match="rag_embedding_network") as failure:
            await provider.embed_query("another sensitive body")
        assert secret not in str(failure.value)
        assert "another sensitive body" not in str(failure.value)


@pytest.mark.asyncio
async def test_embedding_cancellation_propagates_and_closes_mock_transport():
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(60)
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatibleEmbeddingProvider(
        "https://provider.invalid/v1", "private-key", "embedding-model",
        transport=httpx.MockTransport(slow))
    pending = asyncio.create_task(provider.embed_query("query"))
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await provider.aclose()


def test_disabled_or_unconfigured_rag_returns_explicit_unavailable(monkeypatch):
    monkeypatch.delenv("FORGEPILOT_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("FORGEPILOT_API_KEY", raising=False)
    parser = argparse.ArgumentParser()
    add_rag_arguments(parser)
    disabled = rag_runtime_availability_from_args(parser.parse_args([]), api_key="unused")
    missing = rag_runtime_availability_from_args(
        parser.parse_args(["--enable-rag"]), api_key=None)
    missing_secret = rag_runtime_availability_from_args(parser.parse_args([
        "--enable-rag", "--embedding-model", "embedding-model",
        "--embedding-base-url", "https://provider.invalid/v1",
    ]), api_key=None)
    assert (disabled.status, disabled.reason) == ("unavailable", "disabled")
    assert (missing.status, missing.reason) == ("unavailable", "not_configured")
    assert (missing_secret.status, missing_secret.reason) == ("unavailable", "not_configured")
    provider = embedding_provider_from_runtime(disabled)
    assert provider.status == "unavailable" and provider.reason == "disabled"
    assert embedding_provider_from_runtime(missing).reason == "not_configured"

    monkeypatch.setenv("FORGEPILOT_DATABASE", "D:/app-data-fixture/tasks.sqlite3")
    assert vector_store_path() == Path("D:/app-data-fixture/qdrant")


@pytest.mark.asyncio
async def test_qdrant_local_crud_and_repository_isolation(tmp_path):
    storage = tmp_path / "vectors"
    moved = tmp_path / "vectors-closed"
    store = QdrantVectorStore(storage)
    try:
        first = chunk(content="first")
        second = chunk(path="src/other.py", content="second")
        foreign = chunk("repository-b", content="foreign")
        assert await store.count("repository-a") == 0
        assert await store.upsert_chunks(
            [first, second, foreign],
            [[1.0, 0.0, 0.0], [0.8, 0.2, 0.0], [1.0, 0.0, 0.0]],
        ) == 3

        results = await store.search("repository-a", [1.0, 0.0, 0.0], limit=10)
        assert [result.chunk.repository_id for result in results] == ["repository-a", "repository-a"]
        assert results[0].chunk == first
        assert {result.chunk.path for result in results} == {"src/main.py", "src/other.py"}
        assert [result.rank for result in results] == [1, 2]
        assert await store.count("repository-a") == 2
        assert await store.count("repository-b") == 1

        assert await store.delete_file("repository-a", "src/main.py") == 1
        assert await store.delete_file("repository-a", "src/main.py") == 0
        assert await store.count("repository-a") == 1
        assert await store.clear_repository("repository-a") == 1
        assert await store.count("repository-a") == 0
        assert await store.count("repository-b") == 1
    finally:
        await store.aclose()

    # The Local Mode lock/file handles are released; Windows can move the directory.
    storage.rename(moved)
    assert moved.is_dir()


@pytest.mark.asyncio
async def test_qdrant_dimension_mismatch_requires_rebuild_without_deleting_data(tmp_path):
    async with QdrantVectorStore(tmp_path / "vectors") as store:
        item = chunk()
        await store.upsert_chunks([item], [[1.0, 0.0, 0.0]])
        with pytest.raises(ForgePilotError, match="rag_index_rebuild_required.*rebuild"):
            await store.upsert_chunks([item], [[1.0, 0.0]])
        with pytest.raises(ForgePilotError, match="rag_index_rebuild_required.*rebuild"):
            await store.search("repository-a", [1.0, 0.0])
        assert await store.count("repository-a") == 1
