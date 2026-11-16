"""Mission retrieval uses current worktree bytes and never rewrites source indexes."""
import asyncio
from datetime import datetime, timezone
import hashlib
import subprocess

import pytest

from forgepilot.artifacts.models import DataPolicy
from forgepilot.rag.config import RagConfig, RagRuntimeAvailability
from forgepilot.rag.integration import RagManager, repository_id_for
from forgepilot.rag.mission import build_mission_retrieval
from forgepilot.rag.models import CodeChunk, IndexFileState, RetrievedChunk
from forgepilot.rag.store import RagFileStore
from forgepilot.repository import Repository
from forgepilot.sqlite_store import SqliteEventStore


class Embeddings:
    def __init__(self, *, fail=False):
        self.queries = []
        self.fail = fail

    async def embed_query(self, text):
        self.queries.append(text)
        if self.fail:
            raise RuntimeError("private provider details")
        return [1.0, 0.0]


class SearchOnlyVectors:
    def __init__(self, items=(), *, fail=False):
        self.items = list(items)
        self.calls = []
        self.fail = fail

    async def search(self, repository_id, query_vector, *, limit):
        self.calls.append(repository_id)
        if self.fail:
            raise RuntimeError("private vector details")
        return self.items[:limit]

    def __getattr__(self, name):
        if name in {"upsert_chunks", "replace_file", "delete_file", "clear_repository"}:
            raise AssertionError("Mission must never mutate the source index")
        raise AttributeError(name)


def indexed_chunk(root, repository_id, path, *, content=None, start=1, end=1):
    data = (root / path).read_bytes()
    return RetrievedChunk(chunk=CodeChunk(
        repository_id=repository_id, path=path, language="python",
        start_line=start, end_line=end,
        content=content or data.decode("utf-8").splitlines()[0],
        file_hash=hashlib.sha256(data).hexdigest()), score=0.9)


