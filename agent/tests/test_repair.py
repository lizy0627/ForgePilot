import asyncio
from dataclasses import asdict
import json
import sys

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.build import BUILD_TOOLS, BuildPipeline
from forgepilot.build_process import CommandResult
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.presets import BuildConfig
from forgepilot.process import executable
from forgepilot.repair import ensure_validation_integrity
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.trace import MemoryTrace
from forgepilot.tools.change_tools import WRITE_TOOLS
from test_build import build_config, build_repo, finish
from test_changes import write
from test_planning import proposal


def reflection(*paths, category="code"):
    plan = proposal(*(paths or ("main.cpp",)))
    plan["root_cause"] = "Current compiler/test diagnostic identifies an incorrect return expression in main.cpp"
    plan["implementation_steps"] = ["Correct the production expression; preserve all tests and assertions"]
    return reply(json.dumps({"category": category, "plan": plan}))


def script(rounds, *, initial=None, path="main.cpp"):
    messages = [reply("inspect production source"), reply(json.dumps(proposal(path))),
                reply(calls=[call("read_file", json.dumps({"path": path}), "read-source"),
                             write(path, initial or "int main(){return broken;}\n")]), reply("initial change")]
    for n in range(rounds):
        messages += [reflection(path), reply(calls=[write(path, f"int main(){{return {n};}}\n")]), reply(f"repair {n + 1}")]
    return messages


def service(root, rg, git, messages, config=None, trace=None, redactor=None):
    trace = trace or MemoryTrace(redactor)
    llm = ScriptedLLM(messages)
    return TaskService(llm, rg_executable=rg, git_executable=git, trace=trace, redactor=redactor,
                       build_config=config or BuildConfig(executable(None, "cmake"), executable(None, "ctest"))), llm, trace


@pytest.fixture
def fake_validation(monkeypatch):
    """Fake process results, retaining real pipeline order, tools, runtime and write/approval guards."""
    settings = {"failures": ["cmake_build"], "status": "failed", "diagnostic": "main.cpp(1): error C2065: broken identifier", "names": ["counter_test"]}
    async def command(pipeline, phase, argv, timeout):
        n = pipeline._report["repair_round"]
        failure = settings["failures"][n] if n < len(settings["failures"]) else None
        failed = phase == failure
        result = CommandResult(argv, str(pipeline.root), 1 if failed else 0,
            f"round={n}: " + settings["diagnostic"] if failed else f"round={n}: success", "", 1,
            settings["status"] if failed else "succeeded")
        pipeline._save_command(phase, result)
        return result
    def tests(pipeline, path, status):
        pipeline._report["tests"] = {"passed": int(status == "succeeded"), "failed": int(status != "succeeded"),
            "total": 1, "not_run": 0, "status": "passed" if status == "succeeded" else "failed"}
        pipeline._report["test_names"] = settings["names"][:]
    monkeypatch.setattr(BuildPipeline, "_command", command)
    monkeypatch.setattr(BuildPipeline, "_configured", lambda _: None)
    monkeypatch.setattr(BuildPipeline, "_test_results", tests)
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("read_before", [False, True])
async def test_reflection_source_collection_does_not_establish_or_refresh_read_version(cpp_repo, rg_exe, git_exe, fake_validation, read_before):
    path = "CMakeLists.txt"
    target = cpp_repo / path
    external = target.read_bytes() + b"\n# external edit\n"
    messages = script(0)
    messages[1] = reply(json.dumps(proposal("main.cpp", path)))
    if read_before:
        messages[2] = reply(calls=[call("read_file", json.dumps({"path": path}), "read-cmake"),
                                  *(c.model_dump() for c in messages[2].message.tool_calls)])
    messages += [reflection("main.cpp", path),
                 reply(calls=[write(path, "# stale replacement\n")]), reply("done")]
    def on_event(event):
        if event.kind == "repair_started":
            target.write_bytes(external)
    svc, _, _ = service(cpp_repo, rg_exe, git_exe, messages, trace=MemoryTrace(on_event=on_event))
    try:
        result = await finish(svc, cpp_repo)
        assert result.repair_round == 1 and result.repair_records
        evidence = result.repair_records[0]["evidence"]["current_sources"]
        assert "external edit" in next(s["content"] for s in evidence if s["path"] == path)
        expected = "write_conflict" if read_before else "write_requires_read"
        assert any((r["error"] or "").startswith(expected) for r in result.tool_results)
        assert target.read_bytes() == external and path.casefold() not in svc._changes.snapshots
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures,rounds,state", [([], 0, "completed"), (["cmake_build"], 1, "completed"),
    (["run_tests"] * 3, 3, "completed"), (["cmake_build"] * 4, 3, "failed"),
    (["run_tests", "cmake_build"], 2, "completed")])
