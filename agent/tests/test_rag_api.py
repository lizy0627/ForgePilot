"""RAG management API, repository isolation and durable status."""
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from forgepilot.api import create_app
from forgepilot.errors import ForgePilotError
from forgepilot.rag import (IndexFileState, RagFileStore, RagIndexCoordinator, RagManager,
                            RagRunStore, RagRuntimeSettings, repository_id_for)
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore
from test_rag_indexer import MemoryVectors, RecordingEmbeddings, config


def management(store, embeddings, vectors, rg_exe, git_exe):
    runtime = RagRuntimeSettings(config=config(), embedding_api_key="fixture-secret")
    manager = RagManager(runtime, RagFileStore(store), embedding_provider=embeddings,
                         vector_store=vectors)

    async def open_repository(path):
        return await Repository.open(path, rg_executable=rg_exe, git_executable=git_exe)
    return RagIndexCoordinator(manager, open_repository)


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def wait_finished(client, repository):
    for _ in range(100):
        response = await client.get("/api/v1/rag/status", params={"repository_path": str(repository)})
        assert response.status_code == 200
        if not response.json()["running"]:
            return response.json()
        await asyncio.sleep(0.01)
    raise AssertionError("index did not finish")


@pytest.mark.asyncio
async def test_rag_api_indexes_reports_progress_restores_and_deletes(
        cpp_repo, rg_exe, git_exe, tmp_path):
    database = tmp_path / "rag.sqlite3"
    store = SqliteEventStore(database)
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator,
                     heartbeat_seconds=0.01)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            started = await client.post("/api/v1/rag/index", json={"repository_path": str(cpp_repo)})
            assert started.status_code == 202
            assert started.json()["running"] is True
            finished = await wait_finished(client, cpp_repo)
            assert finished["status"] == "ready"
            assert finished["operation"] == "index"
            assert finished["file_count"] > 0 and finished["chunks"] > 0
            stream = await client.get("/api/v1/rag/index/events",
                                      params={"repository_path": str(cpp_repo)})
            assert stream.status_code == 200
            assert "event: rag_index_progress" in stream.text
            assert f"id: {finished['operation_id']}:{finished['revision']}" in stream.text
            current = await client.get("/api/v1/rag/index/events",
                params={"repository_path": str(cpp_repo)},
                headers={"Last-Event-ID": f"{finished['operation_id']}:{finished['revision']}"})
            assert current.status_code == 204

    # A new coordinator reconstructs the repository status from rag_files.
    restarted = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=restarted)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            restored = await client.get("/api/v1/rag/status",
                                        params={"repository_path": str(cpp_repo)})
            assert restored.status_code == 200
            assert restored.json()["file_count"] == finished["file_count"]
            assert restored.json()["operation_id"] == finished["operation_id"]
            assert restored.json()["revision"] == finished["revision"]
            deleted = await client.delete("/api/v1/rag/index",
                                          params={"repository_path": str(cpp_repo)})
            assert deleted.status_code == 200
            assert deleted.json()["deleted_files"] == finished["file_count"]
            assert deleted.json()["file_count"] == 0
            assert cpp_repo.joinpath("main.cpp").exists()
    store.close()