@pytest.fixture
def source_and_worktree(tmp_path, git_exe):
    source, worktree = tmp_path / "source", tmp_path / "task"
    source.mkdir()
    (source / "source.py").write_text("needle_value = 'source branch'\n", encoding="utf-8")
    (source / "common.py").write_text("needle_value = 'shared bytes'\n", encoding="utf-8")
    (source / "deleted.py").write_text("needle_value = 'removed in task'\n", encoding="utf-8")

    def git(*args):
        subprocess.run([git_exe, "-C", str(source), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "fixture")
    git("worktree", "add", "--detach", str(worktree), "HEAD")
    return source, worktree


@pytest.fixture
def make_manager():
    stores = []

    def create(embeddings, vectors, *, base_url="http://127.0.0.1:9999/v1", enabled=True):
        events = SqliteEventStore()
        stores.append(events)
        config = RagConfig(enabled=enabled, embedding_model="fixture",
                           embedding_base_url=base_url)
        return RagManager(RagRuntimeAvailability("unavailable", config=config),
            RagFileStore(events), embedding_provider=embeddings, vector_store=vectors)

    yield create
    for store in stores:
        store.close()


@pytest.mark.asyncio
async def test_source_index_candidates_are_rechecked_against_worktree_without_refresh(
        source_and_worktree, make_manager, rg_exe):
    source, worktree = source_and_worktree
    identity = repository_id_for(source)
    vectors = SearchOnlyVectors([indexed_chunk(source, identity, path)
        for path in ("source.py", "common.py", "deleted.py")])
    manager = make_manager(Embeddings(), vectors)
    for item in vectors.items:
        manager.file_store.upsert(IndexFileState(repository_id=identity,
            path=item.chunk.path, content_hash=item.chunk.file_hash,
            indexed_at=datetime.now(timezone.utc), chunk_count=1, status="indexed"))
    before = manager.file_store.list(identity)
    original_source = (source / "source.py").read_bytes()
    (worktree / "source.py").write_text("needle_value = 'task branch'\n", encoding="utf-8")
    (worktree / "new.py").write_text("needle_value = 'new task file'\n", encoding="utf-8")
    (worktree / "deleted.py").unlink()
    subject = build_mission_retrieval(manager, source, Repository(worktree, rg_exe),
                                     data_policy=DataPolicy())

    found = await subject.retrieve("needle_value")

    assert {item.path for item in found} == {"source.py", "common.py", "new.py"}
    assert all(item.file_hash == hashlib.sha256((worktree / item.path).read_bytes()).hexdigest()
               for item in found)
    assert all("source branch" not in item.content and "removed in task" not in item.content
               for item in found)
    assert next(item for item in found if item.path == "common.py").sources == ("literal", "vector")
    assert all(item.sources == ("literal",) for item in found if item.path != "common.py")
    assert subject.last_trace.stale_chunk_filtered == 2
    assert subject.last_trace.incremental_update_triggered == 0
    assert "mission_index_read_only" in subject.status()["fallback_reasons"]
    assert vectors.calls == [identity]
    assert manager.file_store.list(identity) == before
    assert manager._index_states == {}
    assert (source / "source.py").read_bytes() == original_source


@pytest.mark.asyncio
async def test_vector_payload_content_is_never_returned_and_invalid_ranges_are_omitted(
        source_and_worktree, make_manager, rg_exe):
    source, worktree = source_and_worktree
    identity = repository_id_for(source)
    vectors = SearchOnlyVectors([
        indexed_chunk(source, identity, "source.py", content="untrusted stale index payload"),
        indexed_chunk(source, identity, "common.py", start=9, end=10)])
    subject = build_mission_retrieval(make_manager(Embeddings(), vectors), source,
        Repository(worktree, rg_exe), data_policy=DataPolicy())

    found = await subject.retrieve("concept with no literal match", mode="vector")

    assert [item.path for item in found] == ["source.py"]
    assert found[0].content == "needle_value = 'source branch'"
    assert found[0].line == found[0].end_line == 1
    assert subject.last_trace.stale_chunk_filtered == 1
    assert "stale_chunk_filtered" in subject.status()["fallback_reasons"]


@pytest.mark.asyncio
async def test_literal_search_finds_new_task_files_even_with_full_vector_candidates(
        source_and_worktree, make_manager, rg_exe):
    source, worktree = source_and_worktree
    identity = repository_id_for(source)
    items = []
    for number in range(20):
        name = f"unrelated{number}.py"
        (source / name).write_text("unrelated = 1\n", encoding="utf-8")
        (worktree / name).write_text("unrelated = 1\n", encoding="utf-8")
        items.append(indexed_chunk(source, identity, name))
    (worktree / "task_only.py").write_text("def task_symbol(): return 1\n", encoding="utf-8")
    subject = build_mission_retrieval(make_manager(Embeddings(), SearchOnlyVectors(items)),
        source, Repository(worktree, rg_exe), data_policy=DataPolicy())

    found = await subject.retrieve("Find task_symbol")

    assert found[0].path == "task_only.py"
    assert found[0].sources == ("literal",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason", [("embedding", "embedding_failed"), ("vector", "qdrant_failed")])
async def test_optional_provider_failures_return_current_text_with_reason(
        source_and_worktree, make_manager, rg_exe, failure, reason):
    source, worktree = source_and_worktree
    embeddings = Embeddings(fail=failure == "embedding")
    vectors = SearchOnlyVectors(fail=failure == "vector")
    statuses = []
    subject = build_mission_retrieval(make_manager(embeddings, vectors), source,
        Repository(worktree, rg_exe), data_policy=DataPolicy(), on_status=statuses.append)

    found = await asyncio.create_task(subject.retrieve("needle_value"))

    assert found and all(item.sources == ("literal",) for item in found)
    assert embeddings.queries == ["needle_value"]
    assert len(vectors.calls) == (failure == "vector")
    assert reason in subject.status()["fallback_reasons"]
    assert subject.status()["used"] is True
    assert statuses == [subject.status()]
    assert "private" not in str(statuses)


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,allowed,calls", [
    ("https://embedding.invalid/v1", False, 0),
    ("https://embedding.invalid/v1", True, 1),
    ("http://localhost:9999/v1", False, 1),
    ("http://[::1]:9999/v1", False, 1),
    ("http://127.0.0.1:9999/v1", False, 1),
    ("https://localhost.attacker.invalid/v1", False, 0),
])
async def test_embedding_requires_configured_endpoint_and_task_cloud_policy(
        source_and_worktree, make_manager, rg_exe, base_url, allowed, calls):
    source, worktree = source_and_worktree
    embeddings = Embeddings()
    subject = build_mission_retrieval(make_manager(embeddings, SearchOnlyVectors(), base_url=base_url),
        source, Repository(worktree, rg_exe), data_policy=DataPolicy(cloud_model_allowed=allowed))

    assert await subject.retrieve("needle_value")

    assert len(embeddings.queries) == calls
    assert ("embedding_policy_denied" in subject.status()["fallback_reasons"]) == (not calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("available,reason", [(False, "rag_unavailable"), (True, "rag_disabled")])
async def test_disabled_or_missing_manager_preserves_task_literal_fallback(
        source_and_worktree, make_manager, rg_exe, available, reason):
    source, worktree = source_and_worktree
    embeddings = Embeddings()
    manager = make_manager(embeddings, SearchOnlyVectors(), enabled=False) if available else None
    subject = build_mission_retrieval(manager, source, Repository(worktree, rg_exe),
                                     data_policy=DataPolicy(cloud_model_allowed=True))

    assert subject.status()["requested"] is True
    assert subject.status()["enabled"] is False
    assert reason in subject.status()["fallback_reasons"]
    assert await subject.retrieve("needle_value")
    assert not embeddings.queries
    assert reason in subject.status()["fallback_reasons"]