async def test_rounds_and_current_evidence(cpp_repo, rg_exe, git_exe, fake_validation, failures, rounds, state):
    fake_validation["failures"] = failures
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, script(rounds))
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == state, result.error
        assert result.repair_round == rounds and len(result.repair_records) == rounds
        assert result.steps == 4 + 3 * rounds
        assert [r["repair_round"] for r in result.validation_history] == list(range(rounds + 1))
        assert result.build_result == result.validation_history[-1]
        for n, report in enumerate(result.validation_history):
            phases = [c["phase"] for c in report["commands"]]
            expected = list(BUILD_TOOLS[:2]) if n < len(failures) and failures[n] == "cmake_build" else list(BUILD_TOOLS)
            assert phases == expected
            assert all(c["repair_round"] == n for c in report["commands"])
            if phases[-1] == "cmake_build":
                assert report["tests"]["total"] is None
        for n, record in enumerate(result.repair_records, 1):
            assert record["source_round"] == n - 1
            assert record["evidence"]["commands"][0]["stdout"].startswith(f"round={n - 1}:")
            assert record["evidence"]["current_sources"] and record["evidence"]["task_diff"]["files"]
            assert record["reflection"]["plan"]["root_cause"]
        states = [e.data for e in trace.events if e.kind == "task_state_changed"]
        assert [s["repair_round"] for s in states if s["state"] == "reflecting"] == list(range(1, rounds + 1))
        assert len([e for e in trace.events if e.kind == "task_finished"]) == 1
        assert result.task_diff["files"]
        if state == "failed":
            assert result.outcome == "repair_exhausted" and result.final_result is None
            assert result.last_validation_error["repair_round"] == 3
            assert all(r["status"] == "failed" for r in result.repair_records)
        elif rounds:
            assert result.repair_records[-1]["status"] == "succeeded" and result.error is None
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "cancel", "reject"])
async def test_repair_new_scope_review(cpp_repo, rg_exe, git_exe, fake_validation, decision):
    messages = script(0) + [reflection("main.cpp", "helper.cpp"),
        reply(calls=[write("helper.cpp", "int helper(){return 0;}\n")]), reply("repaired")]
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, messages)
    try:
        pending = await finish(svc, cpp_repo)
        assert pending.state == "waiting_approval" and pending.plan_version == 2
        assert pending.repair_round == 1 and pending.repair_records[-1]["status"] == "waiting_approval"
        assert pending.approved_files == set() and pending.approved_plan_version is None
        assert not (cpp_repo / "helper.cpp").exists() and pending.task_diff["files"]
        assert len(pending.validation_history) == 1 and pending.steps == 5
        with pytest.raises(ForgePilotError, match="stale_plan"):
            svc.approve(pending.task_id, 1)
        if decision == "approve":
            svc.approve(pending.task_id, 2)
            result = await svc.execute(pending.task_id)
            assert result.state == "completed", result.error
            assert result.repair_round == 1 and result.steps == 7 and len(result.validation_history) == 2
            assert (cpp_repo / "helper.cpp").exists()
        else:
            if decision == "cancel":
                assert svc.cancel()
                result = svc._context
            else:
                result = svc.reject(pending.task_id, 2)
            assert result.state == "cancelled" and result.repair_round == 1
            assert result.repair_records[-1]["status"] == "cancelled"
            assert result.steps == 5 and len(result.validation_history) == 1
            assert not (cpp_repo / "helper.cpp").exists() and result.task_diff["files"]
        assert trace.events[-1].kind == "task_finished"
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_scope_discovered_during_repair_write(cpp_repo, rg_exe, git_exe, fake_validation):
    messages = script(0) + [reflection(), reply(calls=[write("helper.cpp", "new"), write("main.cpp", "must not run", "write-main")]),
                           reply(json.dumps(proposal("main.cpp", "helper.cpp"))), reply("finish repair")]
    svc, _, _ = service(cpp_repo, rg_exe, git_exe, messages)
    try:
        pending = await finish(svc, cpp_repo)
        assert pending.state == "waiting_approval" and pending.repair_round == 1
        assert "broken" in (cpp_repo / "main.cpp").read_text() and not (cpp_repo / "helper.cpp").exists()
        svc.approve(pending.task_id, 2)
        result = await svc.execute(pending.task_id)
        assert result.state == "completed" and result.repair_round == 1 and result.steps == 8
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [26, 24])
async def test_global_budget_never_resets(cpp_repo, rg_exe, git_exe, fake_validation, used):
    fake_validation["failures"] = ["cmake_build"] * 4
    # 26 inspection calls + analysis/plan/write/summary consume all 30 before reflection.
    # 24 consume 28, then reflection/write use 29/30; no second validation or request 31.
    messages = [reply(calls=[call()]) for _ in range(used)] + script(3)
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, messages)
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.error.startswith("step_limit:")
        assert result.steps == len(llm.requests) == 30
        assert result.repair_round == (0 if used == 26 else 1)
        assert len(result.validation_history) == 1 and result.task_diff["files"]
        assert trace.events[-1].data["steps"] == 30
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,diagnostic,kind", [("timed_out", "compile taking too long", "timed_out"),
    ("failed", "ninja: error: loading build.ninja", "environment"), ("failed", "HTTP 401 unauthorized", "credentials"),
    ("failed", "fatal error LNK1104: cannot open file library.lib", "environment")])
