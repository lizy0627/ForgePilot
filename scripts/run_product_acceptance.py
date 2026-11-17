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
PAID_CAMPAIGN = ROOT / "build-test" / "product-readiness-stage6" / "real-acceptance"
sys.path.insert(0, str(ROOT / "agent"))

import httpx

from acceptance_budget import BudgetTransport, RequestBudget
from acceptance_reconciliation import runtime_identity
from acceptance_recovery import capture_stop, materialize_recovery, open_recovery, sample_stopped_on_budget
from acceptance_revalidation import verify_revalidation
from forgepilot.api import create_app
from forgepilot.desktop_configuration import (
    ConfigurationLock, configuration_revision, load_configuration, load_embedding_key, load_saved_key,
)
from forgepilot.errors import ForgePilotError
from forgepilot.mission_factory import MissionDependencies, build_mission_workflow
from forgepilot.models.accounting import Accounting, ModelBudget
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration, ModelRoute, SecretResolver
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
    output = tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                         delete=False, mode="w", encoding="utf-8")
    temporary = Path(output.name)
    try:
        with output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def gateway(configuration, key, ledger, label, *, check=False, verify_usage=False):
    if check:
        configuration = configuration.model_copy(update={"profiles": tuple(
            profile.model_copy(update={"max_output_tokens": min(1024, profile.max_output_tokens)})
            for profile in configuration.profiles)})
    def transport(profile):
        host = urlsplit(profile.base_url).hostname or ""
        from acceptance_revalidation import total_output_parameter

        try:
            parameter = total_output_parameter(profile.model)
        except ValueError:
            raise ForgePilotError("llm_budget", "Acceptance has no verified total-output bound for this model") from None
        if host != "ark.cn-beijing.volces.com":
            raise ForgePilotError("llm_budget", "Acceptance has no verified total-output bound for this provider/model")
        return BudgetTransport(ledger, lambda: label, total_output_parameter=parameter, verify_usage=verify_usage,
                               expected_model=profile.model, api_root=profile.base_url, request_kind="chat",
                               output_ceiling=profile.max_output_tokens)
    return ModelGateway(configuration, secret_resolver=SecretResolver(environment={**os.environ, "FORGEPILOT_API_KEY": key}),
                        transport_factory=transport,
                        accounting=Accounting(max_records=4096))


