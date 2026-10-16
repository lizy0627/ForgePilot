"""RAG management API, repository isolation and durable status."""
import asyncio
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from forgepilot.api import create_app
from forgepilot.rag import (RagFileStore, RagIndexCoordinator, RagManager,
                            RagRuntimeSettings)
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
            assert finished["file_count"] > 0 and finished["chunks"] > 0
            stream = await client.get("/api/v1/rag/index/events",
                                      params={"repository_path": str(cpp_repo)})
            assert stream.status_code == 200
            assert "event: rag_index_progress" in stream.text

    # A new coordinator reconstructs the repository status from rag_files.
    restarted = management(store, embeddings, vectors, rg_exe, git_exe)
    app = create_app(lambda trace: None, store=store, rag_coordinator=restarted)
    async with app.router.lifespan_context(app):
        async with await client_for(app) as client:
            restored = await client.get("/api/v1/rag/status",
                                        params={"repository_path": str(cpp_repo)})
            assert restored.status_code == 200
            assert restored.json()["file_count"] == finished["file_count"]
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
