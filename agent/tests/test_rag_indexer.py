"""Incremental repository indexing and per-file failure isolation."""
import asyncio
from pathlib import Path

import pytest

from forgepilot.errors import ForgePilotError
from forgepilot.rag import RagConfig, RagFileStore, RagManager, RagRuntimeSettings, RepositoryIndexer
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.security import Redactor


class RecordingEmbeddings:
    def __init__(self):
        self.calls = []
        self.fail_paths = set()
        self.started = None
        self.block = False

    async def embed_documents(self, texts):
        copied = list(texts)
        self.calls.append(copied)
        if self.started is not None:
            self.started.set()
        if self.block:
            await asyncio.sleep(60)
        if any(f"Path: {path}\n" in text for path in self.fail_paths for text in copied):
            raise RuntimeError("provider body must not escape")
        return [[float(len(text)), 1.0] for text in copied]

    async def embed_query(self, text):
        return [float(len(text)), 1.0]


class MemoryVectors:
    def __init__(self):
        self.files = {}
        self.replacements = []
        self.fail_clear = False
        self.fail_delete_paths = set()

    async def replace_file(self, repository_id, path, chunks, vectors):
        await asyncio.sleep(0)
        assert len(chunks) == len(vectors)
        assert all(chunk.repository_id == repository_id and chunk.path == path for chunk in chunks)
        self.files[(repository_id, path)] = list(chunks)
        self.replacements.append((repository_id, path))
        return len(chunks)

    async def delete_file(self, repository_id, path):
        removed = len(self.files.pop((repository_id, path), []))
        if path in self.fail_delete_paths:
            raise RuntimeError("delete interrupted after vector mutation")
        return removed

    async def clear_repository(self, repository_id):
        if self.fail_clear:
            raise RuntimeError("vector backend detail must not escape")
        keys = [key for key in self.files if key[0] == repository_id]
        removed = sum(len(self.files.pop(key)) for key in keys)
        return removed


def config():
    return RagConfig(
        enabled=True,
        embedding_model="fixture",
        embedding_base_url="https://provider.invalid/v1",
        embedding_batch_size=2,
    )


def repository(root: Path):
    return Repository(root.resolve(), "rg")


def indexer(root, repository_id, events, embeddings, vectors):
    return RepositoryIndexer(
        repository_id,
        repository(root),
        config(),
        embeddings,
        vectors,
        RagFileStore(events),
    )


@pytest.mark.asyncio
async def test_index_redacts_credentials_before_embedding_and_vector_storage(tmp_path):
    secret = "fixture-index-credential"
    (tmp_path / "a.py").write_text(f"API_KEY = '{secret}'\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = RepositoryIndexer("repository-a", repository(tmp_path), config(),
            embeddings, vectors, RagFileStore(events), redactor=Redactor((secret,)))
        result = await subject.index_repository()
        assert result.status == "ready"
        assert secret not in str(embeddings.calls)
        assert secret not in "\n".join(chunk.content for chunks in vectors.files.values() for chunk in chunks)
    finally:
        events.close()


@pytest.mark.asyncio
async def test_manager_inherits_model_credential_redactor_for_index_and_query(tmp_path):
    model_key = "model-provider-opaque-credential-47"
    embedding_key = "embedding-provider-opaque-credential-91"
    (tmp_path / "a.py").write_text(
        f"MODEL_KEY = '{model_key}'\nEMBEDDING_KEY = '{embedding_key}'\n", encoding="utf-8")
    shared = Redactor((model_key,))
    events = SqliteEventStore(redactor=shared)
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    queries = []

    async def embed_query(text):
        queries.append(text)
        return [1.0, 0.0]

    async def search(*args, **kwargs):
        return []

    embeddings.embed_query = embed_query
    vectors.search = search
    try:
        manager = RagManager(RagRuntimeSettings(config=config(), embedding_api_key=embedding_key),
            RagFileStore(events), embedding_provider=embeddings, vector_store=vectors,
            redactor=shared)
        services = manager.for_repository(repository(tmp_path))
        assert (await services.indexer.index_repository()).status == "ready"
        await services.retrieval.retrieve(f"Find {model_key} and {embedding_key}")
        for secret in (model_key, embedding_key):
            assert secret not in str(embeddings.calls)
            assert secret not in str(queries)
            assert secret not in "\n".join(chunk.content for chunks in vectors.files.values() for chunk in chunks)
    finally:
        events.close()


