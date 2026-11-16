"""Offline execution timings, separate from rendered WorkflowPage measurements.

Run with agent.venv/Scripts/python.exe scripts/profile_workflow_execution.py
--output result.json after other tests/builds settle. Uses the existing delivery
fixture with real Git, CMake, CTest and Protected Oracle, plus deterministic fake
providers. Requires the development test dependencies and the Windows C++ kit.
No real model, embedding or connection-detection request is sent. Observations
are not pass/fail performance thresholds or estimates of real model latency.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "agent/tests")]

from closed_loop_support import make_loop_case
from forgepilot.orchestration import (
    GraphDefinition, GraphRunner, NodeDefinition, NodeResult, NodeStatus, NodeType,
)
from forgepilot.orchestration.node_handlers import HandlerRegistry
from forgepilot.process import executable
from forgepilot.sqlite_store import SqliteEventStore


def source_identity() -> dict:
    sources = []
    for name in ("forgepilot.orchestration.runner", "forgepilot.models.gateway", "forgepilot.agents.delivery",
                 "forgepilot.agents.implementation", "forgepilot.validation", "closed_loop_support"):
        source = Path(import_module(name).__file__).resolve()
        assert source.is_relative_to(ROOT)
        sources.append({"module": name, "path": str(source), "sha256": sha256(source.read_bytes()).hexdigest()})
    return {"root": str(ROOT), "modules": sources}


def summary(samples: list[float], scope: str) -> dict:
    ordered = sorted(samples)
    return {
        "scope": scope,
        "samples": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[(95 * len(ordered) + 99) // 100 - 1], 3),
        "raw_ms": [round(value, 3) for value in samples],
    }


def orchestration_samples(root: Path, iterations: int) -> dict:
    graph = GraphDefinition("profile.local-orchestration.v1", 1, tuple(
        NodeDefinition(f"step-{index}", NodeType.DETERMINISTIC, "noop",
                       dependencies=(f"step-{index - 1}",) if index else ())
        for index in range(8)
    ))
    registry = HandlerRegistry({"noop": lambda context: NodeResult(NodeStatus.SUCCEEDED)})
    store = SqliteEventStore(root / "local-orchestration.sqlite3")
    samples = []
    try:
        runner = GraphRunner(store, registry)
        for index in range(iterations):
            started = perf_counter()
            snapshot = runner.run(f"profile-local-{index}", graph)
            samples.append((perf_counter() - started) * 1000)
            assert snapshot.status is NodeStatus.SUCCEEDED
            assert len(snapshot.nodes) == 8
    finally:
        store.close()
    return summary(samples, "GraphRunner.run of eight serial no-op deterministic nodes, including graph validation, "
                   "SQLite state/event transactions and snapshots; excludes database open, models, tools and builds. "
                   "Direct local orchestration baseline, not a residual computed from parallel node durations.")


class TimedFixtureProvider:
    """Instrument the existing fixture provider without altering gateway rules."""
    def __init__(self, provider, role: str):
        self.provider, self.role = provider, role
        self.calls = []

    async def chat(self, messages, tools=None):
        started = perf_counter()
        await asyncio.sleep(0.025)
        reply = await self.provider.chat(messages, tools)
        self.calls.append({"role": self.role, "duration_ms": (perf_counter() - started) * 1000})
        return reply

    async def aclose(self):
        await self.provider.aclose()


async def delivery_sample(root: Path, git: str) -> dict:
    case = await make_loop_case(root, git)
    # prepare() stops at the execution gate and does not call a model. The
    # existing provider factory reads this dict when the approved graph starts.
    for role, provider in tuple(case.providers.items()):
        assert not provider.seen
        case.providers[role] = TimedFixtureProvider(provider, role)
    try:
        started = perf_counter()
        result = await case.service.approve(case.approval)
        approved_ms = (perf_counter() - started) * 1000
        assert result.status is NodeStatus.SUCCEEDED, result
        reports = case.artifacts("ValidationReport")
        assert len(reports) == 1 and reports[0].content.status == "passed"
        assert reports[0].content.hidden_oracle == "passed"
        assert len(case.artifacts("DeliveryBundle")) == 1
        calls = case.model_calls()
        assert len(calls) == 3 and all(item["error_code"] is None for item in calls)
        commands = []
        for command in reports[0].content.commands:
            if command.kind == "integrity":
                continue
            duration_ms = (datetime.fromisoformat(command.finished_at.replace("Z", "+00:00"))
                           - datetime.fromisoformat(command.started_at.replace("Z", "+00:00"))).total_seconds() * 1000
            commands.append({"kind": command.kind, "duration_ms": duration_ms, "exit_code": command.exit_code})
        assert {command["kind"] for command in commands} == {"configure", "build", "test", "protected_test"}
        return {
            "approved_pipeline_ms": approved_ms,
            "model_attempts": [{"role": call["role"], "duration_ms": call["duration_ms"]} for call in calls],
            "fixture_provider_calls": [call for provider in case.providers.values() for call in provider.calls],
            "validation_commands": commands,
            "node_metrics": [{"node_id": node.node_id, "status": node.status.value,
                              "duration_ms": node.result.metrics.duration_ms if node.result else None}
                             for node in result.nodes if node.status is not NodeStatus.SKIPPED],
        }
    finally:
        await case.close()


async def delivery_samples(root: Path, iterations: int) -> dict:
    git = executable(None, "git")
    runs = []
    for index in range(iterations):
        directory = root / f"delivery-{index}"
        directory.mkdir()
        runs.append(await delivery_sample(directory, git))
    commands = [command for run in runs for command in run["validation_commands"]]
    return {
        "approved_pipeline": summary([run["approved_pipeline_ms"] for run in runs],
            "Explicit approval to completed delivery: baseline capture, fake model/tool calls, real configure/build/CTest/Oracle, "
            "independent fake review, checkpoints and Git delivery. Excludes fixture setup and human wait."),
        "model_wait": summary([call["duration_ms"] for run in runs for call in run["model_attempts"]],
            "Existing durable ModelGateway attempt duration_ms; each deterministic fixture provider awaits an injected 25ms delay. "
            "Measures offline waiting and gateway request processing, not real inference latency."),
        "fixture_provider_wait": summary([call["duration_ms"] for run in runs for call in run["fixture_provider_calls"]],
            "perf_counter around injected 25ms sleep plus existing fixture response; no network."),
        "validation_commands": {kind: summary([item["duration_ms"] for item in commands if item["kind"] == kind],
            "ValidationReport started_at/finished_at wall time around command preparation, isolated process and cleanup; "
            + ("Oracle configure/build/test remain grouped as protected_test; no hidden output included."
               if kind == "protected_test" else "post-edit public " + kind + "; initial baseline configure is in baseline_capture node_metrics."))
            for kind in ("configure", "build", "test", "protected_test")},
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new JSON path; refuses overwrite")
    parser.add_argument("--iterations", type=int, choices=range(1, 21), default=3, metavar="1..20")
    parser.add_argument("--scheduler-iterations", type=int, choices=range(1, 101), default=20, metavar="1..100")
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("output already exists")
    source = source_identity()
    with tempfile.TemporaryDirectory(prefix="fp-workflow-profile-") as directory:
        root = Path(directory)
        measurements = {"local_orchestration": orchestration_samples(root, arguments.scheduler_iterations),
                        "delivery": asyncio.run(delivery_samples(root, arguments.iterations))}
    document = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "mode": "offline providers, disposable local Git/CMake/CTest/Protected Oracle fixture",
        "source": source,
        "measurements": measurements,
        "limitations": ["No production model latency or token effectiveness measurement", "No rendered UI measurement",
                        "Serial fixture paths; do not subtract these times from parallel Mission elapsed time",
                        "Three delivery runs by default; P95 is a descriptive observation, not a stable release threshold"],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as destination:
        json.dump(document, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(f"saved={arguments.output}")


if __name__ == "__main__":
    main()
