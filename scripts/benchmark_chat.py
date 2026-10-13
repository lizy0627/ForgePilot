"""Repeatable ordinary-chat benchmark with an offline model by default.

Run:
  agent.venv/Scripts/python.exe scripts/benchmark_chat.py --samples 30

No network, credentials, repository, MCP server, or real model is used unless
--real-model is explicitly supplied. Reports contain aggregate numeric metrics
only; prompts, answers, request IDs, URLs, tool data, and credentials are absent.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import platform
import sys
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from forgepilot.api import create_app
from forgepilot.chat_service import ChatService
from forgepilot.llm import ContentDelta, OpenAICompatibleClient, StreamFinished
from forgepilot.sqlite_store import SqliteEventStore


class OfflineModel:
    def __init__(self, first_token_delay_ms: int, chunk_delay_ms: int):
        self.first_token_delay_ms = first_token_delay_ms
        self.chunk_delay_ms = chunk_delay_ms

    async def stream_chat(self, messages, tools=None):
        await asyncio.sleep(self.first_token_delay_ms / 1000)
        yield ContentDelta("offline")
        await asyncio.sleep(self.chunk_delay_ms / 1000)
        yield ContentDelta(" answer")
        yield StreamFinished("stop", None)

    async def aclose(self):
        return None


class MetricCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.rows: list[dict] = []

    def emit(self, record):
        value = getattr(record, "stream_metrics", None)
        if isinstance(value, dict):
            self.rows.append(value)


def percentile(values: list[int], proportion: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * proportion) - 1)]


def distribution(values: list[int]) -> dict[str, int]:
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
        "sample_count": len(values),
    }


async def run(args) -> dict:
    if args.real_model:
        base_url = os.environ.get("FORGEPILOT_BASE_URL", "")
        api_key = os.environ.get("FORGEPILOT_API_KEY", "")
        model = os.environ.get("FORGEPILOT_CHAT_MODEL") or os.environ.get("FORGEPILOT_MODEL", "")
        if not all((base_url, api_key, model)):
            raise SystemExit("--real-model requires FORGEPILOT_BASE_URL, FORGEPILOT_API_KEY and a chat or fallback model")

        def chat_factory():
            return ChatService(OpenAICompatibleClient(base_url, api_key, model, timeout=args.timeout))
    else:
        def chat_factory():
            return ChatService(OfflineModel(args.first_token_delay_ms, args.chunk_delay_ms))

    logger = logging.getLogger("forgepilot.chat_performance")
    collector = MetricCollector()
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    store = SqliteEventStore(":memory:")
    app = create_app(lambda trace: None, store=store, chat_factory=chat_factory, mcp_disabled=True)
    prompt = "Return a short deterministic acknowledgement."

    async def sample(client):
        conversation = (await client.post("/api/v1/conversations", json={})).json()
        request_id = str(uuid4())
        response = await client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/requests",
            json={"request_id": request_id, "mode": "chat", "tool_mode": "auto", "prompt": prompt})
        response.raise_for_status()
        while True:
            state = (await client.get(
                f"/api/v1/conversations/{conversation['conversation_id']}/requests/{request_id}"))
            state.raise_for_status()
            if state.json()["status"] != "running":
                break
            await asyncio.sleep(0.001)

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                for _ in range(args.warmup):
                    await sample(client)
                while len(collector.rows) < args.warmup:
                    await asyncio.sleep(0)
                collector.rows.clear()
                for _ in range(args.samples):
                    await sample(client)
                while len(collector.rows) < args.samples:
                    await asyncio.sleep(0)
    finally:
        store.close()
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    rows = collector.rows
    fields = (
        "request_total_ms", "request_to_first_safe_delta_ms", "request_prepare_ms",
        "context_prepare_ms", "summary_count", "summary_total_ms", "repository_open_ms",
        "repository_cache_hit",
        "summary_queued", "summary_started", "summary_ms",
        "llm_round_count", "exposed_tool_count", "tool_call_count", "tool_total_ms", "request_input_chars",
        "stream_output_chars", "terminal_persist_ms",
    )
    result = {name: distribution([int(row[name]) for row in rows]) for name in fields}
    llm_rounds = [round_metrics for row in rows for round_metrics in row["llm_rounds"]]
    result["llm_ttft_ms"] = distribution([item["llm_ttft_ms"] for item in llm_rounds])
    result["llm_duration_ms"] = distribution([item["llm_duration_ms"] for item in llm_rounds])
    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": "ordinary_chat_real_model" if args.real_model else "ordinary_chat_offline",
        "external_model_enabled": int(args.real_model),
        "configuration": {
            "samples": args.samples,
            "warmup": args.warmup,
            "first_token_delay_ms": -1 if args.real_model else args.first_token_delay_ms,
            "chunk_delay_ms": -1 if args.real_model else args.chunk_delay_ms,
            "request_mode": 1,
            "tool_mode": 4,
            "project_tools_enabled": 0,
            "mcp_enabled": 0,
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "metrics": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--first-token-delay-ms", type=int, default=25)
    parser.add_argument("--chunk-delay-ms", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--real-model", action="store_true",
                        help="Explicitly allow paid/external model requests using FORGEPILOT_* settings")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000 or not 0 <= args.warmup <= 100:
        parser.error("samples must be 1..1000 and warmup must be 0..100")
    if not 0 <= args.first_token_delay_ms <= 10_000 or not 0 <= args.chunk_delay_ms <= 10_000:
        parser.error("offline delays must be 0..10000 ms")
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new report path")
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