async def test_non_code_failures_stop(cpp_repo, rg_exe, git_exe, fake_validation, status, diagnostic, kind):
    fake_validation.update(status=status, diagnostic=diagnostic)
    svc, llm, _ = service(cpp_repo, rg_exe, git_exe, script(0))
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.failure_kind == kind
        assert result.repair_round == 0 and result.steps == 4 and result.repair_records == []
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,kind", [(ForgePilotError("llm_http", "HTTP 401. Check credentials"), "credentials"),
    (ForgePilotError("llm_timeout", "deadline exceeded"), "llm_timeout"),
    (reply("not JSON"), "invalid_reflection"), (reflection(category="environment"), "environment")])
async def test_reflection_failure_preserves_diagnostics(cpp_repo, rg_exe, git_exe, fake_validation, answer, kind):
    svc, _, _ = service(cpp_repo, rg_exe, git_exe, script(0) + [answer])
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.failure_kind == kind
        assert result.repair_round == 1 and result.repair_records[-1]["status"] == "failed"
        assert result.last_validation_error["phase"] == "cmake_build" and result.validation_history[0]["commands"]
        assert result.task_diff["files"] and result.final_result is None
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_tool", [False, True])
async def test_reflection_read_only_and_cancel(cpp_repo, rg_exe, git_exe, fake_validation, during_tool):
    entered = asyncio.Event()
    calls = [write("main.cpp", "forbidden")]
    if during_tool:
        calls.append(call("read_file", '{"path":"main.cpp"}', "reflection-read"))
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, script(0) + [reply(calls=calls)])
    original_chat = llm.chat
    async def chat(messages, tools=None):
        if len(llm.requests) == 5:
            assert "reflection_read_only" in messages[-1]["content"]
            entered.set()
            await asyncio.Future()
        return await original_chat(messages, tools)
    llm.chat = chat
    try:
        pending = await svc.start(str(cpp_repo), "fix")
        tool = svc._runtime.registry.get("read_file")
        execute = tool.execute
        async def inspect(arguments):
            if during_tool and svc.state == TaskState.REFLECTING:
                entered.set()
                await asyncio.Future()
            return await execute(arguments)
        tool.execute = inspect
        svc.approve(pending.task_id, 1)
        running = asyncio.create_task(svc.execute(pending.task_id))
        await asyncio.wait_for(entered.wait(), 5)
        assert svc.cancel()
        result = await running
        assert result.state == "cancelled" and result.repair_round == 1
        assert result.repair_records[-1]["status"] == "cancelled" and result.task_diff["files"]
        assert "broken" in (cpp_repo / "main.cpp").read_text()
        if during_tool:
            finished = [e.data for e in trace.events if e.kind == "tool_finished"
                        and e.data.get("tool_call_id") == "reflection-read"]
            assert len(finished) == 1 and finished[0]["error"].startswith("cancelled:")
            assert finished[0]["repair_round"] == 1
            assert result.conversation[-1]["tool_call_id"] == "reflection-read"
        assert svc._active is None and svc._lock.handle is None
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_reflection_blocks_writers_and_builds_then_repairs_with_edit(cpp_repo, rg_exe, git_exe, fake_validation):
    names = sorted(WRITE_TOOLS | set(BUILD_TOOLS))
    forbidden = [call(name, "{}", "reflect-" + name) for name in names]
    messages = script(0) + [reply(calls=forbidden), reflection(), reply(calls=[call("edit_file",
        json.dumps({"path": "main.cpp", "edits": [{"old_text": "broken", "new_text": "0"}]}), "repair-edit")]), reply("repaired")]
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, messages)
    original_chat = llm.chat

    async def chat(messages, tools=None):
        if svc.state == TaskState.REFLECTING:
            assert not set(names) & {tool.name for tool in tools}
        return await original_chat(messages, tools)

    llm.chat = chat
    try:
        pending = await svc.start(str(cpp_repo), "fix")
        execute = svc._runtime.registry.execute
        attempted = []

        async def observe(name, arguments):
            if svc.state == TaskState.REFLECTING and name in names:
                attempted.append(name)
            return await execute(name, arguments)

        svc._runtime.registry.execute = observe
        svc.approve(pending.task_id, 1)
        done = await svc.execute(pending.task_id)
        assert done.state == TaskState.COMPLETED, done.error
        assert done.repair_round == 1 and len(done.validation_history) == 2
        assert not attempted
        blocked = [r for r in done.tool_results if r.get("tool_call_id", "").startswith("reflect-")]
        assert [r["tool_call_id"] for r in blocked] == [c["id"] for c in forbidden]
        assert all(r["error"].startswith("reflection_read_only:") for r in blocked)
        assert (cpp_repo / "main.cpp").read_bytes() == b"int main(){return 0;}\n"
        assert any(r.get("tool_call_id") == "repair-edit" and r["success"] for r in done.tool_results)
    finally:
        svc.close()