async def run_revalidation(config_path, configuration, key, ledger, *, label, identity):
    from acceptance_revalidation import verify_probe

    if label != "revalidation" or any(r["label"] == label for r in ledger.snapshot()["requests"]):
        raise ValueError("Revalidation requires its unused campaign label")
    started = monotonic()
    checks = []
    result = {"run": label, "status": "failed", "identity": identity, "checks": checks,
              "embedding_enabled": None, "first_failure_reason": None, "final_stop_reason": None}
    try:
        state = await load_configuration(config_path)
        if state["config_revision"] != identity["launcher_revision"]:
            raise ForgePilotError("acceptance_config_drift", "Launcher changed before revalidation")
        fields = state["fields"]
        result["embedding_enabled"] = fields["embedding_enabled"]
        planned = [("connection:" + p.profile_id, p, "Reply with only OK.") for p in configuration.profiles]
        router = configuration.router()
        planned.extend((name, router.resolve(name).primary, prompt) for name, prompt in (
            ("chat", "In one sentence, what is a C++ unit test?"),
            ("summary", "Summarize this dialogue in one short sentence: user asks to fix a C++ boundary error; "
                        "assistant will inspect code and run tests.")))
        if fields["embedding_enabled"]:
            embedding_key = load_embedding_key(config_path) or os.environ.get("FORGEPILOT_EMBEDDING_API_KEY")
            if not embedding_key and urlsplit(fields["embedding_base_url"]) == urlsplit(fields["base_url"]):
                embedding_key = key
            if not embedding_key:
                raise ForgePilotError("credential_missing", "Configured embedding credential unavailable")
            if configuration_revision(config_path) != identity["launcher_revision"]:
                raise ForgePilotError("acceptance_config_drift", "Launcher changed during credential capture")
            planned.append(("embedding", None, "C++ boundary check"))
        for name, profile, prompt in planned:
            check = {"name": name, "kind": "chat" if profile else "embedding", "status": "failed",
                     "model": profile.model if profile else fields["embedding_model"],
                     "profile": profile.profile_id if profile else None,
                     "output_limit": min(1024, profile.max_output_tokens) if profile else None}
            checks.append(check)
            if profile:
                route = ModelRoute(route_id="connection-check", primary=profile.profile_id)
                config = configuration.model_copy(update={"routes": (route,), "enforce_review_family_separation": False})
                async with gateway(config, key, ledger, label, check=True, verify_usage=True) as models:
                    client = models.for_route(route.route_id, budget=ModelBudget(max_model_calls=1))
                    reply = await client.chat([{"role": "user", "content": prompt}])
                    content = (reply.message.content or "").strip()
                    if not content or name.startswith("connection:") and content.casefold() != "ok":
                        raise ForgePilotError("llm_response", "Unexpected acceptance probe reply")
            else:
                transport = BudgetTransport(ledger, lambda: label, verify_usage=True,
                                            expected_model=fields["embedding_model"], api_root=fields["embedding_base_url"],
                                            request_kind="embedding")
                async with OpenAICompatibleEmbeddingProvider(fields["embedding_base_url"], embedding_key,
                        fields["embedding_model"], transport=transport) as embeddings:
                    check["dimensions"] = len(await embeddings.embed_query(prompt))
            attempts = ledger.wire_attempts(label)
            if len(attempts) != len(checks):
                raise ForgePilotError("revalidation_wire", "Probe needs exactly one wire attempt")
            check["request_id"] = attempts[-1]["request_id"]
            try:
                verify_probe(check, attempts[-1])
            except ValueError:
                raise ForgePilotError("probe_usage_invalid", "Probe did not prove the budget contract") from None
            check["status"] = "passed"
        result["status"] = "passed"
    except Exception as exc:
        code = exc.code if isinstance(exc, ForgePilotError) else type(exc).__name__
        result["first_failure_reason"] = result["final_stop_reason"] = code
    attempts = ledger.wire_attempts(label)
    if attempts and result["status"] == "failed" and checks:
        first = next((a for a in attempts if a["failure_reason"]), None)
        if first:
            result["first_failure_reason"] = first["failure_reason"]
        if len(checks) == len(attempts):
            checks[-1]["request_id"] = attempts[-1]["request_id"]
    result.update(duration_seconds=round(monotonic() - started, 3), network_requests=len(attempts),
                  wire_attempts=attempts,
                  reported_tokens=sum(a["reported_tokens"] for a in attempts),
                  estimated_tokens=sum(a["estimated_tokens"] for a in attempts))
    if result["status"] == "passed":
        try:
            verify_revalidation(result, attempts, [{"profile": p.profile_id, "model": p.model}
                                                    for p in configuration.profiles])
        except ValueError:
            result.update(status="failed", first_failure_reason="revalidation_evidence",
                          final_stop_reason="revalidation_evidence")
    return result


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


def sample_terminal_failure(missions, task_id):
    """Read the persisted task failure even when an interrupted node has no NodeResult."""
    after = 0
    terminal = None
    while True:
        _, rows, high_water = missions.event_page(task_id, after, 128)
        for row in rows:
            if row["event_type"] == "task.failed":
                code = json.loads(row["payload_json"]).get("error_code")
                if code:
                    terminal = {"event_type": row["event_type"], "sequence": row["sequence"], "error_code": code}
        if not rows or rows[-1]["sequence"] >= high_water:
            return terminal
        after = rows[-1]["sequence"]


def sample_failure_reasons(result, wire_attempts=()):
    """Keep the earliest transport/accounting cause even if budget stops a later attempt."""
    first = next((r["failure_reason"] for r in wire_attempts if r.get("failure_reason")), None)
    if first is None:
        first = next((r["status"] for r in result.get("accounting", [])
                      if r.get("status") not in (None, "success")), None)
    if result["status"] == "passed":
        return first, None
    errors = result.get("errors") or []
    reason = result.get("reason")
    if reason and reason not in {"failed", "cancelled", "retryable", "interrupted", "awaiting_human"}:
        stop = reason
    elif "llm_budget" in errors:
        stop = "llm_budget"
    elif result.get("terminal_failure_event"):
        stop = result["terminal_failure_event"]["error_code"]
    else:
        stop = errors[-1] if errors else reason or result["status"]
    return first or (errors[0] if errors else stop), stop


