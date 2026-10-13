"""Compare candidate chat-model latency; offline simulation is the safe default.

Real provider calls require both --real-model and at least one --model. API keys
are read only from FORGEPILOT_API_KEY and are never accepted as arguments or
written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from forgepilot.llm import ContentDelta, OpenAICompatibleClient, StreamFinished
from forgepilot.settings import checked_model_id


FIXED_PROMPTS = (
    "Reply with one short sentence explaining why the sky appears blue.",
    "List exactly three practical ways to organize a small desk.",
    "用一句简短中文说明什么是缓存。",
)


def percentile(values: list[int], proportion: float) -> int:
    if not values:
        return -1
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * proportion) - 1)]


def distribution(values: list[int]) -> dict[str, int]:
    return {"p50": percentile(values, .5), "p95": percentile(values, .95),
            "sample_count": len(values)}


async def real_candidate(base_url, key, model, repeats, timeout):
    rows = []
    async with OpenAICompatibleClient(base_url, key, model, timeout=timeout) as client:
        for _ in range(repeats):
            for prompt in FIXED_PROMPTS:
                started = monotonic(); first = None; characters = 0; success = False
                try:
                    async for event in client.stream_chat([{"role": "user", "content": prompt}]):
                        if isinstance(event, ContentDelta) and event.content:
                            if first is None:
                                first = monotonic()
                            characters += len(event.content)
                        elif isinstance(event, StreamFinished):
                            success = bool(characters)
                    finished = monotonic()
                except Exception:
                    finished = monotonic()
                rows.append({"success": success,
                             "ttft_ms": round(((first or finished) - started) * 1000),
                             "duration_ms": round((finished - started) * 1000),
                             "response_chars": characters})
    return rows


async def offline_candidate(index, repeats):
    rows = []
    first_delay = 12 + index * 9
    completion_delay = 7 + index * 4
    for _ in range(repeats):
        for prompt in FIXED_PROMPTS:
            started = monotonic()
            await asyncio.sleep(first_delay / 1000)
            first = monotonic()
            characters = 18 + len(prompt) % 17 + index * 5
            await asyncio.sleep(completion_delay / 1000)
            rows.append({"success": True, "ttft_ms": round((first - started) * 1000),
                         "duration_ms": round((monotonic() - started) * 1000),
                         "response_chars": characters})
    return rows


def summarize(model, rows):
    successful = [row for row in rows if row["success"]]
    return {
        "model": model,
        "samples": len(rows),
        "successful_samples": len(successful),
        "success_rate": round(len(successful) / len(rows), 4) if rows else 0,
        "first_token_ms": distribution([row["ttft_ms"] for row in successful]),
        "complete_ms": distribution([row["duration_ms"] for row in successful]),
        "response_chars": distribution([row["response_chars"] for row in successful]),
    }


async def run(args):
    if args.real_model:
        base_url = os.environ.get("FORGEPILOT_BASE_URL", "")
        key = os.environ.get("FORGEPILOT_API_KEY", "")
        if not base_url or not key or not args.models:
            raise SystemExit("--real-model requires FORGEPILOT_BASE_URL, FORGEPILOT_API_KEY and --model")
        models = [checked_model_id(value, "candidate") for value in args.models]
        if any(model is None for model in models):
            raise SystemExit("candidate model identifiers must be non-empty")
        if len(set(models)) != len(models):
            raise SystemExit("candidate models must be unique")
        results = []
        for model in models:
            results.append(summarize(model, await real_candidate(base_url, key, model, args.repeats, args.timeout)))
    else:
        if args.models:
            raise SystemExit("--model is used only with the explicit --real-model switch")
        models = ["offline-low-latency", "offline-high-quality"]
        results = [summarize(model, await offline_candidate(index, args.repeats))
                   for index, model in enumerate(models)]
    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": "chat_model_latency_real" if args.real_model else "chat_model_latency_offline",
        "external_model_enabled": int(args.real_model),
        "fixed_prompt_count": len(FIXED_PROMPTS),
        "repeats": args.repeats,
        "models": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true",
                        help="Explicitly permit external requests and possible provider charges")
    parser.add_argument("--model", action="append", dest="models",
                        help="Candidate chat model identifier; repeat for each candidate")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Repetitions of the three fixed prompts (1..20)")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be 1..20")
    if not 0 < args.timeout <= 600:
        parser.error("timeout must be in (0, 600] seconds")
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new report path")
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        if isinstance(exc, SystemExit):
            raise
        raise SystemExit("benchmark failed; no credentials, URL, prompt, or provider body was reported") from None
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
