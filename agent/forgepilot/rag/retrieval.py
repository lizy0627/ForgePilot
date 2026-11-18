"""Deterministic hybrid retrieval over literal, vector and clangd evidence.

Repository content returned by this module is untrusted data. It is ranked and
packed as evidence only; no source text or comment is interpreted as an Agent
instruction.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import inspect
from pathlib import Path
import re
from typing import Literal

from ..compile_database import CPP_SUFFIXES
from ..repository import Repository
from ..security import Redactor
from .config import RagConfig, rag_path_matches
from .embedding import EmbeddingProvider
from .models import (RetrievalQuery, RetrievalResult, RetrievalScoreTrace,
                     RetrievalSourceTrace, RetrievalTrace, RetrievedChunk,
                     checked_relative_path, checked_repository_id)
from .vector_store import VectorStore


_SOURCE_LIMIT = 20
_OVERLAP_RATIO = 0.8
_SYMBOL_BOOST = 0.020
_PATH_BOOST = 0.012
_PLANNED_FILE_BOOST = 0.015
_TEST_ASSOCIATION_BOOST = 0.006
_PATH = re.compile(
    r"(?<![\w.])(?:[A-Za-z0-9_.-]+[\\/])*[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,11}")
_EXTENSION = re.compile(r"(?<!\w)\.[A-Za-z][A-Za-z0-9]{0,11}\b")
_DECLARED_SYMBOL = re.compile(
    r"(?:\b(?:class|function|method|func|def|struct|enum|interface)\s+|"
    r"(?:函数|类|方法)[：:\s]*)([A-Za-z_$][\w$]*)",
    re.IGNORECASE,
)
_CALL_SYMBOL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_SNAKE_CASE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_CAMEL_CASE = re.compile(
    r"\b(?:[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)\b")
_ERROR_IDENTIFIER = re.compile(
    r"\b(?:[A-Z]{1,8}\d{3,6}|[A-Za-z_$][\w$]*(?:Error|Exception)|[A-Z][A-Z0-9_]{2,})\b")
_WORD = re.compile(r"\b[A-Za-z][A-Za-z0-9-]{2,}\b")
_IGNORED_CALLS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "sizeof", "assert",
    "print", "len", "range",
})
_IGNORED_WORDS = frozenset({
    "add", "and", "bug", "call", "change", "class", "code", "complete", "error",
    "explain", "failure", "file", "find", "fix", "for", "from", "function", "handle",
    "implement", "into", "issue", "method", "remove", "repair", "src", "test", "tests",
    "the", "this", "update", "with",
})
_LANGUAGES = {
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".h": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".py": "python",
    ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".cs": "csharp",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".kt": "kotlin",
    ".kts": "kotlin", ".rb": "ruby", ".php": "php", ".lua": "lua",
    ".sql": "sql", ".sh": "shell", ".ps1": "powershell", ".cmake": "cmake",
    ".qml": "qml",
}
_INDEXABLE_EXTENSIONS = frozenset((*_LANGUAGES, ".txt"))

RetrievalMode = Literal["lexical", "vector", "hybrid"]


def _unique(values: Iterable[str], *, limit: int = 64) -> tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
            if len(result) >= limit:
                break
    return tuple(result)


@dataclass(frozen=True, slots=True)
class QueryTerms:
    """Bounded terms extracted without an LLM or any network request."""

    paths: tuple[str, ...]
    extensions: tuple[str, ...]
    symbols: tuple[str, ...]
    error_identifiers: tuple[str, ...]
    keywords: tuple[str, ...] = ()

    @property
    def literal_terms(self) -> tuple[str, ...]:
        return _unique((*self.symbols, *self.error_identifiers, *self.keywords), limit=16)


class QueryBuilder:
    """Deterministically extract code-shaped terms from task text."""

    @staticmethod
    def build(task_text: str) -> QueryTerms:
        # RetrievalQuery provides the shared strict text bounds without storing
        # or logging the task body.
        query = RetrievalQuery(repository_id="query-builder", query=task_text)
        text = query.query

        paths = []
        for match in _PATH.finditer(text):
            candidate = match.group().replace("\\", "/")
            if Path(candidate).suffix.casefold() not in _INDEXABLE_EXTENSIONS:
                continue
            try:
                paths.append(checked_relative_path(candidate))
            except ValueError:
                continue
        extensions = [Path(path).suffix.casefold() for path in paths if Path(path).suffix]
        extensions.extend(
            match.group().casefold() for match in _EXTENSION.finditer(text)
            if match.group().casefold() in _INDEXABLE_EXTENSIONS)

        positioned: list[tuple[int, int, str]] = []
        for priority, pattern, group in (
                (0, _DECLARED_SYMBOL, 1), (1, _CALL_SYMBOL, 1),
                (2, _SNAKE_CASE, 0), (3, _CAMEL_CASE, 0),
                (4, _ERROR_IDENTIFIER, 0)):
            for match in pattern.finditer(text):
                value = match.group(group)
                if priority == 1 and value.casefold() in _IGNORED_CALLS:
                    continue
                positioned.append((match.start(group), priority, value))
        positioned.sort(key=lambda item: (item[0], item[1], item[2]))
        symbols = _unique(item[2] for item in positioned)
        errors = _unique(match.group() for match in _ERROR_IDENTIFIER.finditer(text))
        reserved = {*symbols, *errors}
        keywords = _unique(
            match.group() for match in _WORD.finditer(text)
            if match.group() not in reserved and match.group().casefold() not in _IGNORED_WORDS)
        return QueryTerms(
            paths=_unique(paths),
            extensions=_unique(extensions),
            symbols=symbols,
            error_identifiers=errors,
            keywords=keywords,
        )


@dataclass(slots=True)
class _Candidate:
    path: str
    language: str
    line: int
    end_line: int
    symbol: str | None
    content: str
    file_hash: str
    contributions: dict[str, float] = field(default_factory=dict)
    ranks: dict[str, int] = field(default_factory=dict)
    reasons: set[str] = field(default_factory=set)

    @property
    def score(self) -> float:
        return sum(self.contributions.values())


@dataclass(frozen=True, slots=True)
class RetrievalSourceOutcome:
    """Result contract for a bounded literal, vector, or semantic source."""

    source: Literal["literal", "vector", "clangd"]
    status: Literal["available", "unavailable", "failed", "not_applicable"]
    candidates: tuple[_Candidate, ...] = ()
    reason: str | None = None
    stale_count: int = 0
    stale_paths: tuple[str, ...] = ()


# Existing retrieval internals keep their original name; adapters use the
# explicit public result contract above.
_SourceOutcome = RetrievalSourceOutcome


class RetrievalService:
    """Optional repository-scoped hybrid retrieval with deterministic ranking."""

    def __init__(self, repository_id: str, repository: Repository, config: RagConfig,
                 embedding_provider: EmbeddingProvider | None,
                 vector_store: VectorStore | None, *, code_intelligence=None,
                 file_store=None, rrf_k: int = 60,
                 stale_handler: Callable[[str], bool | None | Awaitable[bool | None]] | None = None,
                 redactor: Redactor | None = None) -> None:
        if type(rrf_k) is not int or not 1 <= rrf_k <= 10_000:
            raise ValueError("rrf_k must be an integer between 1 and 10000")
        self.repository_id = checked_repository_id(repository_id)
        self.repository = repository
        self.config = RagConfig.model_validate(config)
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.code_intelligence = code_intelligence
        self.file_store = file_store
        self.rrf_k = rrf_k
        self.stale_handler = stale_handler
        self.redactor = redactor or Redactor()
        self._trace: ContextVar[RetrievalTrace | None] = ContextVar(
            f"rag_retrieval_trace_{id(self)}", default=None)

    @property
    def available(self) -> bool:
        return self.config.enabled

    @property
    def last_trace(self) -> RetrievalTrace | None:
        return self._trace.get()

    def build_query(self, task_text: str) -> QueryTerms:
        return QueryBuilder.build(task_text)

    def _snapshot(self, path: str,
                  snapshots: dict[str, tuple[str, str] | None]) -> tuple[str, str] | None:
        try:
            normalized = checked_relative_path(self.repository.normalize_plan_path(path))
            if not rag_path_matches(self.config, normalized):
                return None
        except Exception:
            return None
        if normalized in snapshots:
            return snapshots[normalized]
        try:
            text, truncated, content_hash = self.repository._read_snapshot(normalized)
            value = None if truncated or content_hash is None else (text, content_hash)
        except Exception:
            value = None
        snapshots[normalized] = value
        return value

    @staticmethod
    def _line_content(text: str, line: int, end_line: int) -> str | None:
        rows = [row.removesuffix("\r") for row in text.split("\n")]
        if not 1 <= line <= end_line <= len(rows):
            return None
        content = "\n".join(rows[line - 1:end_line])
        return content or None

    def _rrf(self, source: Literal["literal", "vector", "clangd"], rank: int) -> float:
        weight = {
            "literal": self.config.literal_weight,
            "vector": self.config.vector_weight,
            "clangd": self.config.clangd_weight,
        }[source]
        return weight / (self.rrf_k + rank)

    @staticmethod
    def _language(path: str) -> str:
        candidate = Path(path)
        if candidate.name.casefold() == "cmakelists.txt":
            return "cmake"
        return _LANGUAGES.get(candidate.suffix.casefold(), "text")

    def _path_filters(self, values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or len(values) > 32:
            raise ValueError("path_filters must contain at most 32 repository paths")
        normalized = []
        for value in values:
            target = self.repository._path(value)
            relative = target.relative_to(self.repository.root).as_posix()
            normalized.append("." if relative == "." else checked_relative_path(relative))
        return _unique(normalized, limit=32)

    @staticmethod
    def _languages(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or len(values) > 32:
            raise ValueError("languages must contain at most 32 language names")
        checked = []
        for value in values:
            if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]{0,31}", value)):
                raise ValueError("languages must contain bounded language names")
            checked.append(value.casefold())
        return _unique(checked, limit=32)

    @staticmethod
    def _matches_filters(candidate: _Candidate, paths: Sequence[str],
                         languages: Sequence[str]) -> bool:
        folded = candidate.path.casefold()
        path_match = not paths or any(
            path == "." or folded == path.casefold()
            or folded.startswith(path.casefold().rstrip("/") + "/")
            for path in paths)
        return path_match and (not languages or candidate.language.casefold() in languages)

    async def _literal(self, terms: QueryTerms, path_filters: Sequence[str],
                       snapshots: dict[str, tuple[str, str] | None]) -> _SourceOutcome:
        candidates = []
        suffixes = set(terms.extensions) if terms.extensions else None
        try:
            for term in terms.literal_terms:
                for scope in path_filters or (".",):
                    remaining = _SOURCE_LIMIT - len(candidates)
                    if remaining <= 0:
                        break
                    payload = json.loads(await self.repository.search_code(
                        term, path=scope, max_results=remaining, file_suffixes=suffixes,
                        skip_directory_globs=self.config.exclude))
                    rows = payload.get("matches", []) if isinstance(payload, dict) else []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        path, line = row.get("path"), row.get("line")
                        if not isinstance(path, str) or type(line) is not int:
                            continue
                        snapshot = self._snapshot(path, snapshots)
                        if snapshot is None:
                            continue
                        content = self._line_content(snapshot[0], line, line)
                        if content is None or term not in content:
                            continue
                        exact = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", content)
                        symbol = term if exact and term in terms.symbols else None
                        rank = len(candidates) + 1
                        candidates.append(_Candidate(
                            path=checked_relative_path(path), language=self._language(path),
                            line=line, end_line=line, symbol=symbol, content=content,
                            file_hash=snapshot[1],
                            contributions={"literal": self._rrf("literal", rank)},
                            ranks={"literal": rank},
                            reasons={f"literal_rank:{rank}"},
                        ))
                        if len(candidates) >= _SOURCE_LIMIT:
                            break
                if len(candidates) >= _SOURCE_LIMIT:
                    break
            return _SourceOutcome("literal", "available", tuple(candidates))
        except asyncio.CancelledError:
            raise
        except Exception:
            return _SourceOutcome(
                "literal", "failed", tuple(candidates), "literal_failed")

    async def _vector(self, task_text: str,
                      snapshots: dict[str, tuple[str, str] | None]) -> _SourceOutcome:
        if self.embedding_provider is None:
            return _SourceOutcome("vector", "unavailable", reason="embedding_unavailable")
        if self.vector_store is None:
            return _SourceOutcome("vector", "unavailable", reason="qdrant_unavailable")
        try:
            # Query Builder keeps the original text for local ranking; the
            # external embedding boundary receives only sanitized text.
            try:
                vector = await self.embedding_provider.embed_query(self.redactor.text(task_text))
            except asyncio.CancelledError:
                raise
            except Exception:
                return _SourceOutcome("vector", "failed", reason="embedding_failed")
            try:
                raw = await self.vector_store.search(
                    self.repository_id, vector, limit=_SOURCE_LIMIT)
            except asyncio.CancelledError:
                raise
            except Exception:
                return _SourceOutcome("vector", "failed", reason="qdrant_failed")
            checked = []
            for item in raw:
                try:
                    result = RetrievedChunk.model_validate(item)
                    if result.chunk.repository_id == self.repository_id:
                        checked.append(result)
                except Exception:
                    continue
            checked.sort(key=lambda item: (
                -item.score, item.chunk.path.casefold(), item.chunk.start_line,
                item.chunk.end_line, item.chunk.chunk_id))
            candidates = []
            stale_paths = []
            stale_count = 0
            for result in checked[:_SOURCE_LIMIT]:
                chunk = result.chunk
                if not rag_path_matches(self.config, chunk.path):
                    continue
                if self.file_store is not None:
                    state = self.file_store.get(self.repository_id, chunk.path)
                    if state is not None and state.status != "indexed":
                        continue
                snapshot = self._snapshot(chunk.path, snapshots)
                if snapshot is None or snapshot[1] != chunk.file_hash:
                    stale_count += 1
                    stale_paths.append(chunk.path)
                    continue
                content = self._line_content(snapshot[0], chunk.start_line, chunk.end_line)
                if content is None:
                    stale_count += 1
                    stale_paths.append(chunk.path)
                    continue
                rank = len(candidates) + 1
                candidates.append(_Candidate(
                    path=chunk.path, language=chunk.language,
                    line=chunk.start_line, end_line=chunk.end_line,
                    symbol=chunk.symbol, content=content,
                    file_hash=snapshot[1],
                    contributions={"vector": self._rrf("vector", rank)},
                    ranks={"vector": rank},
                    reasons={f"vector_rank:{rank}"},
                ))
            return _SourceOutcome(
                "vector", "available", tuple(candidates),
                stale_count=stale_count,
                stale_paths=_unique(stale_paths, limit=_SOURCE_LIMIT))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Embedding and Qdrant are optional. Their failure cannot prevent
            # current literal/clangd evidence from being returned.
            return _SourceOutcome("vector", "failed", reason="vector_failed")

    async def _clangd(self, terms: QueryTerms, path_filters: Sequence[str],
                      snapshots: dict[str, tuple[str, str] | None]) -> _SourceOutcome:
        if not terms.symbols:
            return _SourceOutcome("clangd", "not_applicable", reason="no_symbol_query")
        if self.code_intelligence is None:
            return _SourceOutcome("clangd", "unavailable", reason="clangd_unavailable")
        symbol = terms.symbols[0]
        try:
            location = None
            for scope in path_filters or (".",):
                seed = json.loads(await self.repository.search_code(
                    symbol, path=scope, max_results=_SOURCE_LIMIT, file_suffixes=CPP_SUFFIXES,
                    skip_directory_globs=self.config.exclude))
                for row in seed.get("matches", []):
                    text = row.get("text")
                    if not isinstance(text, str):
                        continue
                    match = re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", text)
                    if match:
                        location = (row.get("path"), row.get("line"), match.start() + 1)
                        break
                if location is not None:
                    break
            if (location is None or not isinstance(location[0], str)
                    or type(location[1]) is not int):
                return _SourceOutcome("clangd", "available")

            common = dict(path=location[0], line=location[1], column=location[2],
                          max_results=_SOURCE_LIMIT)
            definition, references = await asyncio.gather(
                self.code_intelligence.query("find_symbol", **common),
                self.code_intelligence.query(
                    "find_references", **common, include_declaration=True),
                return_exceptions=True,
            )
            combined = []
            seen = set()
            fallback_reasons = []
            semantic_responses = 0
            for relation, response in (("definition", definition), ("reference", references)):
                if isinstance(response, BaseException):
                    fallback_reasons.append("clangd_failed")
                    continue
                payload = json.loads(response)
                if payload.get("source") != "clangd" or payload.get("semantic") is False:
                    fallback_reasons.append(str(payload.get("fallback_reason") or "clangd_unavailable"))
                    continue
                semantic_responses += 1
                source_rows = payload.get("locations", [])
                if not isinstance(source_rows, list):
                    fallback_reasons.append("clangd_failed")
                    continue
                source_rows = sorted(
                    (row for row in source_rows if isinstance(row, dict)),
                    key=lambda row: (
                        str(row.get("path", "")).casefold(),
                        row.get("line") if type(row.get("line")) is int else 2**31,
                        row.get("end_line") if type(row.get("end_line")) is int else 2**31,
                    ))
                for row in source_rows:
                    path, line = row.get("path"), row.get("line")
                    end_line = row.get("end_line", line)
                    if (not isinstance(path, str) or type(line) is not int
                            or type(end_line) is not int):
                        continue
                    key = (path.casefold(), line, end_line)
                    if key in seen:
                        continue
                    seen.add(key)
                    combined.append((relation, path, line, end_line))
                    if len(combined) >= _SOURCE_LIMIT:
                        break
                if len(combined) >= _SOURCE_LIMIT:
                    break

            candidates = []
            for rank, (relation, path, line, end_line) in enumerate(combined, 1):
                snapshot = self._snapshot(path, snapshots)
                if snapshot is None:
                    continue
                content = self._line_content(snapshot[0], line, end_line)
                if content is None:
                    continue
                candidates.append(_Candidate(
                    path=checked_relative_path(path), language=self._language(path),
                    line=line, end_line=end_line,
                    symbol=symbol, content=content,
                    file_hash=snapshot[1],
                    contributions={"clangd": self._rrf("clangd", rank)},
                    ranks={"clangd": rank},
                    reasons={f"clangd_{relation}", f"clangd_rank:{rank}"},
                ))
            if not semantic_responses:
                reason = sorted(fallback_reasons)[0] if fallback_reasons else "clangd_unavailable"
                return _SourceOutcome("clangd", "unavailable", reason=reason)
            if fallback_reasons:
                return _SourceOutcome(
                    "clangd", "failed", tuple(candidates), sorted(fallback_reasons)[0])
            return _SourceOutcome("clangd", "available", tuple(candidates))
        except asyncio.CancelledError:
            raise
        except Exception:
            return _SourceOutcome("clangd", "failed", reason="clangd_failed")

    @staticmethod
    def _combine_exact(candidates: Sequence[_Candidate]) -> list[_Candidate]:
        combined: dict[tuple, _Candidate] = {}
        for candidate in candidates:
            key = (candidate.path.casefold(), candidate.line, candidate.end_line,
                   candidate.symbol.casefold() if candidate.symbol else None)
            current = combined.get(key)
            if current is None:
                combined[key] = candidate
                continue
            for source, score in candidate.contributions.items():
                current.contributions[source] = max(
                    score, current.contributions.get(source, 0.0))
            for source, rank in candidate.ranks.items():
                current.ranks[source] = min(rank, current.ranks.get(source, rank))
            current.reasons.update(candidate.reasons)
        return list(combined.values())

    @staticmethod
    def _highly_overlapping(first: _Candidate, second: _Candidate) -> bool:
        overlap = min(first.end_line, second.end_line) - max(first.line, second.line) + 1
        if overlap <= 0:
            return False
        shorter = min(first.end_line - first.line + 1, second.end_line - second.line + 1)
        return overlap / shorter >= _OVERLAP_RATIO

    def _merge_overlaps(self, candidates: Sequence[_Candidate],
                        snapshots: dict[str, tuple[str, str] | None]) -> list[_Candidate]:
        grouped: dict[str, list[_Candidate]] = {}
        for candidate in self._combine_exact(candidates):
            grouped.setdefault(candidate.path.casefold(), []).append(candidate)
        merged = []
        for values in grouped.values():
            values.sort(key=lambda item: (item.line, item.end_line, item.symbol or ""))
            for candidate in values:
                if not merged or merged[-1].path.casefold() != candidate.path.casefold() \
                        or not self._highly_overlapping(merged[-1], candidate):
                    merged.append(candidate)
                    continue
                current = merged[-1]
                current.line = min(current.line, candidate.line)
                current.end_line = max(current.end_line, candidate.end_line)
                if current.symbol is None:
                    current.symbol = candidate.symbol
                for source, score in candidate.contributions.items():
                    current.contributions[source] = max(
                        score, current.contributions.get(source, 0.0))
                for source, rank in candidate.ranks.items():
                    current.ranks[source] = min(rank, current.ranks.get(source, rank))
                current.reasons.update(candidate.reasons)
                snapshot = self._snapshot(current.path, snapshots)
                if snapshot is not None:
                    content = self._line_content(snapshot[0], current.line, current.end_line)
                    if content is not None:
                        current.content = content
        return merged

    @staticmethod
    def _is_test(path: str) -> bool:
        candidate = Path(path)
        stem = candidate.stem.casefold()
        parts = {part.casefold() for part in candidate.parts[:-1]}
        return ("test" in parts or "tests" in parts or stem.startswith("test_")
                or stem.endswith(("_test", "_tests", "_spec")))

    @classmethod
    def _base_stem(cls, path: str) -> str:
        stem = Path(path).stem.casefold()
        if stem.startswith("test_"):
            stem = stem[5:]
        for suffix in ("_tests", "_test", "_spec"):
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        return stem

    @classmethod
    def _associated(cls, first: str, second: str) -> bool:
        return (cls._is_test(first) != cls._is_test(second)
                and cls._base_stem(first) == cls._base_stem(second))

    def _boost(self, candidates: Sequence[_Candidate], terms: QueryTerms,
               planned_files: Sequence[str]) -> None:
        mentioned = {path.casefold() for path in terms.paths}
        planned = {path.casefold() for path in planned_files}
        anchors = tuple((*terms.paths, *planned_files))
        all_paths = tuple(candidate.path for candidate in candidates)
        for candidate in candidates:
            if candidate.symbol is not None and candidate.symbol in terms.symbols:
                candidate.contributions["boost_symbol"] = _SYMBOL_BOOST
                candidate.reasons.add("exact_symbol")
            folded = candidate.path.casefold()
            if any(folded == path or folded.endswith("/" + path) for path in mentioned):
                candidate.contributions["boost_path"] = _PATH_BOOST
                candidate.reasons.add("path_match")
            if folded in planned:
                candidate.contributions["boost_planned"] = _PLANNED_FILE_BOOST
                candidate.reasons.add("planned_file")
            related = any(self._associated(candidate.path, path) for path in anchors)
            if not related:
                related = any(
                    other.casefold() != folded and self._associated(candidate.path, other)
                    for other in all_paths)
            if related:
                candidate.contributions["boost_test"] = _TEST_ASSOCIATION_BOOST
                candidate.reasons.add("implementation_test_association")

    @staticmethod
    def _ranked(candidates: Sequence[_Candidate]) -> list[_Candidate]:
        return sorted(candidates, key=lambda item: (
            -item.score, item.path.casefold(), item.line, item.end_line,
            item.symbol or "", item.content))

    @staticmethod
    def _bounded_reasons(reasons: set[str]) -> tuple[str, ...]:
        ranks: dict[str, int] = {}
        plain = set()
        for reason in reasons:
            prefix, separator, value = reason.partition(":")
            if separator and prefix in {"literal_rank", "vector_rank", "clangd_rank"}:
                try:
                    rank = int(value)
                except ValueError:
                    plain.add(reason)
                else:
                    ranks[prefix] = min(rank, ranks.get(prefix, rank))
            else:
                plain.add(reason)
        normalized = plain | {f"{source}:{rank}" for source, rank in ranks.items()}
        return tuple(sorted(normalized))[:32]

    async def _trigger_stale_updates(self, paths: Sequence[str]) -> int:
        if self.stale_handler is None:
            return 0
        triggered = 0
        for path in _unique(paths, limit=_SOURCE_LIMIT):
            try:
                value = self.stale_handler(path)
                if inspect.isawaitable(value):
                    value = await value
                if value is not False:
                    triggered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        return triggered

    @staticmethod
    def _trace_status(outcomes: Sequence[_SourceOutcome], stale_count: int) -> str:
        return ("degraded" if stale_count or any(
            outcome.status in {"unavailable", "failed"} for outcome in outcomes)
                else "ready")

    def _score_trace(self, candidate: _Candidate) -> tuple[RetrievalScoreTrace, ...]:
        weights = {
            "literal": self.config.literal_weight,
            "vector": self.config.vector_weight,
            "clangd": self.config.clangd_weight,
        }
        order = {"literal": 0, "vector": 1, "clangd": 2}
        traces = []
        for source, contribution in sorted(
                candidate.contributions.items(), key=lambda item: (order.get(item[0], 10), item[0])):
            rank = candidate.ranks.get(source)
            weight = weights.get(source, 1.0)
            traces.append(RetrievalScoreTrace(
                source=source,
                rank=rank,
                weight=weight,
                rrf=(None if rank is None else 1.0 / (self.rrf_k + rank)),
                contribution=contribution,
            ))
        return tuple(traces)

    def _pack(self, candidates: Sequence[_Candidate], *, limit: int,
              max_chars: int) -> list[RetrievalResult]:
        results = []
        used = 0
        per_file: dict[str, int] = {}
        for candidate in candidates:
            if len(results) >= limit or used >= max_chars:
                break
            # Old vector payloads may predate index-time sanitization. Never
            # return a source identity that embeds a known credential.
            if self.redactor.text(candidate.path) != candidate.path:
                continue
            file_key = candidate.path.casefold()
            if per_file.get(file_key, 0) >= self.config.retrieval_max_chunks_per_file:
                continue
            remaining = max_chars - used
            content = self.redactor.text(candidate.content)
            end_line = candidate.end_line
            reasons = set(candidate.reasons)
            if len(content) > remaining:
                content = content[:remaining]
                end_line = min(end_line, candidate.line + content.count("\n"))
                reasons.add("context_truncated")
            if not content:
                break
            results.append(RetrievalResult(
                repository_id=self.repository_id,
                path=candidate.path,
                line=candidate.line,
                end_line=end_line,
                symbol=self.redactor.text(candidate.symbol) if candidate.symbol else None,
                score=candidate.score,
                reasons=RetrievalService._bounded_reasons(reasons),
                file_hash=candidate.file_hash,
                sources=tuple(source for source in ("literal", "vector", "clangd")
                              if source in candidate.contributions),
                score_trace=self._score_trace(candidate),
                content=content,
            ))
            used += len(content)
            per_file[file_key] = per_file.get(file_key, 0) + 1
        return results

    async def retrieve(self, task: str | RetrievalQuery, *,
                       planned_files: Sequence[str] = (), path_filters: Sequence[str] = (),
                       languages: Sequence[str] = (),
                       mode: RetrievalMode = "hybrid") -> list[RetrievalResult]:
        if not self.available:
            return []
        if mode not in {"lexical", "vector", "hybrid"}:
            raise ValueError("mode must be lexical, vector or hybrid")
        if isinstance(planned_files, (str, bytes)):
            raise ValueError("planned_files must be a sequence of repository paths")
        if isinstance(task, RetrievalQuery):
            query = RetrievalQuery.model_validate(task)
            if query.repository_id != self.repository_id:
                raise ValueError("RetrievalQuery belongs to a different repository")
        else:
            query = RetrievalQuery(
                repository_id=self.repository_id,
                query=task,
                top_k=self.config.retrieval_top_k,
                context_max_chars=self.config.context_max_chars,
            )
        normalized_plans = []
        for path in planned_files:
            try:
                normalized_plans.append(checked_relative_path(
                    self.repository.normalize_plan_path(path)))
            except Exception:
                continue

        normalized_filters = self._path_filters(path_filters)
        normalized_languages = self._languages(languages)

        terms = self.build_query(query.query)
        snapshots: dict[str, tuple[str, str] | None] = {}
        literal_task = (self._literal(terms, normalized_filters, snapshots)
                        if mode != "vector" else asyncio.sleep(
                            0, result=_SourceOutcome(
                                "literal", "not_applicable", reason="mode_excluded")))
        vector_task = (self._vector(query.query, snapshots)
                       if mode != "lexical" else asyncio.sleep(
                           0, result=_SourceOutcome(
                               "vector", "not_applicable", reason="mode_excluded")))
        clangd_task = (self._clangd(terms, normalized_filters, snapshots)
                       if mode != "vector" else asyncio.sleep(
                           0, result=_SourceOutcome(
                               "clangd", "not_applicable", reason="mode_excluded")))
        literal, vector, clangd = await asyncio.gather(
            literal_task, vector_task, clangd_task)
        outcomes = (literal, vector, clangd)
        stale_count = sum(outcome.stale_count for outcome in outcomes)
        triggered = await self._trigger_stale_updates(
            tuple(path for outcome in outcomes for path in outcome.stale_paths))
        filtered = [candidate for outcome in outcomes for candidate in outcome.candidates
                    if self._matches_filters(
                        candidate, normalized_filters, normalized_languages)]
        candidates = self._merge_overlaps(filtered, snapshots)
        self._boost(candidates, terms, normalized_plans)
        results = self._pack(
            self._ranked(candidates), limit=query.top_k,
            max_chars=query.context_max_chars)
        events = []
        for outcome in outcomes:
            if outcome.status in {"unavailable", "failed"} and outcome.reason:
                events.append(outcome.reason)
        if stale_count:
            events.append("stale_chunk_filtered")
            events.append("incremental_update_triggered" if triggered
                          else "incremental_update_unavailable")
        usable = {outcome.source for outcome in outcomes if outcome.status == "available"}
        if usable == {"literal"}:
            events.append("literal_only")
        self._trace.set(RetrievalTrace(
            mode=mode,
            status=self._trace_status(outcomes, stale_count),
            sources=tuple(RetrievalSourceTrace(
                source=outcome.source,
                status=outcome.status,
                candidate_count=len(outcome.candidates),
                reason=outcome.reason,
            ) for outcome in outcomes),
            events=_unique(events, limit=32),
            stale_chunk_filtered=stale_count,
            incremental_update_triggered=triggered,
            result_count=len(results),
            context_chars=sum(len(result.content) for result in results),
        ))
        return results