@pytest.mark.asyncio
async def test_rag_api_conflict_cancel_isolation_and_path_validation(
        cpp_repo, rg_exe, git_exe, tmp_path):
    other = tmp_path / "other-repository"
    shutil.copytree(cpp_repo, other, ignore=shutil.ignore_patterns(".git"))
    subprocess.run([git_exe, "init", "-q", str(other)], check=True, capture_output=True)
    store = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    embeddings.block = True
    embeddings.started = asyncio.Event()
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            first = await client.post("/api/v1/rag/index", json={"repository_path": str(cpp_repo)})
            assert first.status_code == 202
            await asyncio.wait_for(embeddings.started.wait(), 2)
            duplicate = await client.post("/api/v1/rag/index",
                                          json={"repository_path": str(cpp_repo / ".")})
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "rag_index_busy"
            deleting_busy = await client.delete(
                "/api/v1/rag/index", params={"repository_path": str(cpp_repo)})
            assert deleting_busy.status_code == 409
            assert deleting_busy.json()["error"]["code"] == "rag_index_busy"

            isolated = await client.get("/api/v1/rag/status",
                                        params={"repository_path": str(other)})
            assert isolated.status_code == 200
            assert isolated.json()["repository_id"] != first.json()["repository_id"]
            assert isolated.json()["file_count"] == 0

            cancelled = await client.post("/api/v1/rag/index/cancel",
                                          json={"repository_path": str(cpp_repo)})
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["running"] is False
            operation_id = cancelled.json()["operation_id"]
            revision = cancelled.json()["revision"]
            repeated_cancel = await client.post("/api/v1/rag/index/cancel",
                json={"repository_path": str(cpp_repo)})
            assert repeated_cancel.status_code == 200
            assert repeated_cancel.json()["operation_id"] == operation_id
            assert repeated_cancel.json()["revision"] == revision

            # A later retry recovers, and deleting it cannot touch another
            # repository's vectors or rag_files rows.
            embeddings.block = False
            embeddings.started = None
            assert (await client.post("/api/v1/rag/index",
                                      json={"repository_path": str(other)})).status_code == 202
            other_ready = await wait_finished(client, other)
            assert other_ready["file_count"] > 0
            assert (await client.post("/api/v1/rag/index",
                                      json={"repository_path": str(cpp_repo)})).status_code == 202
            await wait_finished(client, cpp_repo)
            assert (await client.delete("/api/v1/rag/index",
                                        params={"repository_path": str(cpp_repo)})).status_code == 200
            isolated_after_delete = await client.get(
                "/api/v1/rag/status", params={"repository_path": str(other)})
            assert isolated_after_delete.json()["file_count"] == other_ready["file_count"]
            assert isolated_after_delete.json()["chunks"] == other_ready["chunks"]

            repeated_delete = await client.delete(
                "/api/v1/rag/index", params={"repository_path": str(cpp_repo)})
            assert repeated_delete.status_code == 200
            assert repeated_delete.json()["deleted_files"] == 0
            assert repeated_delete.json()["status"] == "empty"
            assert repeated_delete.json()["operation"] == "clear"

            outside = await client.get("/api/v1/rag/status",
                                       params={"repository_path": str(tmp_path)})
            assert outside.status_code == 422
            assert outside.json()["error"]["code"] == "repository_not_git_root"
            traversal = await client.get("/api/v1/rag/status",
                                         params={"repository_path": str(cpp_repo / "..")})
            assert traversal.status_code == 422
    store.close()


@pytest.mark.asyncio
async def test_disabled_rag_reports_status_without_starting(cpp_repo, rg_exe, git_exe, tmp_path):
    store = SqliteEventStore(tmp_path / "disabled.sqlite3")
    manager = RagManager(None, RagFileStore(store))

    async def open_repository(path):
        return await Repository.open(path, rg_executable=rg_exe, git_executable=git_exe)
    coordinator = RagIndexCoordinator(manager, open_repository)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            status = await client.get("/api/v1/rag/status",
                                      params={"repository_path": str(cpp_repo)})
            assert status.status_code == 200
            assert status.json()["status"] == "disabled"
            assert status.json()["enabled"] is False
            start = await client.post("/api/v1/rag/index",
                                      json={"repository_path": str(cpp_repo)})
            assert start.status_code == 409
            assert start.json()["error"]["code"] == "rag_disabled"
    store.close()


@pytest.mark.asyncio
async def test_interrupted_run_recovers_as_failed_and_can_be_retried(
        cpp_repo, rg_exe, git_exe, tmp_path):
    database = tmp_path / "interrupted.sqlite3"
    store = SqliteEventStore(database)
    repository_id = repository_id_for(cpp_repo.resolve())
    RagFileStore(store).upsert(IndexFileState(
        repository_id=repository_id, path="main.cpp", content_hash="0" * 64,
        indexed_at=datetime.now(timezone.utc), chunk_count=1, status="indexed",
    ))
    started = RagRunStore(store).begin(repository_id, "index")
    store.close()

    # Reopening the database models process startup recovery. A nonterminal
    # durable run must never be reconstructed from files as ready/empty.
    store = SqliteEventStore(database)
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            recovered = await client.get(
                "/api/v1/rag/status", params={"repository_path": str(cpp_repo)})
            assert recovered.status_code == 200
            body = recovered.json()
            assert body["status"] == "failed" and body["running"] is False
            assert body["error_code"] == "rag_index_interrupted"
            assert body["operation_id"] == started.operation_id
            assert body["revision"] == started.revision + 1

            retry = await client.post(
                "/api/v1/rag/index", json={"repository_path": str(cpp_repo)})
            assert retry.status_code == 202
            assert retry.json()["operation_id"] != started.operation_id
            assert (await wait_finished(client, cpp_repo))["status"] == "ready"
    store.close()