async def run_sample(task, repeat, configuration, key, ledger, output, *, identity, retest_of=None):
    if task not in TASKS or type(repeat) is not int or not 1 <= repeat <= 3:
        raise ValueError("Sample must match a fixed task and round")
    slot = f"{task['id']}-r{repeat}"
    label = slot
    result = {"task": task["id"], "repeat": repeat, "status": "failed", "identity": identity}
    if retest_of is not None:
        if retest_of != slot:
            raise ValueError("Retest must identify its original fixed slot")
        path = output / slot / "result.json"
        if path.resolve(strict=True) != path.absolute() or path.stat().st_nlink != 1:
            raise ValueError("Unsafe original result")
        raw = path.read_bytes()
        original = json.loads(raw)
        if (original.get("run") != slot or original.get("status") != "failed"
                or identity.get("original_result_sha256") != hashlib.sha256(raw).hexdigest()):
            raise ValueError("Retest requires the frozen failed result")
        first, stop = sample_failure_reasons(original)
        failure = {"first_failure_reason": first, "final_stop_reason": stop}
        hold_path = output / "budget-hold-audit.json"
        if hold_path.exists():
            if hold_path.resolve(strict=True) != hold_path.absolute() or hold_path.stat().st_nlink != 1:
                raise ValueError("Unsafe original hold evidence")
            raw_hold = hold_path.read_bytes()
            hold = json.loads(raw_hold)
            if hold["original_request"]["label"] == slot:
                failure.update(hold_reason=hold["reason"], hold_audit_sha256=hashlib.sha256(raw_hold).hexdigest())
        result.update(retest_of=slot, original_failure=failure)
        label = "retest-" + slot
    evidence = output / label
    evidence.mkdir(exist_ok=False)
    result["run"] = label
    started = monotonic()
    try:
        await _run_sample_workflow(task, configuration, key, ledger, evidence, result)
    except asyncio.CancelledError:
        result.update(status="cancelled", reason="cancelled")
        raise
    except Exception as exc:
        result.update(status="failed", reason=exc.code if isinstance(exc, ForgePilotError) else type(exc).__name__)
    finally:
        rows = [row for row in ledger.snapshot()["requests"] if row["label"] == label]
        attempts = ledger.wire_attempts(label)
        result.update(duration_seconds=round(monotonic() - started, 3), network_requests=len(rows),
                      reported_tokens=sum(r["charged"] for r in rows if r["estimated"] == 0),
                      estimated_tokens=sum(r["reserved"] if r["charged"] is None else r["charged"]
                                           for r in rows if r["estimated"] != 0), wire_attempts=attempts)
        result["tokens"] = result["reported_tokens"] + result["estimated_tokens"]
        result["first_failure_reason"], result["final_stop_reason"] = sample_failure_reasons(result, attempts)
        if not rows and result["final_stop_reason"] == "llm_budget":
            result["status"] = "not_run_budget"
        save(evidence / "result.json", result)
    return result


