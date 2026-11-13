"""Repository-wide coordination of refresh, clear, and shutdown."""
import asyncio
from pathlib import Path
import threading

import pytest

from forgepilot.rag import (RagFileStore, RagIndexCoordinator, RagManager,
                            RagRuntimeSettings, repository_id_for)
from forgepilot.sqlite_store import SqliteEventStore
from test_rag_indexer import MemoryVectors, RecordingEmbeddings, config, repository


class ControlledEmbeddings(RecordingEmbeddings):
    def __init__(self):
        super().__init__()
        self.hold_paths = set()
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False
        self.refreshes = []
        self.pending_on_close = False

    async def embed_documents(self, texts):
        if any(f"Path: {path}\n" in text for path in self.hold_paths for text in texts):
            self.paused.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await super().embed_documents(texts)

    async def aclose(self):
        self.pending_on_close = any(not task.done() for task in self.refreshes)
        self.closed = True


class ThreadVectors(MemoryVectors):
    """Model a vector mutation that cannot stop after to_thread has started."""

    def __init__(self):
        super().__init__()
        self.hold_operation = None
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.running = False
        self.order = []
        self.closed = False
        self.closed_during_mutation = False
        self.cleared_during_mutation = False

    async def mutate(self, operation, mutation):
        loop = asyncio.get_running_loop()

        def run():
            self.running = True
            self.order.append(f"{operation}:started")
            loop.call_soon_threadsafe(self.started.set)
            self.release.wait()
            result = mutation()
            self.running = False
            self.order.append(f"{operation}:finished")
            return result

        return await asyncio.to_thread(run)

    async def replace_file(self, repository_id, path, chunks, vectors):
        if self.hold_operation != "replace":
            return await super().replace_file(repository_id, path, chunks, vectors)

        def replace():
            self.files[(repository_id, path)] = list(chunks)
            self.replacements.append((repository_id, path))
            return len(chunks)

        return await self.mutate("replace", replace)

    async def delete_file(self, repository_id, path):
        if self.hold_operation != "delete":
            return await super().delete_file(repository_id, path)
        return await self.mutate(
            "delete", lambda: len(self.files.pop((repository_id, path), [])))

    async def clear_repository(self, repository_id):
        self.cleared_during_mutation = self.running
        self.order.append("clear")
        return await super().clear_repository(repository_id)

    async def aclose(self):
        self.closed_during_mutation = self.running
        self.closed = True
        self.order.append("close")


def management(events, embeddings, vectors):
    manager = RagManager(
        RagRuntimeSettings(config=config(), embedding_api_key="fixture-secret"),
        RagFileStore(events), embedding_provider=embeddings, vector_store=vectors)

    async def open_repository(path):
        return repository(Path(path))

    return manager, RagIndexCoordinator(manager, open_repository)