@pytest.mark.asyncio
async def test_index_excludes_secret_filename_and_scrubs_symbol_metadata(tmp_path):
    secret = "opaqueProviderValue8473625"
    (tmp_path / (secret + ".py")).write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "safe.py").write_text(f"def {secret}():\n    return 1\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = RepositoryIndexer("repository-a", repository(tmp_path), config(),
            embeddings, vectors, RagFileStore(events), redactor=Redactor((secret,)))
        result = await subject.index_repository()
        assert result.status == "ready" and result.total_files == 1
        assert secret not in str(embeddings.calls)
        assert secret not in str(vectors.files)
        assert all(path == "safe.py" for _, path in vectors.files)
    finally:
        events.close()


@pytest.mark.asyncio
async def test_first_index_then_unchanged_repository_uses_no_more_embeddings(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class Beta:\n    pass\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        progress = []
        first = await subject.index_repository(progress=progress.append)
        assert (first.total_files, first.processed, first.skipped, first.failed) == (2, 2, 0, 0)
        assert first.chunks == 2
        assert len(embeddings.calls) == 2
        assert all(text.startswith(("Language: python\nPath: a.py\n",
                                    "Language: python\nPath: b.py\n"))
                   for call in embeddings.calls for text in call)
        assert progress[-1] == first

        second = await subject.index_repository()
        assert (second.total_files, second.processed, second.skipped, second.failed) == (2, 2, 2, 0)
        assert second.chunks == 2
        assert len(embeddings.calls) == 2
    finally:
        events.close()


@pytest.mark.asyncio
async def test_only_changed_file_is_rebuilt_and_deleted_file_is_removed(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    second.write_text("def beta():\n    return 2\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        await subject.index_repository()
        calls = len(embeddings.calls)
        first.write_text("def alpha():\n    return 3\n", encoding="utf-8")

        changed = await subject.index_repository()
        assert changed.skipped == 1
        assert len(embeddings.calls) == calls + 1
        assert vectors.replacements[-1] == ("repository-a", "a.py")
        assert vectors.files[("repository-a", "a.py")][0].content.splitlines()[-1] == "    return 3"

        second.unlink()
        removed = await subject.index_repository()
        assert removed.total_files == 2  # one unchanged file plus one deletion
        assert ("repository-a", "b.py") not in vectors.files
        assert RagFileStore(events).get("repository-a", "b.py") is None
    finally:
        events.close()


@pytest.mark.asyncio
async def test_renamed_file_converges_as_scoped_delete_and_add(tmp_path):
    original = tmp_path / "old_name.py"
    renamed = tmp_path / "new_name.py"
    original.write_text("VALUE = 'same content'\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        await subject.index_repository()
        original.rename(renamed)

        result = await subject.index_repository()

        assert result.status == "ready" and result.total_files == 2
        assert ("repository-a", "old_name.py") not in vectors.files
        assert ("repository-a", "new_name.py") in vectors.files
        assert [state.path for state in RagFileStore(events).list("repository-a")] == ["new_name.py"]
    finally:
        events.close()


@pytest.mark.asyncio
async def test_interrupted_delete_is_not_reported_ready_and_retry_converges(tmp_path):
    kept = tmp_path / "kept.py"
    removed = tmp_path / "removed.py"
    kept.write_text("KEEP = 1\n", encoding="utf-8")
    removed.write_text("REMOVE = 1\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        await subject.index_repository()
        removed.unlink()
        vectors.fail_delete_paths.add("removed.py")

        interrupted = await subject.index_repository()

        state = RagFileStore(events).get("repository-a", "removed.py")
        assert interrupted.status == "degraded"
        assert state is not None and state.status == "failed"
        assert ("repository-a", "removed.py") not in vectors.files

        vectors.fail_delete_paths.clear()
        retried = await subject.index_repository()
        assert retried.status == "ready"
        assert RagFileStore(events).get("repository-a", "removed.py") is None
    finally:
        events.close()


@pytest.mark.asyncio
async def test_embedding_failure_preserves_hash_and_retry_recovers(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        await subject.index_repository()
        files = RagFileStore(events)
        old_state = files.get("repository-a", "a.py")
        old_chunks = list(vectors.files[("repository-a", "a.py")])

        source.write_text("def alpha():\n    return 2\n", encoding="utf-8")
        embeddings.fail_paths.add("a.py")
        failed = await subject.index_repository()
        failed_state = files.get("repository-a", "a.py")
        assert failed.failed == 1 and failed_state.status == "failed"
        assert failed_state.content_hash == old_state.content_hash
        assert vectors.files[("repository-a", "a.py")] == old_chunks

        embeddings.fail_paths.clear()
        recovered = await subject.index_repository()
        recovered_state = files.get("repository-a", "a.py")
        assert recovered.failed == 0 and recovered_state.status == "indexed"
        assert recovered_state.content_hash != old_state.content_hash
        assert vectors.files[("repository-a", "a.py")][0].content.splitlines()[-1] == "    return 2"
    finally:
        events.close()


@pytest.mark.asyncio
async def test_new_file_failure_is_recorded_without_advancing_its_hash(tmp_path):
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    embeddings.fail_paths.add("b.py")
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        result = await subject.index_repository()
        files = RagFileStore(events)
        assert result.status == "degraded" and result.failed == 1
        assert files.get("repository-a", "a.py").status == "indexed"
        failed = files.get("repository-a", "b.py")
        assert failed.status == "failed" and failed.content_hash == "0" * 64
        assert ("repository-a", "a.py") in vectors.files
        assert ("repository-a", "b.py") not in vectors.files

        embeddings.fail_paths.clear()
        recovered = await subject.index_repository()
        assert recovered.failed == 0
        assert files.get("repository-a", "b.py").status == "indexed"
    finally:
        events.close()


@pytest.mark.asyncio
async def test_cancelled_embedding_leaves_retryable_state(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    embeddings.started = asyncio.Event()
    embeddings.block = True
    cancel = asyncio.Event()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        pending = asyncio.create_task(subject.index_repository(cancel_event=cancel))
        await asyncio.wait_for(embeddings.started.wait(), 1)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        state = RagFileStore(events).get("repository-a", "a.py")
        assert subject.get_status().status == "cancelled"
        assert state.status == "pending" and state.content_hash == "0" * 64

        embeddings.block = False
        recovered = await subject.index_repository()
        assert recovered.status == "ready"
        assert RagFileStore(events).get("repository-a", "a.py").status == "indexed"
    finally:
        events.close()


@pytest.mark.asyncio
async def test_two_repositories_index_in_parallel_without_crossing_data(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "same.py").write_text("VALUE = 'one'\n", encoding="utf-8")
    (two / "same.py").write_text("VALUE = 'two'\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        first = indexer(one, "repository-one", events, embeddings, vectors)
        second = indexer(two, "repository-two", events, embeddings, vectors)
        await asyncio.gather(first.index_repository(), second.index_repository())

        assert vectors.files[("repository-one", "same.py")][0].content.splitlines() == ["VALUE = 'one'"]
        assert vectors.files[("repository-two", "same.py")][0].content.splitlines() == ["VALUE = 'two'"]
        store = RagFileStore(events)
        assert [state.path for state in store.list("repository-one")] == ["same.py"]
        assert [state.path for state in store.list("repository-two")] == ["same.py"]
    finally:
        events.close()


def test_disabled_indexer_is_inert(tmp_path):
    events = SqliteEventStore()
    try:
        subject = RepositoryIndexer(
            "repository-a", repository(tmp_path), RagConfig(), None, None, RagFileStore(events))
        assert not subject.available
        assert subject.get_status().status == "disabled"
    finally:
        events.close()


@pytest.mark.asyncio
async def test_explicit_file_dirty_remove_and_clear_operations(tmp_path):
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    try:
        subject = indexer(tmp_path, "repository-a", events, embeddings, vectors)
        await subject.index_repository()
        calls = len(embeddings.calls)

        assert subject.mark_dirty("a.py").status == "pending"
        assert subject.get_status().status == "pending"
        indexed = await subject.index_file("a.py")
        assert indexed.processed == 1 and indexed.skipped == 0
        assert len(embeddings.calls) == calls + 1

        assert await subject.remove_file("a.py") == 1
        assert RagFileStore(events).get("repository-a", "a.py") is None
        assert await subject.clear_repository() == 1
        assert vectors.files == {}
        assert RagFileStore(events).list("repository-a") == []
        assert subject.get_status().status == "empty"
    finally:
        events.close()


@pytest.mark.asyncio
async def test_direct_index_and_dirty_calls_cannot_bypass_configured_scope(tmp_path):
    (tmp_path / "allowed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "protected.py").write_text("SECRET = 2\n", encoding="utf-8")
    events = SqliteEventStore()
    embeddings, vectors = RecordingEmbeddings(), MemoryVectors()
    scoped = RagConfig(
        enabled=True, embedding_model="fixture",
        embedding_base_url="https://provider.invalid/v1",
        exclude=("protected.py",))
    try:
        subject = RepositoryIndexer(
            "repository-a", repository(tmp_path), scoped,
            embeddings, vectors, RagFileStore(events))
        result = await subject.index_repository()
        assert result.status == "ready"
        assert set(vectors.files) == {("repository-a", "allowed.py")}
        with pytest.raises(ForgePilotError, match="rag_file_excluded"):
            await subject.index_file("protected.py")
        with pytest.raises(ForgePilotError, match="rag_file_excluded"):
            subject.mark_dirty("protected.py")
    finally:
        events.close()