async def _run_sample_workflow(task, configuration, key, ledger, evidence, result):
    root, source, oracle, head, git = fixture(task)
    result.update(fixture_root=str(root), source_head=head)
    before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob("*") if p.is_file() and ".git" not in p.parts}
    result["source_files_sha256"] = before
    result["source_status_before"] = git("status", "--porcelain")
    models = gateway(configuration, key, ledger, result["run"], verify_usage=True)
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
                    pending = {n["node_id"]: n["status"] for n in graph["nodes"]
                               if n["node_id"] in {"baseline_capture", "implementer"}}
                    if pending != {"baseline_capture": "pending", "implementer": "pending"}:
                        raise ForgePilotError("acceptance_approval", "Execution nodes must be pending before approval")
                    save(evidence / "approved-review.json", state["review"])
                    review = state["review"]
                    await call("POST", path + "/approvals", {"expected_revision": review["revision"], "decision": "approved",
                        **{field: review[field] for field in ("task_spec_hash", "plan_hash", "scope_hash")}})
                    state = await settle()
                if state["status"] == "failed":
                    result["terminal_failure_event"] = sample_terminal_failure(store.missions, task_id)
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
                    if (last["status"] != "passed" or last["failure_classification"] != "none"
                            or any(last[k] != "passed" for k in ("hidden_oracle", "test_integrity", "workspace_integrity", "diff_scope"))
                            or last["tests_passed"] < 1 or last["tests_failed"]
                            or any(c["exit_code"] != 0 for c in last["commands"])
                            or not {"configure", "build", "test", "protected_test", "integrity"} <= {c["kind"] for c in last["commands"]}):
                        raise ForgePilotError("acceptance_validation", "Missing successful build, tests, integrity or scope checks")
                    calls = models.accounting.snapshot()
                    writers = {configuration.profile(c.profile_id).family for c in calls
                               if c.role in {"implementer", "repair"} and c.status == "success"}
                    reviewers = {configuration.profile(c.profile_id).family for c in calls
                                 if c.role == "reviewer" and c.status == "success"}
                    if (not reviews[-1]["recommend_delivery"] or reviews[-1]["verdict"] != "approve"
                            or not writers or not reviewers or writers & reviewers):
                        raise ForgePilotError("acceptance_review", "Independent review approval is required")
                    if bundles[-1]["content"]["changed_files"] != ["src/value.cpp"]:
                        raise ForgePilotError("acceptance_scope", "Delivery exceeded fixed sample scope")
                    result.update(writer_families=sorted(writers), reviewer_families=sorted(reviewers))
                    diff = await call("GET", path + "/diff"); save(evidence / "diff.json", diff)
                    preview = await call("POST", path + "/delivery", {"expected_revision": state["current_revision"], "mode": "preview"})
                    exported = await call("POST", path + "/delivery", {"expected_revision": preview["mission"]["current_revision"],
                        "mode": "patch", "token": preview["delivery"]["token"]})
                    delivery = exported["delivery"]
                    patch = delivery["patch"]
                    patch_file = Path(patch["path"])
                    if (delivery["mode"] != "patch" or patch_file.resolve(strict=True) != patch_file
                            or not patch_file.is_relative_to(root / "a")):
                        raise ForgePilotError("acceptance_delivery", "Unexpected exported patch path")
                    patch_bytes = patch_file.read_bytes()
                    if len(patch_bytes) != patch["size"] or not patch_bytes or hashlib.sha256(patch_bytes).hexdigest() != patch["sha256"]:
                        raise ForgePilotError("acceptance_delivery", "Patch export identity mismatch")
                    with (evidence / "delivery.patch").open("xb") as file:
                        file.write(patch_bytes)
                    save(evidence / "delivery.json", exported)
                    result["status"] = "passed"
                else:
                    result["reason"] = state["status"]
    finally:
        result["accounting"] = [asdict(record) for record in models.accounting.snapshot()]
        store.close()
        after = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in source.rglob("*") if p.is_file() and ".git" not in p.parts}
        result["source_status_after"] = git("status", "--porcelain")
        result["source_unchanged"] = (after == before and git("rev-parse", "HEAD") == head
                                      and result["source_status_before"] == result["source_status_after"] == "")
        if not result["source_unchanged"]:
            raise ForgePilotError("acceptance_source", "Source repository changed")


def campaign_ledger(output):
    if output.absolute() != PAID_CAMPAIGN.absolute() or output.resolve() != PAID_CAMPAIGN.absolute():
        raise ValueError("Paid acceptance must use the existing stage-six campaign directory")
    path = PAID_CAMPAIGN / "budget.sqlite3"
    if not path.is_file():
        raise ValueError("Missing original acceptance ledger")
    for file in (path, path.with_suffix(".sqlite3-wal"), path.with_suffix(".sqlite3-shm"),
                 path.with_suffix(".sqlite3-journal")):
        if file.is_symlink() or file.exists() and (file.resolve() != file or file.stat().st_nlink != 1):
            raise ValueError("Unexpected linked acceptance ledger")
    return RequestBudget(path, existing_only=True)