@pytest.mark.parametrize("path,old,new", [
    ("tests/check.cpp", "int main(){return 1;}", "int main(){return 0;}"),
    ("main.cpp", "assert(x == 42);", "assert(true);"),
    ("CMakeLists.txt", "add_test(NAME check COMMAND x)\n", ""),
    ("CMakeLists.txt", "add_test(\nNAME check\nCOMMAND x)\n", "add_test(\nNAME check\nCOMMAND y)\n"),
    ("CMakeLists.txt", "", "set_tests_properties(check PROPERTIES DISABLED TRUE)"),
    ("main.cpp", "int x;", "#define NDEBUG\nint x;"),
    ("main.cpp", "int x;", "#pragma warning(disable: 123)\nint x;")])
def test_cannot_manufacture_success(path, old, new):
    with pytest.raises(ForgePilotError, match="validation_integrity"):
        ensure_validation_integrity(path, old.encode(), new)


@pytest.mark.asyncio
async def test_policy_violation_stops_without_writing(cpp_repo, rg_exe, git_exe, fake_validation):
    svc, _, _ = service(cpp_repo, rg_exe, git_exe, script(0) + [reflection(),
        reply(calls=[write("main.cpp", "#define NDEBUG\nint main(){return 0;}")])])
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.failure_kind == "validation_integrity"
        assert len(result.validation_history) == 1 and "NDEBUG" not in (cpp_repo / "main.cpp").read_text()
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_tool", ["write_file", "edit_file"])
async def test_real_compile_failure_then_repair(build_repo, build_config, rg_exe, git_exe, repair_tool):
    original = (build_repo / "main.cpp").read_bytes()
    index = (build_repo / ".git/index").read_bytes()
    repair = (write("main.cpp", "int main(){return 0;}\n// existing user change\n") if repair_tool == "write_file" else
        call("edit_file", json.dumps({"path": "main.cpp", "edits": [{"old_text": "missing_identifier", "new_text": "0"}]})))
    messages = script(0, initial="int main(){return missing_identifier;}\n// existing user change\n") + [reflection(),
        reply(calls=[repair]), reply("fixed undefined name")]
    svc, llm, trace = service(build_repo, rg_exe, git_exe, messages, build_config)
    try:
        result = await finish(svc, build_repo)
        assert result.state == "completed", result.error
        assert result.repair_round == 1 and result.steps == 7
        initial, repair = result.validation_history
        assert list(initial["stages"].values()) == ["succeeded", "failed", "not_run"]
        assert initial["commands"][-1]["returncode"] != 0
        assert "missing_identifier" in initial["commands"][-1]["stdout"]
        assert initial["tests"]["total"] is None
        assert list(repair["stages"].values()) == ["succeeded"] * 3
        assert repair["tests"]["passed"] == repair["tests"]["total"] == 1
        assert all(c["returncode"] == 0 for c in repair["commands"])
        assert "missing_identifier" in json.dumps(llm.requests[4])
        assert (build_repo / ".git/index").read_bytes() == index
        assert (build_repo / "notes.txt").read_text() == "user untracked\n"
        print("REAL PHASE5 REPAIR " + json.dumps({"state": result.state, "steps": result.steps,
            "repair_round": result.repair_round, "validation_history": result.validation_history,
            "repairs": result.repair_records, "diff": result.task_diff,
            "states": [e.data for e in trace.events if e.kind == "task_state_changed"]}, ensure_ascii=False))
        restored = await svc.restore(result.task_id)
        assert restored["conflicts"] == 0 and (build_repo / "main.cpp").read_bytes() == original
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_real_configuration_repair_reconfigures(build_repo, build_config, rg_exe, git_exe):
    cmake = (build_repo / "CMakeLists.txt").read_text()
    messages = script(0, initial="int main(){return FORGEPILOT_VALUE;}\n// existing user change\n")
    messages[1] = reply(json.dumps(proposal("main.cpp", "CMakeLists.txt")))
    messages += [reflection("main.cpp", "CMakeLists.txt"),
        reply(calls=[call("read_file", '{"path":"CMakeLists.txt"}', "read-cmake"),
                     write("CMakeLists.txt", cmake + "\ntarget_compile_definitions(counter PRIVATE FORGEPILOT_VALUE=0)\n")]),
        reply("Added missing production compile definition; preserved tests")]
    svc, _, _ = service(build_repo, rg_exe, git_exe, messages, build_config)
    try:
        result = await finish(svc, build_repo)
        assert result.state == "completed" and result.repair_round == 1, result.error
        assert result.validation_history[0]["stages"]["cmake_build"] == "failed"
        assert [c["phase"] for c in result.validation_history[1]["commands"]] == list(BUILD_TOOLS)
        assert result.build_result["tests"]["passed"] == 1
        assert "add_test(NAME counter_test COMMAND counter)" in (build_repo / "CMakeLists.txt").read_text()
        print("REAL CONFIGURATION REPAIR " + json.dumps(result.validation_history, ensure_ascii=False))
    finally:
        svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", BUILD_TOOLS)
