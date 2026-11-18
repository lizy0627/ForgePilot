"""Local R5/R6 timing fixtures: temporary files and in-memory SQLite only.

Run with agent.venv/Scripts/python.exe scripts/profile_hotpaths.py --output result.json.
No model/MCP calls, credentials, user database, or business fault injection. Output
contains timing samples and cache sizes, never cache keys or values. No fixed-time
assertions: compare samples on the same machine, outside concurrent test/build runs.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from forgepilot.context import TaskContext, TaskState
from forgepilot.process import executable
from forgepilot.recording import now
from forgepilot.repository import Repository
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.task_service import TaskService
from forgepilot.trace import TraceEvent

MIB = 1024 * 1024


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def search_samples(rg):
    # Same names/content/limits and five unprimed runs as the saved R5 probe.
    with tempfile.TemporaryDirectory(prefix="forgepilot-profile-") as directory:
        fixture = Path(directory).resolve()
        assert fixture.parent == Path(tempfile.gettempdir()).resolve()
        content = b"int small_fixture = 1;\n" * 10
        for index in range(200):
            (fixture / f"source{index:03}.cpp").write_bytes(content)
        manifest = {p.name: sha256(p) for p in sorted(fixture.iterdir())}
        repo = Repository(fixture, rg)
        actual = asyncio.create_subprocess_exec
        counts = {"all": 0, "rg": 0}

        async def counted(*args, **kwargs):
            counts["all"] += 1
            counts["rg"] += Path(args[0]).name.casefold() in {"rg", "rg.exe"}
            return await actual(*args, **kwargs)

        samples = []
        asyncio.create_subprocess_exec = counted
        try:
            for index in range(5):
                counts.update(all=0, rg=0)
                started = perf_counter()
                result = json.loads(await repo.search_code("needle_not_present"))
                elapsed = (perf_counter() - started) * 1000
                assert result == {"matches": [], "truncated": False, "limits": [],
                                  "skipped_non_utf8_or_binary": 0}
                samples.append({"run": index + 1, "elapsed_ms": elapsed,
                                "search_subprocesses": counts["all"], "rg_subprocesses": counts["rg"]})
        finally:
            asyncio.create_subprocess_exec = actual
        assert manifest == {p.name: sha256(p) for p in sorted(fixture.iterdir())}
        return {"fixture": {"files": 200, "lines_per_file": 10, "bytes_per_file": len(content),
                            "query": "needle_not_present", "repetitions": 5, "warmup": 0},
                "fixture_manifest": manifest, "samples": samples,
                "median_ms": statistics.median(s["elapsed_ms"] for s in samples),
                "timing_scope": "Repository.search_code including enumeration, path/handle checks, reads, matching and JSON; setup, hashes, git opening and rg version excluded",
                "subprocess_counter": "asyncio.create_subprocess_exec during search only"}


def compiler_log(size, number=0):
    line = f"normal compiler output fixture {number:02d}\n"
    return (line * (size // len(line) + 1))[:size]


def occupancy(redactor):
    # Read only bounded numeric diagnostics; never serialize the cache itself.
    with redactor._cache_lock:
        entries, charged = len(redactor._cache), redactor._cache_bytes
        assert entries <= 16 and 0 <= charged <= 4 * MIB
        return {"entries": entries, "charged_bytes": charged}


def snapshot_scenario(name, batches, *, reset=False, prime=False):
    store = SqliteEventStore(":memory:")
    try:
        context = TaskContext(str(ROOT), "synthetic R6 benchmark", state=TaskState.ANALYZING)
        service = TaskService.__new__(TaskService)
        service._context, service._runtime, service._changes = context, None, None
        service.redactor = Redactor()
        host = TaskHost(lambda _: None, store=store)
        stamp = now()
        entry = SimpleNamespace(service=service, initial_context=context, created_at=stamp, updated_at=stamp,
                                finished_at=None, cancel_requested=False, terminal_traces=[], persistence_error=None)
        store.create(context.task_id, host._snapshot(entry))  # Admit without priming large text.
        sequence = 0

        def publish(logs):
            nonlocal sequence
            context.build_result = {"commands": [{"stdout": value} for value in logs]}
            context.validation_history = [context.build_result]
            sequence += 1
            trace = TraceEvent(sequence, context.task_id, "llm_response", now(),
                               {"step": sequence, "message": {"content": "small"}})
            started = perf_counter()
            host._publish(entry, trace)
            return (perf_counter() - started) * 1000

        warmup = publish(batches[0]) if prime else None
        samples = []
        for batch in batches:
            if reset:
                service.redactor, store.redactor = Redactor(), Redactor()
            elapsed = publish(batch)
            samples.append({"elapsed_ms": elapsed, "service_cache": occupancy(service.redactor),
                            "store_cache": occupancy(store.redactor)})
        saved = store.get(context.task_id)
        assert saved["build_result"] == context.build_result
        assert saved["validation_history"] == context.validation_history
        return {"scenario": name, "fresh_redactors_each_sample": reset, "warmup_ms": warmup,
                "log_characters_per_sample": [[len(value) for value in batch] for batch in batches],
                "log_sha256_per_sample": [[hashlib.sha256(value.encode()).hexdigest() for value in batch] for batch in batches],
                "samples": samples, "median_ms": statistics.median(s["elapsed_ms"] for s in samples)}
    finally:
        store.close()


def snapshot_samples():
    large = compiler_log(MIB)
    cases = [snapshot_scenario("cold_1m", [[large]] * 5, reset=True),
             snapshot_scenario("warm_1m", [[large]] * 5, prime=True),
             snapshot_scenario("alternating_1m", [[compiler_log(MIB, i % 3)] for i in range(6)]),
             snapshot_scenario("working_set_3m", [[compiler_log(MIB, i) for i in range(3)]] * 3),
             snapshot_scenario("oversized_3m", [[compiler_log(3 * MIB)]] * 3),
             snapshot_scenario("twenty_distinct_4k", [[compiler_log(4096, i)] for i in range(20)])]
    return {"fixture": {"database": ":memory:", "copies": ["build_result", "validation_history"],
                        "event": "small llm_response", "data": "deterministic ASCII compiler logs"},
            "cases": cases, "cache_limits_per_instance": {"entries": 16, "charged_bytes": 4 * MIB},
            "timing_scope": "TaskHost._publish including snapshot redaction and SQLite publish; setup, log construction, hashing and verification excluded",
            "cold_definition": "New service/store Redactor before each cold sample; OS caches are not flushed",
            "warm_definition": "One unmeasured publication of the same log precedes five measured publications; warmup_ms is reported separately"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON to a new file; refuses to overwrite existing evidence")
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new evidence file")
    started = datetime.now(timezone.utc).isoformat()
    rg = executable(None, "rg")  # Retain the project's existing rg configuration requirement.
    environment = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                   "processor": platform.processor(), "cpu_count": os.cpu_count()}
    search = asyncio.run(search_samples(rg))
    search["rg"] = rg
    search["rg_version"] = subprocess.run([rg, "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    data = {"started_utc": started, "environment": environment, "search": search, "snapshots": snapshot_samples(),
            "sources_sha256": {name: sha256(ROOT / name) for name in (
                "agent/forgepilot/repository.py", "agent/forgepilot/security.py", "agent/forgepilot/task_host.py",
                "agent/forgepilot/sqlite_store.py", "scripts/profile_hotpaths.py")},
            "finished_utc": datetime.now(timezone.utc).isoformat()}
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