def verify_plan(output):
    persisted = json.loads((output / "plan.json").read_bytes())
    if json.dumps(persisted, sort_keys=True) != json.dumps(plan(), sort_keys=True):
        raise ValueError("Fixed acceptance plan changed; cannot resume this campaign")


def reconcile_offline(output, inputs):
    ledger = campaign_ledger(output)
    try:
        verify_plan(output)
        if inputs:
            ledger.reconcile(*inputs, anchors=capture_stop(output))
        derived = materialize_recovery(output, ledger, runtime_identity())
        print(f"Offline recovery record: {derived}", flush=True)
    finally:
        ledger.close()


async def execute(output, *, revalidate_only=False, retest=None):
    output = output.absolute()
    ledger = campaign_ledger(output)
    try:
        original = json.loads((output / "summary.json").read_bytes())
        audits = ledger.reconciliation_audits()
        if ledger.snapshot()["held"] or (original.get("stop_reason") == "shared_budget_exhausted" and not audits):
            print("Shared acceptance budget is held; existing evidence is unchanged.", flush=True)
            return
        if not audits or original.get("stop_reason") != "shared_budget_exhausted":
            raise ValueError("Missing the original stop or its request-level reconciliation")
        failed_slot = audits[0]["before"]["label"]
        if retest is not None and retest != failed_slot:
            raise ValueError("Retest must identify the original reconciled failure")
        verify_plan(output)
        identity = runtime_identity()
        # Serialize progress checkpoints across harness processes. This uses a
        # campaign-local OS lock; SQLite still arbitrates each transport reservation.
        writer = ConfigurationLock(output / "summary.json")
        if writer.path.is_symlink() or writer.path.exists() and (
                writer.path.resolve() != writer.path or writer.path.stat().st_nlink != 1):
            raise ValueError("Unexpected linked campaign writer lock")
        with writer:
            previous, progress = open_recovery(output, ledger, identity)
            await _execute_available(output, ledger, previous, progress, identity,
                                     failed_slot=failed_slot, revalidate_only=revalidate_only, retest=retest)
    finally:
        ledger.close()