@pytest.mark.asyncio
async def test_persistent_claim_blocks_a_second_coordinator(
        cpp_repo, rg_exe, git_exe, tmp_path):
    store = SqliteEventStore(tmp_path / "claim.sqlite3")
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    embeddings.block = True
    embeddings.started = asyncio.Event()
    first = management(store, embeddings, vectors, rg_exe, git_exe)
    second = management(store, embeddings, vectors, rg_exe, git_exe)
    try:
        await first.start(str(cpp_repo))
        await asyncio.wait_for(embeddings.started.wait(), 2)
        with pytest.raises(ForgePilotError, match="rag_index_busy") as error:
            await second.start(str(cpp_repo))
        assert error.value.code == "rag_index_busy"
        assert (await first.cancel(str(cpp_repo)))["status"] == "cancelled"
    finally:
        await first.close()
        await second.close()
        store.close()


@pytest.mark.asyncio
async def test_clear_failure_is_durable_sanitized_and_retryable(
        cpp_repo, rg_exe, git_exe, tmp_path):
    store = SqliteEventStore(tmp_path / "clear-failure.sqlite3")
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            assert (await client.post("/api/v1/rag/index",
                json={"repository_path": str(cpp_repo)})).status_code == 202
            assert (await wait_finished(client, cpp_repo))["status"] == "ready"

            vectors.fail_clear = True
            failed = await client.delete(
                "/api/v1/rag/index", params={"repository_path": str(cpp_repo)})
            assert failed.status_code == 500
            assert failed.json()["error"]["code"] == "rag_index_clear_failed"
            assert "vector backend detail" not in failed.text
            status = (await client.get("/api/v1/rag/status",
                params={"repository_path": str(cpp_repo)})).json()
            assert status["status"] == "failed"
            assert status["error_code"] == "rag_index_clear_failed"

            vectors.fail_clear = False
            cleared = await client.delete(
                "/api/v1/rag/index", params={"repository_path": str(cpp_repo)})
            assert cleared.status_code == 200 and cleared.json()["status"] == "empty"
    store.close()


@pytest.mark.asyncio
async def test_index_file_failures_publish_stable_failed_state(
        cpp_repo, rg_exe, git_exe, tmp_path):
    store = SqliteEventStore(tmp_path / "index-failure.sqlite3")
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    embeddings.fail_paths.update({
        "App.cpp", "CMakeLists.txt", "Counter.cpp", "Counter.hpp", "main.cpp",
    })
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            started = await client.post(
                "/api/v1/rag/index", json={"repository_path": str(cpp_repo)})
            assert started.status_code == 202
            failed = await wait_finished(client, cpp_repo)
            assert failed["status"] == "failed"
            assert failed["failed"] == failed["total_files"]
            assert failed["error_code"] == "rag_index_file_failed"
            assert failed["operation"] == "index"
    store.close()


@pytest.mark.asyncio
async def test_rag_api_strict_schema_does_not_echo_secrets(
        cpp_repo, rg_exe, git_exe, tmp_path, caplog):
    store = SqliteEventStore(tmp_path / "strict.sqlite3")
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    coordinator = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=coordinator)
    caplog.set_level(logging.DEBUG, logger="forgepilot")
    secret = "fixture-secret-source-and-env"
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            invalid_body = await client.post("/api/v1/rag/index", json={
                "repository_path": f" {secret}\n", "embedding_input": secret,
            })
            assert invalid_body.status_code == 422
            assert invalid_body.json()["error"]["code"] == "invalid_request"
            invalid_query = await client.get(
                "/api/v1/rag/status", params={"repository_path": f" {secret} "})
            assert invalid_query.status_code == 422
            assert invalid_query.json()["error"]["code"] == "invalid_request"
    captured = invalid_body.text + invalid_query.text + caplog.text
    assert secret not in captured
    store.close()
