"""Fixed 2 Bug Fix + 2 Feature acceptance, three rounds, one 300k token ledger.

Preparation is offline by default. --execute uses existing saved credentials;
every real model/embedding HTTP attempt goes through BudgetTransport. This does
not invoke or unlock the formal 15+5 A/B evaluator.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import httpx

from acceptance_budget import BudgetTransport, RequestBudget
from forgepilot.api import create_app
from forgepilot.desktop_configuration import load_saved_key, load_embedding_key, load_configuration, configuration_revision
from forgepilot.errors import ForgePilotError
from forgepilot.mission_factory import MissionDependencies, build_mission_workflow
from forgepilot.models.accounting import Accounting
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, SecretResolver
from forgepilot.presets import BuildConfig
from forgepilot.process import executable
from forgepilot.rag.embedding import OpenAICompatibleEmbeddingProvider
from forgepilot.sqlite_store import SqliteEventStore


TASKS = (
    {"id": "bug-negative-clamp", "kind": "bug_fix", "declaration": "int value(int);",
     "source": "int value(int input) { return input; }\n",
     "objective": "Fix value: negative integers return zero; zero and positive integers are unchanged.",
     "public": "value(-1)==0 && value(0)==0 && value(3)==3",
     "oracle": "value(-7)==0 && value(0)==0 && value(91)==91"},
    {"id": "bug-inclusive-sum", "kind": "bug_fix", "declaration": "int value(int);",
     "source": "int value(int n) { int total=0; for(int i=0;i<n;++i) total+=i; return total; }\n",
     "objective": "Fix value(n): sum integers from 1 through n inclusive; return zero for n <= 0. Inputs are within -100..100.",
     "public": "value(0)==0 && value(1)==1 && value(4)==10",
     "oracle": "value(-3)==0 && value(2)==3 && value(10)==55"},
    {"id": "feature-range-clamp", "kind": "feature", "declaration": "int value(int,int,int);",
     "source": "int value(int input, int lower, int upper) { return input; }\n",
     "objective": "Implement value(input,lower,upper): clamp to the inclusive bounds. lower <= upper is guaranteed. Keep the existing public API.",
     "public": "value(-2,0,5)==0 && value(2,0,5)==2 && value(9,0,5)==5",
     "oracle": "value(7,4,4)==4 && value(-8,-5,-1)==-5 && value(-3,-5,-1)==-3"},
    {"id": "feature-ascii-vowels", "kind": "feature", "declaration": "int value(const char*);",
     "source": "int value(const char* text) { return 0; }\n",
     "objective": "Implement value(text): count ASCII vowels a/e/i/o/u case-insensitively in a NUL-terminated string. A null pointer returns zero. Other bytes are not vowels.",
     "public": 'value("hello")==2 && value("AEIOU")==5 && value("")==0',
     "oracle": 'value(nullptr)==0 && value("rhythm")==0 && value("aEiOu xyz 123")==5'},
)


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def plan():
    return {"protocol": "product-readiness-v1", "formal_ab": False, "token_cap": 300_000,
            "rounds": 3, "planned_runs": 12, "order": "round then task order",
            "tasks": TASKS, "allowed_changed_files": ["src/value.cpp"],
            "success": ["actual CMake/CTest/Protected Oracle passed", "scope and integrity passed",
                        "independent review approved", "DeliveryBundle and explicit patch export",
                        "source repository unchanged"],
            "failure_policy": "retain every failed attempt; corrected reruns require separate evidence; denominator stays 12"}


def saved_configuration():
    identity = hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16].upper()
    path = Path(os.environ["LOCALAPPDATA"]) / "ForgePilot/launcher" / identity / "config.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    args = data["server_args"]
    if "--model-config" not in args:
        raise ValueError("Acceptance requires existing complete trusted Mission model routes")
    configuration = ModelConfiguration.from_json(Path(args[args.index("--model-config") + 1]).read_bytes())
    # Enforce existing role/family checks before a paid request.
    from forgepilot.agents.registry import AgentRegistry
    AgentRegistry(router=configuration.router())
    return path, configuration


def gateway(configuration, key, ledger, label, *, check=False):
    if check:
        configuration = configuration.model_copy(update={"profiles": tuple(
            profile.model_copy(update={"max_output_tokens": min(1024, profile.max_output_tokens)})
            for profile in configuration.profiles)})
    def transport(profile):
        host = urlsplit(profile.base_url).hostname or ""
        if host == "ark.cn-beijing.volces.com" and profile.model.startswith("doubao-seed-2-1-"):
            parameter = "max_completion_tokens"
        elif host == "ark.cn-beijing.volces.com" and profile.model.startswith("deepseek-v4-1-"):
            parameter = "max_tokens"
        else:
            raise ForgePilotError("llm_budget", "Acceptance has no verified total-output bound for this provider/model")
        return BudgetTransport(ledger, lambda: label, total_output_parameter=parameter)
    return ModelGateway(configuration, secret_resolver=SecretResolver(environment={**os.environ, "FORGEPILOT_API_KEY": key}),
                        transport_factory=transport,
                        accounting=Accounting(max_records=4096))


async def preflight(config_path, configuration, key, ledger, output):
    result = {"connections": [], "chat": "not_run", "summary": "not_run", "embedding": "not_run"}
    async with gateway(configuration, key, ledger, "preflight", check=True) as models:
        for profile in configuration.profiles:
            # Reuse the selected provider through a complete gateway configuration.
            from forgepilot.models.profiles import ModelRoute
            check_config = configuration.model_copy(update={"routes": (
                ModelRoute(route_id="connection-check", primary=profile.profile_id),),
                "enforce_review_family_separation": False})
            try:
                async with gateway(check_config, key, ledger, "connection:" + profile.profile_id, check=True) as check_models:
                    reply = await check_models.for_route("connection-check").chat([
                        {"role": "user", "content": "Reply with only OK."}])
                    result["connections"].append({"profile": profile.profile_id, "ok": bool(reply.message.content)})
            except ForgePilotError as exc:
                result["connections"].append({"profile": profile.profile_id, "ok": False, "code": exc.code})
        if all(row["ok"] for row in result["connections"]):
            for route, prompt in (("chat", "In one sentence, what is a C++ unit test?"),
                                  ("summary", "Summarize this dialogue in one short sentence: user asks to fix a C++ boundary error; assistant will inspect code and run tests.")):
                try:
                    reply = await models.for_route(route).chat([{"role": "user", "content": prompt}])
                    result[route] = "passed" if reply.message.content else "failed"
                except ForgePilotError as exc:
                    result[route] = exc.code
        result["accounting"] = [asdict(record) for record in models.accounting.snapshot()]
    state = await load_configuration(config_path)
    fields = state["fields"]
    if fields["embedding_enabled"]:
        embedding_key = load_embedding_key(config_path) or os.environ.get("FORGEPILOT_EMBEDDING_API_KEY", "")
        if not embedding_key and urlsplit(fields["embedding_base_url"]) == urlsplit(fields["base_url"]):
            embedding_key = key  # Same configured provider only; never writes a credential/config.
            result["embedding_credential"] = "existing_same_provider_key_for_this_probe_only"
        if embedding_key:
            try:
                async with OpenAICompatibleEmbeddingProvider(fields["embedding_base_url"], embedding_key,
                        fields["embedding_model"], transport=BudgetTransport(ledger, lambda: "embedding-check")) as embeddings:
                    vector = await embeddings.embed_query("C++ boundary check")
                    result["embedding"] = "passed"
                    result["embedding_dimensions"] = len(vector)
            except ForgePilotError as exc:
                result["embedding"] = exc.code
        else:
            result["embedding"] = "credential_missing"
    save(output / "preflight.json", result)
    return all(row["ok"] for row in result["connections"]) and result["chat"] == result["summary"] == "passed"


def fixture(task):
    root = Path(tempfile.mkdtemp(prefix="fp6-", dir=ROOT.anchor))
    source, oracle = root / "s", root / "o"
    (source / "src").mkdir(parents=True); (source / "tests").mkdir(); oracle.mkdir()
    (source / "src/value.cpp").write_text(task["source"], encoding="utf-8")
    cmake = ('cmake_minimum_required(VERSION 3.21)\nproject(Acceptance LANGUAGES CXX)\n'
             'enable_testing()\nadd_library(value src/value.cpp)\nadd_executable(public_test tests/public.cpp)\n'
             'target_link_libraries(public_test PRIVATE value)\nadd_test(NAME public_behavior COMMAND public_test)\n')
    (source / "CMakeLists.txt").write_text(cmake, encoding="utf-8")
    (source / "tests/public.cpp").write_text(task["declaration"] + '\nint main(){ return (' + task["public"] + ') ? 0 : 1; }\n', encoding="utf-8")
    (oracle / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.21)\nproject(Oracle LANGUAGES CXX)\nenable_testing()\n'
        'add_executable(check_behavior check.cpp "${FORGEPILOT_WORKTREE}/src/value.cpp")\nadd_test(NAME protected_behavior COMMAND check_behavior)\n', encoding="utf-8")
    (oracle / "check.cpp").write_text(task["declaration"] + '\nint main(){ return (' + task["oracle"] + ') ? 0 : 1; }\n', encoding="utf-8")
    def git(*args):
        return subprocess.check_output([executable(None, "git"), "-C", str(source), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q", "-b", "main"); git("config", "user.name", "Acceptance Fixture")
    git("config", "user.email", "acceptance@example.invalid"); git("config", "core.autocrlf", "false")
    git("add", "."); git("commit", "-qm", "Fixed acceptance baseline")
    return root, source, oracle, git("rev-parse", "HEAD"), git


async def run_sample(task, repeat, configuration, key, ledger, output):
    label = task["id"] + "-r" + str(repeat)
    evidence = output / label; evidence.mkdir()
    root, source, oracle, head, git = fixture(task)
    before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob("*") if p.is_file() and ".git" not in p.parts}
    models = gateway(configuration, key, ledger, label)
    store = SqliteEventStore(root / "mission.sqlite3", artifact_root=root / "a", redactor=models.redactor)
    workflows = []
    build = BuildConfig(cmake=executable(None, "cmake"), ctest=executable(None, "ctest"),
                        generator="Visual Studio 17 2022", architecture="x64", configuration="Release")
    def no_legacy(trace):
        raise AssertionError("Acceptance must use production Mission")
    def factory(events, *, mcp_snapshots=()):
        dependencies = MissionDependencies(gateway=models, events=events, rg_executable=executable(None, "rg"),
            git_executable=executable(None, "git"), build_config=build, redactor=models.redactor,
            mcp_connection_factory=app.state.task_host.mcp_service.connection_factory)
        workflow = build_mission_workflow(dependencies, oracle_source=oracle, cloud_model_allowed=True,
                                          mcp_snapshots=mcp_snapshots)
        workflows.append(workflow)
        return workflow
    app = create_app(no_legacy, store=store, mission_factory=factory, model_client_closer=models.aclose)
    result = {"run": label, "task": task["id"], "repeat": repeat, "status": "failed", "fixture_root": str(root)}
    started = monotonic(); initial_charge = ledger.snapshot()["charged_or_reserved"]
    task_id = None
    try:
        async with app.router.lifespan_context(app):
            service = app.state.task_host.mcp_service
            candidate = {"name": "Local acceptance notes", "transport": "stdio", "modes": ["task"],
                "selected_tools": ["project_info"], "effect": "read", "enabled": True,
                "connection": {"command": sys.executable, "args": ["-I", "-u", str(ROOT / "agent/examples/mcp_project_notes.py")], "cwd": str(root)}}
            probe = await service.probe(candidate)
            saved = await service.save(str(uuid4()), candidate, base_revision=0, catalog_hash=probe["catalog_hash"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=30) as client:
                async def call(method, path, body=None):
                    response = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
                    if response.is_error:
                        raise ForgePilotError(response.json().get("error", {}).get("code", "acceptance_http"), "Acceptance HTTP failed")
                    return response.json()
                request = (task["objective"] + " Only modify src/value.cpp; preserve tests/public.cpp and CMakeLists.txt unchanged. "
                    "Use allowed_scope.path_patterns=[\"src/value.cpp\"], max_changed_files=1. No new files or tests needed. "
                    "Acceptance criteria: AC-1 actual CMake build passes (build); AC-2 existing public CTest passes (ctest); "
                    "AC-3 protected boundary checks pass (protected_test). Retain function signature. "
                    "The selected local MCP project_info tool is read-only reference data. Read the current source before editing.")
                created = await call("POST", "/api/v2/missions", {"repository_path": str(source), "request": request,
                    "task_type": task["kind"], "mcp_server_ids": [saved["server_id"]]})
                task_id = created["task_id"]; path = "/api/v2/missions/" + task_id
                result["task_id"] = task_id
                async def settle(analysis=False):
                    deadline = monotonic() + 900
                    while monotonic() < deadline:
                        state = await call("GET", path)
                        if state["status"] in {"succeeded", "failed", "cancelled", "retryable", "interrupted"}:
                            return state
                        if analysis and state["status"] == "awaiting_human" and state.get("review"):
                            return state
                        if not analysis and state["status"] == "awaiting_human":
                            return state
                        await asyncio.sleep(0.1)
                    raise TimeoutError("Acceptance Mission did not settle")
                state = await settle(analysis=True)
                if state["status"] == "awaiting_human" and state.get("review"):
                    artifacts = (await call("GET", path + "/artifacts"))["items"]
                    spec = next(a["content"] for a in artifacts if a["artifact_type"] == "TaskSpec")
                    plans = [a["content"] for a in artifacts if a["artifact_type"] == "ExecutionPlan"]
                    if (spec["allowed_scope"]["path_patterns"] != ["src/value.cpp"]
                            or spec["allowed_scope"]["max_changed_files"] != 1
                            or any(p != "src/value.cpp" for step in plans[-1]["steps"] for p in step["expected_files"])):
                        raise ForgePilotError("acceptance_scope", "Plan exceeds precommitted sample authorization")
                    graph = await call("GET", path + "/graph")
                    assert all(n["status"] == "pending" for n in graph["nodes"] if n["node_id"] in {"baseline_capture", "implementer"})
                    save(evidence / "approved-review.json", state["review"])
                    review = state["review"]
                    await call("POST", path + "/approvals", {"expected_revision": review["revision"], "decision": "approved",
                        **{field: review[field] for field in ("task_spec_hash", "plan_hash", "scope_hash")}})
                    state = await settle()
                artifacts = (await call("GET", path + "/artifacts"))["items"]
                graph = await call("GET", path + "/graph")
                validation = await call("GET", path + "/validation")
                save(evidence / "state.json", state); save(evidence / "graph.json", graph)
                save(evidence / "artifacts.json", artifacts); save(evidence / "validation.json", validation)
                reports = [a["content"] for a in artifacts if a["artifact_type"] == "ValidationReport"]
                reviews = [a["content"] for a in artifacts if a["artifact_type"] == "ReviewReport"]
                bundles = [a for a in artifacts if a["artifact_type"] == "DeliveryBundle"]
                result.update(graph_status=state["status"], errors=[n["result"]["error_summary"] for n in graph["nodes"]
                    if n.get("result") and n["result"].get("error_summary")], tool_context=state.get("tool_context"))
                if state["status"] == "succeeded" and bundles and reports and reviews:
                    last = reports[-1]
                    assert last["status"] == "passed" and last["failure_classification"] == "none"
                    assert last["hidden_oracle"] == last["test_integrity"] == "passed"
                    assert all(c["exit_code"] == 0 for c in last["commands"])
                    assert {"configure", "build", "test", "protected_test", "integrity"} <= {c["kind"] for c in last["commands"]}
                    assert reviews[-1]["recommend_delivery"] and reviews[-1]["verdict"] == "approve"
                    diff = await call("GET", path + "/diff"); save(evidence / "diff.json", diff)
                    preview = await call("POST", path + "/delivery", {"expected_revision": state["current_revision"], "mode": "preview"})
                    exported = await call("POST", path + "/delivery", {"expected_revision": preview["mission"]["current_revision"],
                        "mode": "patch", "token": preview["delivery"]["token"]})
                    save(evidence / "delivery.json", exported)
                    result["status"] = "passed"
                else:
                    result["reason"] = state["status"]
                after = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in source.rglob("*") if p.is_file() and ".git" not in p.parts}
                assert after == before and git("rev-parse", "HEAD") == head and not git("status", "--porcelain")
                result["source_unchanged"] = True
    except Exception as exc:
        result.update(status="failed", reason=exc.code if isinstance(exc, ForgePilotError) else type(exc).__name__)
    finally:
        result["duration_seconds"] = round(monotonic() - started, 3)
        result["tokens"] = ledger.snapshot()["charged_or_reserved"] - initial_charge
        result["network_requests"] = sum(row["label"] == label for row in ledger.snapshot()["requests"])
        if not result["network_requests"] and (result.get("reason") == "llm_budget" or "llm_budget" in result.get("errors", [])):
            result["status"] = "not_run_budget"
        result["accounting"] = [asdict(record) for record in models.accounting.snapshot()]
        save(evidence / "result.json", result)
        store.close()
    return result


async def execute(output):
    config_path, configuration = saved_configuration()
    key = load_saved_key(config_path)
    if not key:
        raise ValueError("Existing model credential unavailable")
    identity = {"model_configuration_sha256": hashlib.sha256(configuration.model_dump_json().encode()).hexdigest(),
                "launcher_revision": configuration_revision(config_path)}
    previous = json.loads((output / "summary.json").read_text(encoding="utf-8")) if (output / "summary.json").exists() else None
    if previous and any(previous.get(name) != value for name, value in identity.items()):
        raise ValueError("Acceptance model configuration or credential identity changed; cannot resume")
    ledger = RequestBudget(output / "budget.sqlite3")
    runs = previous.get("runs", []) if previous else []
    summary = {"planned_runs": 12, "protocol_sha256": hashlib.sha256(json.dumps(plan(), sort_keys=True).encode()).hexdigest(),
               "models": [{"profile": p.profile_id, "model": p.model, "family": p.family} for p in configuration.profiles],
               **identity, "runs": runs}
    try:
        save(output / "summary.json", {**summary, "budget": ledger.snapshot()})
        if ledger.snapshot()["held"] or previous and previous.get("stop_reason") == "shared_budget_exhausted":
            summary["stop_reason"] = "shared_budget_held"
            return
        prior = json.loads((output / "preflight.json").read_text(encoding="utf-8")) if (output / "preflight.json").exists() else None
        ready = prior and all(row["ok"] for row in prior["connections"]) and prior["chat"] == prior["summary"] == "passed"
        if ready or await preflight(config_path, configuration, key, ledger, output):
            for repeat in range(1, 4):
                for task in TASKS:
                    label = task["id"] + "-r" + str(repeat)
                    if any(run["run"] == label for run in runs):
                        continue
                    if (output / label).exists():
                        raise ValueError("An incomplete run directory requires inspection; never overwrite paid evidence")
                    print("RUN", task["id"], repeat, flush=True)
                    result = await run_sample(task, repeat, configuration, key, ledger, output)
                    runs.append(result)
                    save(output / "summary.json", {**summary, "budget": ledger.snapshot()})
                    print("RESULT", result["run"], result["status"], result["tokens"], flush=True)
                    if result.get("reason") == "llm_budget" or "llm_budget" in result.get("errors", []):
                        summary["stop_reason"] = "shared_budget_exhausted"
                        return
        else:
            summary["stop_reason"] = "preflight_failed"
    finally:
        attempted = sum(run.get("network_requests", 0) > 0 for run in runs)
        summary.update(attempted_runs=attempted, passed_runs=sum(r["status"] == "passed" for r in runs),
                       unattempted_runs=12-attempted, budget=ledger.snapshot())
        save(output / "summary.json", summary)
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="send budgeted real requests using existing configuration")
    parser.add_argument("--resume", action="store_true", help="continue remaining planned runs using the same ledger and evidence")
    args = parser.parse_args()
    if args.resume:
        if not args.execute or not (args.output / "budget.sqlite3").is_file():
            parser.error("resume requires execute and the existing paid ledger")
        previous = json.loads((args.output / "plan.json").read_text(encoding="utf-8"))
        if json.dumps(previous, sort_keys=True) != json.dumps(plan(), sort_keys=True):
            parser.error("fixed acceptance plan changed; cannot resume this campaign")
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / "plan.json", plan())
    if args.execute:
        asyncio.run(execute(args.output))
    else:
        print("Prepared fixed protocol only; no real request sent.")


if __name__ == "__main__":
    main()
