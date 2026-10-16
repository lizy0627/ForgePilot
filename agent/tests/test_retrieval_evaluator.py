"""The offline benchmark is retrieval evidence, never scripted Agent success."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

from forgepilot_eval.retrieval import (
    DeterministicEmbeddingProvider, MODES, REQUIRED_CATEGORIES,
    load_retrieval_tasks, parser, summarize_mode,
)
from forgepilot_eval.runner import Benchmark, parser as agent_parser


def test_retrieval_manifest_has_confirmed_coverage_and_existing_paths():
    tasks = load_retrieval_tasks()
    root = Path(__file__).resolve().parents[2]
    assert len(tasks) >= 20
    assert {task.category for task in tasks} == REQUIRED_CATEGORIES
    assert len({task.id for task in tasks}) == len(tasks)
    for task in tasks:
        for path in task.expected_paths:
            source = root / path
            assert source.is_file()
            assert source.resolve().is_relative_to(root.resolve())
        for symbol in task.expected_symbols:
            assert any(symbol in (root / path).read_text(encoding="utf-8")
                       for path in task.expected_paths)


def test_deterministic_embedding_is_local_stable_and_bounded():
    provider = DeterministicEmbeddingProvider(128)
    first = provider.embed("RepositoryIndexer index_repository 中文索引")
    second = provider.embed("RepositoryIndexer index_repository 中文索引")
    different = provider.embed("unrelated token")
    assert first == second
    assert len(first) == 128 and first != different
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_retrieval_metrics_and_three_mode_cli_are_explicit():
    rows = [
        {"recall_at_5": 1.0, "recall_at_10": 1.0, "reciprocal_rank_at_10": 1.0,
         "latency_ms": 1.0, "chunk_count": 2, "context_chars": 100},
        {"recall_at_5": 0.0, "recall_at_10": 0.5, "reciprocal_rank_at_10": 0.25,
         "latency_ms": 9.0, "chunk_count": 4, "context_chars": 300},
    ]
    metrics = summarize_mode(rows)
    assert metrics == {
        "task_count": 2, "recall_at_5": 0.5, "recall_at_10": 0.75,
        "mrr_at_10": 0.625, "average_latency_ms": 5.0,
        "p50_latency_ms": 1.0, "p95_latency_ms": 9.0,
        "average_chunk_count": 3.0, "average_context_chars": 200.0,
    }
    assert tuple(parser().parse_args(["--mode", mode]).mode for mode in MODES) == MODES


def test_agent_benchmark_rag_switch_is_real_model_only(tmp_path, rg_exe, git_exe):
    off = agent_parser().parse_args(["--mode", "offline", "--rag", "off"])
    assert off.rag == "off"
    on = agent_parser().parse_args([
        "--mode", "offline", "--rag", "on", "--rg", rg_exe, "--git", git_exe,
        "--output-root", str(tmp_path)])
    with pytest.raises(ValueError, match="real"):
        Benchmark(on)
