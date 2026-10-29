"""Deterministic hybrid retrieval, stale filtering and graceful degradation."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from forgepilot.rag import (CodeChunk, QueryBuilder, RagConfig, RetrievalQuery,
                            RetrievalService, RetrievedChunk)
from forgepilot.repository import Repository
from forgepilot.security import Redactor


class QueryEmbeddings:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.queries = []

    async def embed_query(self, text):
        self.queries.append(text)
        if self.fail:
            raise RuntimeError("private provider failure")
        return [1.0, 0.0]

    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class MemorySearchVectors:
    def __init__(self, items=None, *, fail=False):
        self.items = items or {}
        self.fail = fail
        self.calls = []

    async def search(self, repository_id, query_vector, *, limit=8):
        self.calls.append((repository_id, list(query_vector), limit))
        if self.fail:
            raise RuntimeError("private qdrant failure")
        return list(self.items.get(repository_id, ()))[:limit]


class FakeIntelligence:
    def __init__(self, definitions=(), references=()):
        self.definitions = list(definitions)
        self.references = list(references)
        self.calls = []

    async def query(self, operation, **arguments):
        self.calls.append((operation, arguments))
        await asyncio.sleep(0)
        rows = self.definitions if operation == "find_symbol" else self.references
        return json.dumps({"source": "clangd", "locations": rows})


class FallbackIntelligence:
    async def query(self, operation, **arguments):
        return json.dumps({
            "source": "rg", "semantic": False,
            "fallback_reason": "clangd_unavailable",
            "locations": [],
            "text_matches": [{"path": arguments["path"], "line": arguments["line"]}],
        })


def config(*, top_k=8, max_chars=24_000):
    return RagConfig(
        enabled=True,
        embedding_model="fixture",
        embedding_base_url="https://provider.invalid/v1",
        retrieval_top_k=top_k,
        context_max_chars=max_chars,
    )


def repository(root):
    return Repository(root.resolve(), "rg")


def chunk(root, repository_id, path, *, start=1, end=None, symbol=None,
          file_hash=None):
    data = (root / path).read_bytes()
    text = data.decode("utf-8")
    lines = text.split("\n")
    end = end or len(lines)
    return CodeChunk(
        repository_id=repository_id,
        path=path,
        language="python" if path.endswith(".py") else "cpp",
        symbol=symbol,
        symbol_type="function" if symbol else None,
        start_line=start,
        end_line=end,
        content="\n".join(lines[start - 1:end]),
        file_hash=file_hash or hashlib.sha256(data).hexdigest(),
    )


def result(value, score, rank):
    return RetrievedChunk(chunk=value, score=score, rank=rank)


@pytest.mark.asyncio
async def test_retrieval_redacts_source_and_remote_query(tmp_path):
    secret = "fixture-query-credential"
    (tmp_path / "source.py").write_text(f"API_KEY = '{secret}'\n", encoding="utf-8")
    embeddings = QueryEmbeddings()
    subject = RetrievalService("repository-a", repository(tmp_path), config(),
        embeddings, MemorySearchVectors(), redactor=Redactor((secret,)))
    results = await subject.retrieve(f"Find API_KEY={secret}")
    assert secret not in str(embeddings.queries)
    assert secret not in "\n".join(item.content for item in results)
    assert results


@pytest.mark.asyncio
async def test_retrieval_suppresses_legacy_secret_path_and_symbol(tmp_path):
    secret = "opaqueProviderValue8473625"
    secret_path = secret + ".py"
    (tmp_path / secret_path).write_text("def target_symbol():\n    return 1\n", encoding="utf-8")
    (tmp_path / "safe.py").write_text("def target_symbol():\n    return 2\n", encoding="utf-8")
    old_path_chunk = chunk(tmp_path, "repository-a", secret_path)
    old_symbol_chunk = CodeChunk.model_validate({
        **chunk(tmp_path, "repository-a", "safe.py").model_dump(exclude={"chunk_id"}),
        "symbol": secret,
    })
    vectors = MemorySearchVectors({"repository-a": [
        result(old_path_chunk, 0.99, 1), result(old_symbol_chunk, 0.98, 2)]})
    subject = RetrievalService("repository-a", repository(tmp_path), config(),
        QueryEmbeddings(), vectors, redactor=Redactor((secret,)))
    found = await subject.retrieve("target_symbol")
    assert found and all(item.path == "safe.py" for item in found)
    assert secret not in str([(item.path, item.symbol, item.content) for item in found])


def test_query_builder_extracts_paths_extensions_symbols_and_errors():
    terms = QueryBuilder.build(
        "Update src/core/http_client.py class HttpClient and call parse_response(); "
        "handle HTTP_500 and ValueError in tests\\test_http_client.py")
    assert terms.paths == ("src/core/http_client.py", "tests/test_http_client.py")
    assert terms.extensions == (".py",)
    assert {"HttpClient", "parse_response", "HTTP_500", "ValueError"} <= set(terms.symbols)
    assert terms.error_identifiers == ("HTTP_500", "ValueError")


def test_query_builder_does_not_treat_dotted_symbol_as_file_extension():
    terms = QueryBuilder.build("Where is RetrievalService.retrieve implemented?")
    assert terms.paths == () and terms.extensions == ()
    assert "RetrievalService" in terms.symbols


@pytest.mark.asyncio
async def test_rrf_ranking_is_deterministic_and_uses_full_query(tmp_path):
    (tmp_path / "alpha.py").write_text("needle_value = 1\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("needle_value = 2\n", encoding="utf-8")
    embeddings = QueryEmbeddings()
    vectors = MemorySearchVectors({"repository-a": [
        result(chunk(tmp_path, "repository-a", "beta.py"), 0.9, 1),
        result(chunk(tmp_path, "repository-a", "alpha.py"), 0.8, 2),
    ]})
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), embeddings, vectors)
    task = "Find needle_value and explain the complete behavior"

    first = await subject.retrieve(task)
    second = await subject.retrieve(task)

    # The documented 0.40 vector weight is intentionally greater than the
    # 0.35 literal weight, so the vector rank-one result wins this symmetric case.
    assert [item.path for item in first] == ["beta.py", "alpha.py"]
    assert [(item.path, item.score, item.reasons) for item in first] == [
        (item.path, item.score, item.reasons) for item in second]
    assert embeddings.queries == [task, task]
    assert all({"literal_rank:1", "vector_rank:2"} <= set(item.reasons)
               for item in first if item.path == "alpha.py")


@pytest.mark.asyncio
async def test_exact_symbol_match_ranks_before_vector_only_result(tmp_path):
    (tmp_path / "exact.py").write_text(
        "def target_symbol():\n    return 1\n", encoding="utf-8")
    (tmp_path / "semantic.py").write_text(
        "def unrelated():\n    return 2\n", encoding="utf-8")
    vectors = MemorySearchVectors({"repository-a": [
        result(chunk(tmp_path, "repository-a", "semantic.py", symbol="unrelated"), 0.99, 1),
    ]})
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(), vectors)

    found = await subject.retrieve("Fix target_symbol failure")

    assert found[0].path == "exact.py"
    assert found[0].symbol == "target_symbol"
    assert "exact_symbol" in found[0].reasons


@pytest.mark.asyncio
async def test_vector_recalls_semantic_result_with_different_keywords(tmp_path):
    (tmp_path / "permissions.py").write_text(
        "def may_enter(user):\n    return user.is_admin\n", encoding="utf-8")
    vector = result(chunk(
        tmp_path, "repository-a", "permissions.py", symbol="may_enter"), 0.95, 1)
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(),
        MemorySearchVectors({"repository-a": [vector]}))

    found = await subject.retrieve("repair authorization policy")

    assert [item.path for item in found] == ["permissions.py"]
    assert found[0].reasons == ("vector_rank:1",)


@pytest.mark.asyncio
async def test_overlapping_chunks_are_merged_and_context_budget_is_enforced(tmp_path):
    (tmp_path / "large.py").write_text(
        "first line\nsecond line\nthird line\nfourth line\n", encoding="utf-8")
    vectors = MemorySearchVectors({"repository-a": [
        result(chunk(tmp_path, "repository-a", "large.py", start=1, end=4), 0.9, 1),
        result(chunk(tmp_path, "repository-a", "large.py", start=2, end=4), 0.8, 2),
    ]})
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(max_chars=18),
        QueryEmbeddings(), vectors)

    found = await subject.retrieve("explain behavior")

    assert len(found) == 1
    assert len(found[0].content) == 18
    assert "context_truncated" in found[0].reasons


@pytest.mark.asyncio
async def test_stale_vector_chunk_is_rejected(tmp_path):
    (tmp_path / "stale.py").write_text("current = True\n", encoding="utf-8")
    (tmp_path / "fresh.py").write_text("fresh = True\n", encoding="utf-8")
    stale = chunk(
        tmp_path, "repository-a", "stale.py", file_hash="0" * 64)
    fresh = chunk(tmp_path, "repository-a", "fresh.py")
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(),
        MemorySearchVectors({"repository-a": [result(stale, 0.99, 1), result(fresh, 0.8, 2)]}))

    found = await subject.retrieve("semantic request")

    assert [item.path for item in found] == ["fresh.py"]


@pytest.mark.asyncio
async def test_stale_vector_records_trace_and_triggers_one_incremental_refresh(tmp_path):
    (tmp_path / "stale.py").write_text("current = True\n", encoding="utf-8")
    stale = chunk(tmp_path, "repository-a", "stale.py", file_hash="0" * 64)
    refreshed = []

    def refresh(path):
        refreshed.append(path)
        return True

    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(),
        MemorySearchVectors({"repository-a": [result(stale, 0.99, 1)]}),
        stale_handler=refresh)

    found = await subject.retrieve("semantic request")

    assert found == [] and refreshed == ["stale.py"]
    assert subject.last_trace.status == "degraded"
    assert subject.last_trace.stale_chunk_filtered == 1
    assert subject.last_trace.incremental_update_triggered == 1
    assert {"stale_chunk_filtered", "incremental_update_triggered"} <= set(
        subject.last_trace.events)


@pytest.mark.asyncio
async def test_results_retain_repository_hash_sources_and_weighted_score_trace(tmp_path):
    (tmp_path / "target.py").write_text("needle_value = 1\n", encoding="utf-8")
    value = chunk(tmp_path, "repository-a", "target.py")
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(),
        MemorySearchVectors({"repository-a": [result(value, 0.9, 1)]}))

    found = await subject.retrieve("Find needle_value")

    item = found[0]
    assert item.repository_id == "repository-a"
    assert item.file_hash == value.file_hash
    assert item.sources == ("literal", "vector")
    traces = {entry.source: entry for entry in item.score_trace}
    assert traces["literal"].weight == 0.35
    assert traces["vector"].weight == 0.40
    assert traces["literal"].contribution == pytest.approx(0.35 / 61)
    assert traces["vector"].contribution == pytest.approx(0.40 / 61)
    assert item.score == pytest.approx(sum(entry.contribution for entry in item.score_trace))


@pytest.mark.asyncio
async def test_same_file_limit_and_configured_path_scope_apply_before_packing(tmp_path):
    rows = [f"line_{index}" for index in range(1, 13)]
    (tmp_path / "many.py").write_text("\n".join(rows), encoding="utf-8")
    (tmp_path / "protected.py").write_text("protected_symbol = 1\n", encoding="utf-8")
    values = [
        result(chunk(tmp_path, "repository-a", "many.py", start=line, end=line),
               1.0 - line / 100, rank)
        for rank, line in enumerate((1, 3, 5, 7), 1)
    ]
    values.append(result(chunk(tmp_path, "repository-a", "protected.py"), 0.99, 5))
    scoped = RagConfig(
        enabled=True, embedding_model="fixture",
        embedding_base_url="https://provider.invalid/v1",
        exclude=("protected.py",), retrieval_top_k=8,
        retrieval_max_chunks_per_file=2, context_max_chars=24_000)
    subject = RetrievalService(
        "repository-a", repository(tmp_path), scoped, QueryEmbeddings(),
        MemorySearchVectors({"repository-a": values}))

    found = await subject.retrieve("semantic request")

    assert [item.path for item in found] == ["many.py", "many.py"]
    assert sum(len(item.content) for item in found) <= 24_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "embeddings,vectors,intelligence,expected_event,expected_sources",
    [
        (None, MemorySearchVectors(), FakeIntelligence(
            definitions=[{"path": "main.cpp", "line": 1, "end_line": 1}]),
         "embedding_unavailable", {"literal", "clangd"}),
        (QueryEmbeddings(), None, FakeIntelligence(
            definitions=[{"path": "main.cpp", "line": 1, "end_line": 1}]),
         "qdrant_unavailable", {"literal", "clangd"}),
        (QueryEmbeddings(), MemorySearchVectors(), FallbackIntelligence(),
         "clangd_unavailable", {"literal"}),
        (None, None, None, "literal_only", {"literal"}),
    ],
    ids=("no-embedding", "no-qdrant", "no-clangd", "literal-only"),
)
async def test_optional_component_degradation_is_explicit_and_non_blocking(
        tmp_path, embeddings, vectors, intelligence, expected_event, expected_sources):
    (tmp_path / "main.cpp").write_text(
        "int target_symbol() { return 1; }\n", encoding="utf-8")
    subject = RetrievalService(
        "repository-a", repository(tmp_path),
        RagConfig(enabled=True), embeddings, vectors,
        code_intelligence=intelligence)

    found = await subject.retrieve("Fix target_symbol()")

    assert found and found[0].path == "main.cpp"
    assert set(found[0].sources) == expected_sources
    assert subject.last_trace.status == "degraded"
    assert expected_event in subject.last_trace.events


@pytest.mark.asyncio
async def test_dirty_file_uses_current_literal_content_not_vector(tmp_path):
    (tmp_path / "dirty.py").write_text(
        "def target_symbol():\n    return 'current'\n", encoding="utf-8")
    value = chunk(tmp_path, "repository-a", "dirty.py", symbol="target_symbol")

    class DirtyStore:
        def get(self, repository_id, path):
            assert (repository_id, path) == ("repository-a", "dirty.py")
            return SimpleNamespace(status="pending")

    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(),
        MemorySearchVectors({"repository-a": [result(value, 0.99, 1)]}),
        file_store=DirtyStore())

    found = await subject.retrieve("Fix target_symbol")

    assert [item.path for item in found] == ["dirty.py"]
    assert found[0].content == "def target_symbol():"
    assert "literal_rank:1" in found[0].reasons
    assert not any(reason.startswith("vector_rank:") for reason in found[0].reasons)


@pytest.mark.asyncio
async def test_path_and_language_filters_are_applied_to_all_sources(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "match.py").write_text(
        "target_symbol = 1\n", encoding="utf-8")
    (tmp_path / "match.cpp").write_text(
        "int target_symbol = 2;\n", encoding="utf-8")
    vectors = MemorySearchVectors({"repository-a": [
        result(chunk(tmp_path, "repository-a", "match.cpp"), 0.99, 1),
        result(chunk(tmp_path, "repository-a", "src/match.py"), 0.8, 2),
    ]})
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(), vectors)

    found = await subject.retrieve(
        "Find target_symbol", path_filters=("src",), languages=("python",))

    assert [item.path for item in found] == ["src/match.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["embedding", "qdrant"])
async def test_vector_failure_degrades_to_literal_and_clangd(tmp_path, failure):
    (tmp_path / "main.cpp").write_text(
        "int target_symbol() { return 1; }\n", encoding="utf-8")
    embeddings = QueryEmbeddings(fail=failure == "embedding")
    vectors = MemorySearchVectors(fail=failure == "qdrant")
    intelligence = FakeIntelligence(
        definitions=[{"path": "main.cpp", "line": 1, "end_line": 1}])
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), embeddings, vectors,
        code_intelligence=intelligence)

    found = await subject.retrieve("Fix target_symbol()")

    assert [item.path for item in found] == ["main.cpp"]
    assert {"exact_symbol", "literal_rank:1", "clangd_definition"} <= set(found[0].reasons)
    assert {call[0] for call in intelligence.calls} == {"find_symbol", "find_references"}


@pytest.mark.asyncio
async def test_repository_scoping_is_preserved_during_parallel_retrieval(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "same.py").write_text("ONE_ONLY = True\n", encoding="utf-8")
    (two / "same.py").write_text("TWO_ONLY = True\n", encoding="utf-8")
    vectors = MemorySearchVectors({
        "repository-one": [result(chunk(one, "repository-one", "same.py"), 0.9, 1)],
        "repository-two": [result(chunk(two, "repository-two", "same.py"), 0.9, 1)],
    })
    first = RetrievalService(
        "repository-one", repository(one), config(), QueryEmbeddings(), vectors)
    second = RetrievalService(
        "repository-two", repository(two), config(), QueryEmbeddings(), vectors)

    first_results, second_results = await asyncio.gather(
        first.retrieve("semantic request"), second.retrieve("semantic request"))

    assert first_results[0].content == "ONE_ONLY = True\n"
    assert second_results[0].content == "TWO_ONLY = True\n"
    assert {call[0] for call in vectors.calls} == {"repository-one", "repository-two"}


@pytest.mark.asyncio
async def test_path_plan_and_test_association_reasons_explain_boosts(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "widget.py").write_text("def build_widget():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_widget.py").write_text(
        "def test_build_widget():\n    pass\n", encoding="utf-8")
    vectors = MemorySearchVectors({"repository-a": [
        result(chunk(tmp_path, "repository-a", "src/widget.py", symbol="build_widget"), 0.9, 1),
        result(chunk(tmp_path, "repository-a", "tests/test_widget.py", symbol="test_build_widget"), 0.8, 2),
    ]})
    subject = RetrievalService(
        "repository-a", repository(tmp_path), config(), QueryEmbeddings(), vectors)

    found = await subject.retrieve(
        "Update src/widget.py build_widget", planned_files=("tests/test_widget.py",))
    reasons = {item.path: set(item.reasons) for item in found}

    assert {"path_match", "implementation_test_association"} <= reasons["src/widget.py"]
    assert {"planned_file", "implementation_test_association"} <= reasons["tests/test_widget.py"]


@pytest.mark.asyncio
async def test_disabled_service_is_inert(tmp_path):
    subject = RetrievalService(
        "repository-a", repository(tmp_path), RagConfig(), None, None)
    assert not subject.available
    assert await subject.retrieve("anything") == []


def test_explicit_query_limits_override_configuration(tmp_path):
    query = RetrievalQuery(
        repository_id="repository-a", query="semantic request", top_k=1,
        context_max_chars=5)
    assert query.top_k == 1 and query.context_max_chars == 5