async def test_cancel_during_repair_validation(cpp_repo, rg_exe, git_exe, fake_validation, monkeypatch, phase):
    entered = asyncio.Event()
    command = BuildPipeline._command
    async def blocked(pipeline, current, argv, timeout):
        if pipeline._report["repair_round"] == 1 and current == phase:
            entered.set()
            await asyncio.Future()
        return await command(pipeline, current, argv, timeout)
    monkeypatch.setattr(BuildPipeline, "_command", blocked)
    svc, _, trace = service(cpp_repo, rg_exe, git_exe, script(1))
    try:
        pending = await svc.start(str(cpp_repo), "fix")
        svc.approve(pending.task_id, 1)
        running = asyncio.create_task(svc.execute(pending.task_id))
        await asyncio.wait_for(entered.wait(), 5)
        svc.cancel()
        result = await running
        assert result.state == "cancelled" and result.failure_kind == "cancelled"
        assert result.repair_round == 1 and result.repair_records[-1]["status"] == "cancelled"
        assert len(result.validation_history) == 2 and result.build_result["stages"][phase] == "cancelled"
        assert result.last_validation_error["repair_round"] == 1 and result.task_diff["files"]
        assert [e for e in trace.events if e.kind == "validation_finished"][-1].data["kind"] == "cancelled"
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_repair_timeout_is_not_retried(cpp_repo, rg_exe, git_exe, fake_validation, monkeypatch):
    command = BuildPipeline._command
    async def timeout(pipeline, phase, argv, seconds):
        if pipeline._report["repair_round"] == 1:
            fake_validation.update(failures=["cmake_build", "cmake_build"], status="timed_out")
        return await command(pipeline, phase, argv, seconds)
    monkeypatch.setattr(BuildPipeline, "_command", timeout)
    svc, llm, _ = service(cpp_repo, rg_exe, git_exe, script(1))
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.failure_kind == "timed_out"
        assert result.repair_round == 1 and result.steps == 7 and len(result.validation_history) == 2
        assert result.build_result["stages"]["run_tests"] == "not_run"
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_reflection_evidence_and_trace_redaction(cpp_repo, rg_exe, git_exe, fake_validation):
    secret = "offline-phase5-secret"
    fake_validation["diagnostic"] += " " + secret
    redactor = Redactor((secret,))
    svc, llm, trace = service(cpp_repo, rg_exe, git_exe, script(1), redactor=redactor)
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "completed"
        assert secret not in json.dumps(result.repair_records) + json.dumps(result.validation_history)
        assert secret not in json.dumps([asdict(e) for e in trace.events]) + json.dumps(llm.requests)
        assert "[REDACTED]" in json.dumps(result.repair_records)
    finally:
        svc.close()


@pytest.mark.asyncio
async def test_missing_tests_cannot_become_success(cpp_repo, rg_exe, git_exe, fake_validation, monkeypatch):
    fake_validation["failures"] = ["run_tests"]
    command = BuildPipeline._command
    async def remove_names(pipeline, phase, argv, timeout):
        if pipeline._report["repair_round"] == 1:
            fake_validation["names"] = ["substitute_test"]
        return await command(pipeline, phase, argv, timeout)
    monkeypatch.setattr(BuildPipeline, "_command", remove_names)
    svc, _, _ = service(cpp_repo, rg_exe, git_exe, script(1))
    try:
        result = await finish(svc, cpp_repo)
        assert result.state == "failed" and result.failure_kind == "validation_integrity"
        assert result.final_result is None and result.repair_round == 1
    finally:
        svc.close()