async def _execute_available(output, ledger, previous, progress, runtime, *, failed_slot, revalidate_only, retest):
    config_path, configuration = saved_configuration()
    identity = {"model_configuration_sha256": hashlib.sha256(configuration.model_dump_json().encode()).hexdigest(),
                "launcher_revision": configuration_revision(config_path)}
    models = [{"profile": p.profile_id, "model": p.model, "family": p.family} for p in configuration.profiles]
    if any(previous.get(name) != value for name, value in identity.items()) or previous["models"] != models:
        raise ValueError("Acceptance model configuration or credential identity changed; cannot resume")
    runs = list(previous["runs"])
    identity.update(protocol_sha256=previous["protocol_sha256"], runtime_identity=runtime,
                    recovery_audit_sha256=previous["recovery_audit_sha256"])
    summary = {**previous, **identity, "runs": runs}
    # open_recovery checked these records against the original ledger and the
    # per-slot evidence before permitting this connection to reserve tokens.
    if previous.get("revalidation") and (revalidate_only or (retest and previous.get("retest"))
                                         or not retest and len(runs) == previous["planned_runs"]):
        return

    def checkpoint():
        attempted = sum(run.get("network_requests", 0) > 0 for run in runs)
        summary.update(attempted_runs=attempted, passed_runs=sum(r["status"] == "passed" for r in runs),
                       unattempted_runs=12-attempted, budget=ledger.snapshot())
        save(progress, summary)

    # Verify the first writable continuation checkpoint before reading a key.
    checkpoint()
    key = load_saved_key(config_path)
    if not key:
        raise ValueError("Existing model credential unavailable")
    if configuration_revision(config_path) != identity["launcher_revision"]:
        raise ValueError("Acceptance credential identity changed during key load")
    try:
        if not summary.get("revalidation"):
            label = "revalidation"
            directory = output / label
            directory.mkdir()
            result = await run_revalidation(config_path, configuration, key, ledger, label=label, identity=identity)
            if result["status"] == "passed":
                old_preflight = json.loads((output / "preflight.json").read_bytes())
                try:
                    verify_revalidation(result, ledger.wire_attempts(label), previous["models"],
                                        require_embedding=old_preflight.get("embedding") == "passed")
                except ValueError:
                    result.update(status="failed", first_failure_reason="invalid_revalidation_evidence",
                                  final_stop_reason="invalid_revalidation_evidence")
            save(directory / "result.json", result)
            summary[label] = result
            if result["status"] != "passed":
                summary["stop_reason"] = result["final_stop_reason"] or "revalidation_failed"
                if not ledger.snapshot()["held"]:
                    ledger.stop_recovery()
            checkpoint()
            if summary.get("stop_reason"):
                return
        if revalidate_only:
            return
        if retest:
            original = next(r for r in runs if r["run"] == failed_slot)
            task = next(t for t in TASKS if failed_slot.rsplit("-r", 1)[0] == t["id"])
            repeat = int(failed_slot.rsplit("-r", 1)[1])
            audit = ledger.reconciliation_audits()[0]
            retest_identity = {**identity, "original_result_sha256": audit["result_sha256"]}
            result = await run_sample(task, repeat, configuration, key, ledger, output,
                                      identity=retest_identity, retest_of=original["run"])
            summary["retest"] = result
            if ledger.snapshot()["held"] or sample_stopped_on_budget(result):
                if not ledger.snapshot()["held"]:
                    ledger.stop_recovery()
                summary["stop_reason"] = result.get("final_stop_reason") or "shared_budget_exhausted"
            checkpoint()
            return
        for repeat in range(1, 4):
            for task in TASKS:
                label = task["id"] + "-r" + str(repeat)
                if any(run["run"] == label for run in runs):
                    continue
                if (output / label).exists():
                    raise ValueError("An incomplete run directory requires inspection; never overwrite paid evidence")
                print("RUN", task["id"], repeat, flush=True)
                result = await run_sample(task, repeat, configuration, key, ledger, output, identity=identity)
                runs.append(result)
                if ledger.snapshot()["held"] or sample_stopped_on_budget(result):
                    if not ledger.snapshot()["held"]:
                        ledger.stop_recovery()
                    summary["stop_reason"] = "shared_budget_exhausted"
                checkpoint()
                print("RESULT", result["run"], result["status"], result["tokens"], flush=True)
                if summary.get("stop_reason"):
                    return
    except BaseException:
        # A partial run or a failed progress write needs inspection before any
        # further paid work, including from another process.
        if not ledger.snapshot()["held"]:
            ledger.stop_recovery()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--runtime-identity", action="store_true",
                        help="print the current source fingerprints for an offline reconciliation manifest")
    action.add_argument("--execute", action="store_true", help="send budgeted real requests using existing configuration")
    action.add_argument("--reconcile", type=Path, nargs=2, metavar=("MANIFEST", "PROVIDER_RECORD"),
                        help="reconcile one held request offline and publish its recovery record")
    action.add_argument("--rebuild-recovery", action="store_true",
                        help="publish or verify the derived record for an already committed audit offline")
    parser.add_argument("--resume", action="store_true", help="continue remaining planned runs using the same ledger and evidence")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--revalidate-only", action="store_true", help="only check the post-repair provider contracts")
    mode.add_argument("--retest", metavar="ORIGINAL_RUN", help="one separate post-repair run of the reconciled failure")
    args = parser.parse_args()
    if args.execute != args.resume:
        parser.error("paid acceptance requires --execute --resume and the existing stage-six campaign")
    if (args.revalidate_only or args.retest is not None) and not args.execute:
        parser.error("revalidation and retest require explicit --execute --resume")
    if args.runtime_identity:
        print(json.dumps(runtime_identity(), indent=2))
        return
    if args.output is None:
        parser.error("--output is required for a campaign operation")
    if args.execute or args.reconcile or args.rebuild_recovery:
        if args.output.absolute() != PAID_CAMPAIGN.absolute():
            parser.error("paid acceptance must use the existing stage-six campaign directory")
        if args.execute:
            asyncio.run(execute(args.output, revalidate_only=args.revalidate_only, retest=args.retest))
        else:
            reconcile_offline(args.output, args.reconcile)
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / "plan.json", plan())
        print("Prepared fixed protocol only; no real request sent.")


if __name__ == "__main__":
    main()