@pytest.mark.asyncio
async def test_clear_cancels_old_request_refresh_and_allows_rebuild(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    source = root / "a.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    events = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = ControlledEmbeddings(), MemoryVectors()
    manager, coordinator = management(events, embeddings, vectors)
    services = manager.for_repository(repository(root))
    repository_id = repository_id_for(root)
    refreshes = ()
    try:
        assert (await services.indexer.index_repository()).status == "ready"
        source.write_text("VALUE = 2\n", encoding="utf-8")
        embeddings.hold_paths.add("a.py")
        assert services.indexer.schedule_refresh("a.py")
        refreshes = tuple(services.indexer._refresh_tasks.values())
        await asyncio.wait_for(embeddings.paused.wait(), 2)

        cleared = await asyncio.wait_for(coordinator.clear(str(root)), 2)
        embeddings.release.set()
        await asyncio.gather(*refreshes, return_exceptions=True)

        assert manager.file_store.list(repository_id) == []
        assert not any(key[0] == repository_id for key in vectors.files)
        status = await coordinator.status(str(root))
        assert status["status"] == cleared["status"] == "empty"
        assert status["operation"] == "clear" and not status["running"]
        assert status["file_count"] == status["chunks"] == status["total_files"] == 0
        assert status["dirty_file_count"] == status["failed_file_count"] == 0
        assert status["revision"] == cleared["revision"]
        assert embeddings.cancelled.is_set()

        # A later request can rebuild after the clear has released its claim.
        rebuilt = manager.for_repository(repository(root))
        assert (await rebuilt.indexer.index_repository()).status == "ready"
        assert len(manager.file_store.list(repository_id)) == 1
        assert len(vectors.files[(repository_id, "a.py")]) == 1
    finally:
        embeddings.release.set()
        await asyncio.gather(*refreshes, return_exceptions=True)
        await coordinator.close()
        await manager.aclose()
        events.close()


@pytest.mark.asyncio
async def test_refresh_registration_is_shared_and_other_repositories_keep_running(tmp_path):
    first_root, other_root = tmp_path / "first", tmp_path / "other"
    first_root.mkdir()
    other_root.mkdir()
    (first_root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (first_root / "next.py").write_text("VALUE = 2\n", encoding="utf-8")
    (other_root / "b.py").write_text("VALUE = 3\n", encoding="utf-8")
    events = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = ControlledEmbeddings(), MemoryVectors()
    manager, coordinator = management(events, embeddings, vectors)
    first_repository, second_repository = repository(first_root), repository(first_root)
    first = manager.for_repository(first_repository)
    second = manager.for_repository(second_repository)
    other = manager.for_repository(repository(other_root))
    first_id, other_id = repository_id_for(first_root), repository_id_for(other_root)
    refreshes = []
    try:
        # Sharing index coordination must preserve each request's Repository and retrieval.
        assert first.indexer.repository is first_repository
        assert second.indexer.repository is second_repository
        assert first.retrieval is not second.retrieval
        embeddings.hold_paths.add("a.py")
        assert first.indexer.schedule_refresh("a.py")
        refreshes.extend(first.indexer._refresh_tasks.values())
        await asyncio.wait_for(embeddings.paused.wait(), 2)
        assert not second.indexer.schedule_refresh("a.py")

        # A paused refresh and a claim for one repository cannot block another.
        assert manager.begin_index(first_id)
        assert not second.indexer.schedule_refresh("next.py")
        assert other.indexer.schedule_refresh("b.py")
        other_refreshes = tuple(other.indexer._refresh_tasks.values())
        refreshes.extend(other_refreshes)
        await asyncio.wait_for(asyncio.gather(*other_refreshes), 2)
        assert len(manager.file_store.list(other_id)) == 1
        assert (other_id, "b.py") in vectors.files
        manager.finish_index(first_id)

        cleared = await asyncio.wait_for(coordinator.clear(str(first_root)), 2)
        assert cleared["status"] == "empty"
        assert len(manager.file_store.list(other_id)) == 1
        assert (other_id, "b.py") in vectors.files
        assert manager.file_store.list(first_id) == []

        # Fresh background work is accepted again after the clear.
        assert second.indexer.schedule_refresh("next.py")
        next_refreshes = tuple(second.indexer._refresh_tasks.values())
        refreshes.extend(next_refreshes)
        await asyncio.wait_for(asyncio.gather(*next_refreshes), 2)
        assert (first_id, "next.py") in vectors.files
    finally:
        embeddings.release.set()
        refreshes.extend(first.indexer._refresh_tasks.values())
        refreshes.extend(second.indexer._refresh_tasks.values())
        refreshes.extend(other.indexer._refresh_tasks.values())
        await asyncio.gather(*refreshes, return_exceptions=True)
        manager.finish_index(first_id)
        await coordinator.close()
        await manager.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("replace", "delete"))
async def test_clear_waits_for_running_vector_mutation_and_blocks_new_refresh(tmp_path, operation):
    root = tmp_path / "repository"
    root.mkdir()
    source = root / "a.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "next.py").write_text("VALUE = 2\n", encoding="utf-8")
    events = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = ControlledEmbeddings(), ThreadVectors()
    manager, coordinator = management(events, embeddings, vectors)
    services = manager.for_repository(repository(root))
    second = manager.for_repository(repository(root))
    repository_id = repository_id_for(root)
    refreshes = []
    clearing = None
    try:
        if operation == "delete":
            await services.indexer.index_file("a.py")
            source.unlink()
        vectors.hold_operation = operation
        assert services.indexer.schedule_refresh("a.py")
        refreshes.extend(services.indexer._refresh_tasks.values())
        await asyncio.wait_for(vectors.started.wait(), 2)

        clearing = asyncio.create_task(coordinator.clear(str(root)))
        await asyncio.sleep(0)
        assert not clearing.done()
        assert not second.indexer.schedule_refresh("next.py")
        assert not vectors.cleared_during_mutation

        vectors.release.set()
        cleared = await asyncio.wait_for(clearing, 2)
        await asyncio.gather(*refreshes, return_exceptions=True)
        assert vectors.order == [f"{operation}:started", f"{operation}:finished", "clear"]
        assert cleared["status"] == "empty"
        assert cleared["file_count"] == cleared["chunks"] == 0
        assert manager.file_store.list(repository_id) == []
        assert not any(key[0] == repository_id for key in vectors.files)
    finally:
        vectors.release.set()
        refreshes.extend(services.indexer._refresh_tasks.values())
        refreshes.extend(second.indexer._refresh_tasks.values())
        await asyncio.gather(*refreshes, return_exceptions=True)
        if clearing is not None:
            await asyncio.gather(clearing, return_exceptions=True)
        await coordinator.close()
        await manager.aclose()
        events.close()


@pytest.mark.asyncio
async def test_close_cancels_embedding_refresh_before_closing_resources(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    events = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = ControlledEmbeddings(), ThreadVectors()
    manager, coordinator = management(events, embeddings, vectors)
    services = manager.for_repository(repository(root))
    refreshes = ()
    try:
        embeddings.hold_paths.add("a.py")
        assert services.indexer.schedule_refresh("a.py")
        refreshes = tuple(services.indexer._refresh_tasks.values())
        embeddings.refreshes = refreshes
        await asyncio.wait_for(embeddings.paused.wait(), 2)

        await asyncio.wait_for(manager.aclose(), 2)
        assert embeddings.cancelled.is_set()
        assert not embeddings.pending_on_close
        assert all(task.done() for task in refreshes)
        assert embeddings.closed and vectors.closed
        assert not services.indexer.schedule_refresh("a.py")
        later = manager.for_repository(repository(root))
        assert later is None or not later.indexer.schedule_refresh("a.py")
        assert not manager.begin_index(repository_id_for(root))
        manager.finish_index(repository_id_for(root))
        assert not services.indexer.schedule_refresh("a.py")
    finally:
        embeddings.release.set()
        await asyncio.gather(*refreshes, return_exceptions=True)
        await coordinator.close()
        await manager.aclose()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("replace", "delete"))
async def test_close_joins_running_vector_mutation_before_closing_resources(tmp_path, operation):
    root = tmp_path / "repository"
    root.mkdir()
    source = root / "a.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "next.py").write_text("VALUE = 2\n", encoding="utf-8")
    events = SqliteEventStore(tmp_path / "rag.sqlite3")
    embeddings, vectors = ControlledEmbeddings(), ThreadVectors()
    manager, coordinator = management(events, embeddings, vectors)
    services = manager.for_repository(repository(root))
    refreshes = []
    closing = None
    try:
        if operation == "delete":
            await services.indexer.index_file("a.py")
            source.unlink()
        vectors.hold_operation = operation
        assert services.indexer.schedule_refresh("a.py")
        refreshes.extend(services.indexer._refresh_tasks.values())
        embeddings.refreshes = refreshes
        await asyncio.wait_for(vectors.started.wait(), 2)

        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        assert not embeddings.closed and not vectors.closed
        assert not services.indexer.schedule_refresh("next.py")

        vectors.release.set()
        await asyncio.wait_for(closing, 2)
        assert vectors.order == [f"{operation}:started", f"{operation}:finished", "close"]
        assert not vectors.closed_during_mutation
        assert not embeddings.pending_on_close
        assert embeddings.closed and vectors.closed
        assert all(task.done() for task in refreshes)
    finally:
        vectors.release.set()
        refreshes.extend(services.indexer._refresh_tasks.values())
        await asyncio.gather(*refreshes, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await coordinator.close()
        await manager.aclose()
        events.close()
