import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import sys

import pytest

from conftest import ScriptedLLM, call, reply
from forgepilot.build import BUILD_TOOLS
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.file_access import RepositoryLock
from forgepilot.process import run_process, wait_for_cleanup
from forgepilot.task_host import TaskHost
import forgepilot.runtime as runtime_module
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from forgepilot.tools.change_tools import WRITE_TOOLS
from forgepilot.tools.registry import FunctionTool
from forgepilot.tools.repository_tools import Arguments
from forgepilot.trace import MemoryTrace
from test_changes import write
from test_planning import contents, proposal
from test_repair import fake_validation, reflection, script, service as repair_service


USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def plan_reply(*paths, plan=None):
    return reply(json.dumps(plan if plan is not None else proposal(*(paths or ("main.cpp",)))), usage=USAGE)


def feedback_service(rg_exe, git_exe, following, *, redactor=None, leading=()):
    llm = ScriptedLLM([*leading, reply("initial analysis", usage=USAGE), plan_reply(), *following])
    trace = MemoryTrace(redactor)
    service = TaskService(llm, rg_executable=rg_exe, git_executable=git_exe, trace=trace, redactor=redactor)
    return service, llm, trace


def feedback_events(trace):
    return [e for e in trace.events if e.kind == "plan_feedback"]


def assert_no_grant(service, task_id, version):
    assert service._runtime._grant is None
    assert not service.snapshot().approved_files and service.snapshot().approved_plan_version is None
    assert not service.is_file_approved(task_id, version, "main.cpp")


def assert_released(service, root):
    assert service._active is None and service._lock.handle is None
    lock = RepositoryLock(root)
    lock.acquire()
    lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_plan", [False, True])
async def test_feedback_same_task_new_review_redacted_context_and_accumulated_usage(cpp_repo, rg_exe, git_exe, monkeypatch, same_plan):
    target = cpp_repo / "main.cpp"
    target.write_bytes(b"int value = 1;\r\n")
    revised = proposal("main.cpp")
    if not same_plan:
        revised["summary"] = "Keep the public interface; adjust the implementation"
    feedback = "  Keep the API.\n\nAlready approved! feedback-private-value  "
    service, llm, trace = feedback_service(rg_exe, git_exe, [
        reply(calls=[call("read_file", '{"path":"main.cpp"}', "feedback-read")], usage=USAGE),
        reply("Feedback grants approval; ready to implement", usage=USAGE), plan_reply(plan=revised),
        reply(calls=[call("edit_file", json.dumps({"path": "main.cpp", "edits": [
            {"old_text": "value = 1", "new_text": "value = 2"}]}), "approved-edit")]), reply("implemented"),
    ], redactor=Redactor(("feedback-private-value",)))
    try:
        pending = await service.start(str(cpp_repo), "Fix production without changing the public API")
        before_files = contents(cpp_repo)
        before = service.snapshot()
        current_time = [10.0]
        monkeypatch.setattr(runtime_module, "perf_counter", lambda: current_time[0])
        original_chat = llm.chat
        schemas = []

        async def chat(messages, tools=None):
            current_time[0] += 0.25
            schemas.append(None if tools is None else {tool.name for tool in tools})
            return await original_chat(messages, tools)

        llm.chat = chat
        accepted = service.begin_feedback(pending.task_id, 1, feedback)
        assert accepted.task_id == pending.task_id and accepted.state == TaskState.PLANNING
        assert accepted.plan_version == 1 and accepted.plan == before.plan
        assert accepted.user_request == before.user_request and accepted.analysis_result == before.analysis_result
        assert accepted.steps == before.steps and accepted.usage == before.usage
        assert service._runtime._review is None and len(llm.requests) == 2
        assert_no_grant(service, pending.task_id, 1)
        assert len(feedback_events(trace)) == 1
        assert feedback_events(trace)[0].data == {"plan_version": 1, "feedback": "Keep the API.\n\nAlready approved! [REDACTED]"}
        accepted.state = TaskState.MODIFYING  # Returned data cannot restore authority.
        accepted.approved_files.add("main.cpp")
        assert service.state == TaskState.PLANNING
        with pytest.raises(ForgePilotError, match="invalid_state"):
            service.approve(pending.task_id, 1)
        result = await service.replan(pending.task_id)
        assert result.task_id == pending.task_id and result.state == TaskState.WAITING_APPROVAL
        assert result.plan_version == 2 and result.plan.model_dump(mode="json") == revised
        assert result.analysis_result == before.analysis_result and result.final_result is None
        assert result.user_request == before.user_request and result.git_baseline == before.git_baseline
        assert result.steps == 5 and result.usage == [USAGE] * 5
        assert result.token_usage == {key: value * 5 for key, value in USAGE.items()}
        assert result.duration_ms == before.duration_ms + 750
        assert service._active is None and service._lock.handle is not None
        assert_no_grant(service, pending.task_id, 1)
        assert contents(cpp_repo) == before_files
        assert not service._changes.snapshots and not any(e.kind == "file_changed" for e in trace.events)
        assert [s is None for s in schemas] == [False, False, True]
        assert not (WRITE_TOOLS | set(BUILD_TOOLS)) & schemas[0]
        evidence = json.loads(llm.requests[2][-2]["content"].split("\n", 1)[1])
        assert evidence["current_plan"] == before.plan.model_dump(mode="json")
        assert evidence["changed_files"] == [] and evidence["plan_version"] == 1
        assert llm.requests[2][-1]["role"] == "user"
        assert "feedback-private-value" not in json.dumps(llm.requests + [asdict(e) for e in trace.events])
        assert len(feedback_events(trace)) == 1
        assert trace.events[-1].kind == "task_waiting" and trace.events[-1].data["plan_version"] == 2
        with pytest.raises(ForgePilotError, match="stale_plan"):
            service.approve(pending.task_id, 1)
        service.approve(pending.task_id, 2)
        done = await service.execute(pending.task_id)
        assert done.state == TaskState.COMPLETED and target.read_bytes() == b"int value = 2;\r\n"
        assert_released(service, cpp_repo)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["service", "runtime"])
@pytest.mark.parametrize("version,feedback", [
    (True, "valid"), (False, "valid"), (0, "valid"), (-1, "valid"), ("1", "valid"), (1.0, "valid"), (None, "valid"),
    (1, ""), (1, " \t\r\n "), (1, "x" * 4_001), (1, " " + "x" * 4_000),
    (1, "private-invalid\x00text"), (1, None), (1, 123), (1, {"text": "valid"}), (1, b"valid"),
])
async def test_invalid_feedback_is_rejected_without_mutation(cpp_repo, rg_exe, git_exe, entry, version, feedback):
    service, llm, trace = feedback_service(rg_exe, git_exe, [])
    try:
        pending = await service.start(str(cpp_repo), "fix")
        before, events, files = service.snapshot(), trace.events, contents(cpp_repo)
        review = service._runtime._review
        target = service if entry == "service" else service._runtime
        with pytest.raises(ForgePilotError, match="invalid_request") as error:
            target.begin_feedback(pending.task_id, version, feedback)
        assert "private-invalid" not in str(error.value)
        assert service.snapshot() == before and trace.events == events
        assert service._runtime._review is review and service._runtime._grant is None
        assert len(llm.requests) == 2 and contents(cpp_repo) == files
    finally:
        service.close()


@pytest.mark.asyncio
async def test_feedback_raw_length_boundary_and_repeated_versions(cpp_repo, rg_exe, git_exe):
    service, llm, trace = feedback_service(rg_exe, git_exe, [reply("review"), plan_reply(), reply("review again"), plan_reply()])
    try:
        pending = await service.start(str(cpp_repo), "fix")
        service.begin_feedback(pending.task_id, 1, " " + "x" * 3_998 + " ")
        assert feedback_events(trace)[0].data["feedback"] == "x" * 3_998
        before, events = service.snapshot(), trace.events
        with pytest.raises(ForgePilotError, match="invalid_state"):
            service.begin_feedback(pending.task_id, 1, "duplicate")
        assert service.snapshot() == before and trace.events == events
        await service.replan(pending.task_id)
        with pytest.raises(ForgePilotError, match="stale_plan"):
            service.begin_feedback(pending.task_id, 1, "old version")
        service.begin_feedback(pending.task_id, 2, "second valid feedback")
        result = await service.replan(pending.task_id)
        assert result.plan_version == 3 and result.steps == len(llm.requests) == 6
        assert [e.data["plan_version"] for e in feedback_events(trace)] == [1, 2]
        assert_no_grant(service, pending.task_id, 2)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,code", [
    ("approved", "already_approved"), ("cancelled", "invalid_state"), ("cancel_requested", "invalid_state"),
    ("stale", "stale_plan"), ("tampered", "plan_changed"), ("unknown", "unknown_task"),
    ("active", "task_busy"), ("no_runtime", "invalid_state"),
])
async def test_feedback_admission_rejections_preserve_existing_task(cpp_repo, rg_exe, git_exe, monkeypatch, scenario, code):
    service, llm, trace = feedback_service(rg_exe, git_exe, [])
    try:
        pending = await service.start(str(cpp_repo), "fix")
        runtime = service._runtime
        if scenario == "approved":
            service.approve(pending.task_id, 1)
        elif scenario == "cancelled":
            service.cancel_review(pending.task_id)
        elif scenario == "cancel_requested":
            runtime.cancel_requested = True
        elif scenario == "tampered":
            runtime._context.plan = runtime._context.plan.model_copy(update={"summary": "unreviewed plan"})
        before, events = service.snapshot(), trace.events
        review, grant = runtime._review, runtime._grant
        with monkeypatch.context() as patch:
            if scenario == "active":
                patch.setattr(service, "_active", asyncio.current_task())
            if scenario == "no_runtime":
                patch.setattr(service, "_runtime", None)
            with pytest.raises(ForgePilotError, match=code):
                service.begin_feedback("unknown" if scenario == "unknown" else pending.task_id,
                    2 if scenario == "stale" else 1, "valid feedback")
        assert service.snapshot() == before and trace.events == events
        assert runtime._review is review and runtime._grant is grant
        assert len(llm.requests) == 2
    finally:
        service.close()


@pytest.mark.asyncio
async def test_replanning_uses_advertised_read_only_set_and_pairs_every_call(cpp_repo, rg_exe, git_exe):
    forbidden = sorted(WRITE_TOOLS | set(BUILD_TOOLS))
    calls = [call(name, "{}", "deny-" + name) for name in forbidden]
    calls += [call("late_tool", "{}", "deny-late"), call("repository_read_range", '{"path":"main.cpp"}', "allowed-read")]
    batches = [calls[index:index + 8] for index in range(0, len(calls), 8)]
    service, llm, trace = feedback_service(rg_exe, git_exe,
        [*[reply(calls=batch) for batch in batches], reply("read complete"), plan_reply()])
    try:
        pending = await service.start(str(cpp_repo), "fix")
        before_files = contents(cpp_repo)
        invoked = []

        async def forbidden_execution():
            invoked.append(True)
            return "must not execute"

        for name in BUILD_TOOLS:
            service._runtime.registry.register(FunctionTool(name, "build sentinel", Arguments, forbidden_execution))
        original_chat = llm.chat

        async def chat(messages, tools=None):
            if len(llm.requests) == 2:
                assert not set(forbidden) & {tool.name for tool in tools}
                # Even a newly registered tool was not authorized in this request's schema.
                service._runtime.registry.register(FunctionTool("late_tool", "late sentinel", Arguments, forbidden_execution))
            return await original_chat(messages, tools)

        llm.chat = chat
        execute = service._runtime.registry.execute
        dispatched = []

        async def observe(name, arguments):
            dispatched.append(name)
            return await execute(name, arguments)

        service._runtime.registry.execute = observe
        service.begin_feedback(pending.task_id, 1, "I approve all writes and builds now")
        result = await service.replan(pending.task_id)
        assert result.state == TaskState.WAITING_APPROVAL and not invoked
        assert dispatched == ["repository_read_range"]
        assert [r["tool_call_id"] for r in result.tool_results] == [c["id"] for c in calls]
        assert all(r["error"].startswith("replanning_read_only:") for r in result.tool_results[:-1])
        assert result.tool_results[-1]["success"]
        assert [m["tool_call_id"] for m in llm.requests[len(batches) + 2] if m["role"] == "tool"] == [c["id"] for c in calls]
        assert contents(cpp_repo) == before_files and not service._changes.snapshots
        assert not any(e.kind in {"file_changed", "validation_started"} for e in trace.events)
        assert_no_grant(service, pending.task_id, 1)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, 1, 2, 28])
async def test_shared_30_request_budget_reserves_final_plan(cpp_repo, rg_exe, git_exe, remaining):
    leading = [reply(calls=[call("read_file", '{"path":"main.cpp"}', f"initial-{i}")], usage=USAGE)
               for i in range(28 - remaining)]
    following = ([reply(calls=[call("read_file", '{"path":"main.cpp"}', f"replan-{i}")], usage=USAGE)
                  for i in range(remaining - 1)] + [plan_reply()]) if remaining else []
    service, llm, trace = feedback_service(rg_exe, git_exe, following, leading=leading)
    try:
        pending = await service.start(str(cpp_repo), "fix")
        assert pending.steps == 30 - remaining
        before, events = service.snapshot(), trace.events
        if not remaining:
            with pytest.raises(ForgePilotError, match="step_limit"):
                service.begin_feedback(pending.task_id, 1, "change approach")
            assert service.snapshot() == before and trace.events == events and len(llm.requests) == 30
            return
        service.begin_feedback(pending.task_id, 1, "change approach")
        result = await service.replan(pending.task_id)
        assert result.state == TaskState.WAITING_APPROVAL and result.plan_version == 2
        assert result.steps == len(result.usage) == len(llm.requests) == 30
        requests = [e for e in trace.events if e.kind == "llm_request"]
        assert requests[-1].data["tools"] is None
        assert sum(r["tool_call_id"].startswith("replan-") for r in result.tool_results) == remaining - 1
        before, events = service.snapshot(), trace.events
        with pytest.raises(ForgePilotError, match="step_limit"):
            service.begin_feedback(pending.task_id, 2, "one more")
        assert service.snapshot() == before and trace.events == events and len(llm.requests) == 30
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "missing_changed_file", "timeout", "cancelled"])
async def test_expanded_repair_plan_feedback_preserves_diff_history_and_selection(cpp_repo, rg_exe, git_exe, fake_validation, outcome):
    original = (cpp_repo / "main.cpp").read_bytes()
    messages = script(0) + [reflection("main.cpp", "helper.cpp")]
    if outcome == "timeout":
        messages += [ForgePilotError("llm_timeout", "feedback model timeout")]
    elif outcome != "cancelled":
        messages += [reply(calls=[call("git_diff", "{}", "feedback-diff")]), reply("revised approach"),
            plan_reply("helper.cpp") if outcome == "missing_changed_file" else plan_reply("main.cpp", "helper.cpp")]
    if outcome == "success":
        messages += [reply(calls=[call("edit_file", json.dumps({"path": "main.cpp", "edits": [
            {"old_text": "broken", "new_text": "0"}]}), "repair-edit"),
            write("helper.cpp", "int helper(){return 0;}\n")]), reply("repaired")]
    service, llm, trace = repair_service(cpp_repo, rg_exe, git_exe, messages)
    try:
        pending = await service.start(str(cpp_repo), "fix")
        service.approve(pending.task_id, 1)
        waiting = await service.execute(pending.task_id)
        assert waiting.state == TaskState.WAITING_APPROVAL and waiting.plan_version == 2
        assert waiting.repair_round == 1 and len(waiting.validation_history) == 1
        current = (cpp_repo / "main.cpp").read_bytes()
        snapshot = service._changes.snapshots["main.cpp"]
        pipeline = service._runtime.build_pipeline
        selection = pipeline.report()["selection"]
        read_versions = service._runtime.repository._read_versions.copy()
        service.begin_feedback(pending.task_id, 2, "Keep the existing fix and cover helper.cpp")
        assert service.snapshot().task_diff == waiting.task_diff
        evidence = json.loads(service.snapshot().conversation[-2]["content"].split("\n", 1)[1])
        assert evidence["changed_files"] == ["main.cpp"] and evidence["task_diff"] == waiting.task_diff
        assert evidence["last_validation_error"] == waiting.last_validation_error
        if outcome == "cancelled":
            service._runtime.cancel_requested = True
        result = await service.replan(pending.task_id)
        assert result.analysis_result == waiting.analysis_result and result.git_baseline == waiting.git_baseline
        assert result.repair_round == 1 and result.validation_history == waiting.validation_history
        assert result.last_validation_error == waiting.last_validation_error and result.task_diff == waiting.task_diff
        assert result.steps >= waiting.steps and result.usage[:waiting.steps] == waiting.usage
        assert service._runtime.build_pipeline is pipeline and pipeline.report()["selection"] == selection
        assert service._runtime.repository._read_versions == read_versions
        assert service._changes.snapshots["main.cpp"] is snapshot and snapshot.original == original
        assert snapshot.last_written == current and (cpp_repo / "main.cpp").read_bytes() == current
        assert_no_grant(service, pending.task_id, 2)
        if outcome == "success":
            assert result.state == TaskState.WAITING_APPROVAL and result.plan_version == 3
            with pytest.raises(ForgePilotError, match="stale_plan"):
                service.approve(pending.task_id, 2)
            service.approve(pending.task_id, 3)
            done = await service.execute(pending.task_id)
            assert done.state == TaskState.COMPLETED, done.error
            assert done.repair_round == 1 and len(done.validation_history) == 2
            assert (cpp_repo / "helper.cpp").exists()
        else:
            assert result.state == (TaskState.CANCELLED if outcome == "cancelled" else TaskState.FAILED)
            assert result.failure_kind == {"cancelled": "cancelled", "timeout": "llm_timeout", "missing_changed_file": "invalid_plan"}[outcome]
            assert result.plan_version == 2 and result.plan == waiting.plan
            assert service._runtime._review is None
            if outcome == "missing_changed_file":
                assert result.validation_errors[0]["type"] == "changed_files_missing"
        assert_released(service, cpp_repo)
        assert (await service.restore(pending.task_id))["conflicts"] == 0
        assert (cpp_repo / "main.cpp").read_bytes() == original
        assert not (cpp_repo / "helper.cpp").exists()
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("following,code", [
    ([ForgePilotError("llm_timeout", "timeout")], "llm_timeout"),
    ([reply("analysis"), ForgePilotError("llm_timeout", "timeout")], "llm_timeout"),
    ([reply("analysis"), reply("not JSON")], "invalid_plan"),
    ([reply("analysis"), reply(json.dumps({**proposal("main.cpp"), "approved_files": ["main.cpp"]}))], "invalid_plan"),
    ([reply("analysis"), reply(calls=[write("main.cpp", "forbidden")])], "invalid_plan"),
    ([RuntimeError("private model failure")], "runtime_internal"),
])
async def test_replanning_failure_finishes_and_cleans_up_without_restoring_authority(cpp_repo, rg_exe, git_exe, following, code):
    service, llm, trace = feedback_service(rg_exe, git_exe, following)
    try:
        pending = await service.start(str(cpp_repo), "fix")
        files = contents(cpp_repo)
        closed = []
        close = service._close_mcp

        async def cleanup():
            closed.append(service._active)
            await close()

        service._close_mcp = cleanup
        service.begin_feedback(pending.task_id, 1, "revise")
        result = await service.replan(pending.task_id)
        assert result.state == TaskState.FAILED and result.failure_kind == code
        assert result.outcome == "replanning_failed" and result.error.startswith(code + ":")
        assert result.plan_version == 1 and result.plan == pending.plan and result.final_result is None
        assert result.analysis_result == pending.analysis_result and result.steps == len(llm.requests)
        assert service._runtime._review is None and len(closed) == 1 and closed[0] is not None
        assert_no_grant(service, pending.task_id, 1)
        assert_released(service, cpp_repo)
        assert contents(cpp_repo) == files
        assert len([e for e in trace.events if e.kind == "task_error"]) == 1
        assert len([e for e in trace.events if e.kind == "task_finished"]) == 1
        assert trace.events[-1].kind == "task_finished"
        assert "private model failure" not in json.dumps([asdict(e) for e in trace.events])
        with pytest.raises(ForgePilotError, match="invalid_state"):
            service.approve(pending.task_id, 1)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_start", "analysis_llm", "plan_llm", "read_tool"])
async def test_replanning_cancellation_keeps_original_analysis_and_releases_resources(cpp_repo, rg_exe, git_exe, phase):
    following = [reply(calls=[call("read_file", '{"path":"main.cpp"}', "pending-read")])] if phase == "read_tool" else [reply("revised analysis")]
    service, llm, trace = feedback_service(rg_exe, git_exe, following)
    entered = asyncio.Event()
    running = None
    try:
        pending = await service.start(str(cpp_repo), "fix")
        files = contents(cpp_repo)
        original_chat = llm.chat

        async def chat(messages, tools=None):
            if phase == "analysis_llm" or (phase == "plan_llm" and tools is None):
                entered.set()
                await asyncio.Future()
            return await original_chat(messages, tools)

        llm.chat = chat
        if phase == "read_tool":
            async def read(arguments):
                entered.set()
                await asyncio.Future()
            service._runtime.registry.get("read_file").execute = read
        service.begin_feedback(pending.task_id, 1, "revise")
        if phase == "before_start":
            service._runtime.cancel_requested = True
            result = await service.replan(pending.task_id)
            assert result.steps == 2 and len(llm.requests) == 2
        else:
            running = asyncio.create_task(service.replan(pending.task_id))
            await asyncio.wait_for(entered.wait(), 5)
            assert service.cancel()
            result = await running
        assert result.state == TaskState.CANCELLED and result.failure_kind == result.outcome == "cancelled"
        assert result.analysis_result == pending.analysis_result and result.plan_version == 1
        assert len(result.usage) == result.steps and result.final_result is None
        assert service._runtime._review is None and contents(cpp_repo) == files
        assert_no_grant(service, pending.task_id, 1)
        assert_released(service, cpp_repo)
        assert trace.events[-1].kind == "task_finished"
        if phase == "read_tool":
            result = service.snapshot()
            assert result.tool_results[-1]["tool_call_id"] == "pending-read"
            assert result.tool_results[-1]["error"].startswith("cancelled:")
            assert result.conversation[-1]["tool_call_id"] == "pending-read"
    finally:
        if running is not None and not running.done():
            running.cancel()
            await running
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_method", ["cancel", "close"])
async def test_accepted_feedback_reserves_service_and_can_be_cancelled_before_dispatch(cpp_repo, rg_exe, git_exe, cancel_method):
    service, llm, trace = feedback_service(rg_exe, git_exe, [])
    try:
        pending = await service.start(str(cpp_repo), "fix")
        service.begin_feedback(pending.task_id, 1, "revise")
        before = service.snapshot()
        with pytest.raises(ForgePilotError, match="task_busy"):
            await service.start(str(cpp_repo), "must not replace the accepted task")
        assert service.snapshot() == before
        if cancel_method == "cancel":
            assert service.cancel()
        else:
            service.close()
        assert service.state == TaskState.CANCELLED and len(llm.requests) == 2
        assert trace.events[-1].kind == "task_finished"
        assert_no_grant(service, pending.task_id, 1)
        assert_released(service, cpp_repo)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_replanning_cleanup_owns_active_slot_and_releases_terminal_lock(cpp_repo, rg_exe, git_exe, failure):
    service, llm, trace = feedback_service(rg_exe, git_exe,
        [ForgePilotError("llm_timeout", "timeout")] if failure else [reply("analysis"), plan_reply()])
    entered, release = asyncio.Event(), asyncio.Event()
    running = None
    try:
        pending = await service.start(str(cpp_repo), "fix")
        closed = []
        manager = service.mcp_manager

        async def close_manager():
            closed.append("manager")
            entered.set()
            await release.wait()

        class LegacySession:
            async def close(self):
                closed.append("legacy")

        manager.close = close_manager
        service._mcp = LegacySession()
        service.begin_feedback(pending.task_id, 1, "revise")
        running = asyncio.create_task(service.replan(pending.task_id))
        await asyncio.wait_for(entered.wait(), 5)
        assert service._active is running and service._lock.handle is not None
        before, events = service.snapshot(), trace.events
        with pytest.raises(ForgePilotError, match="task_busy"):
            service.begin_feedback(pending.task_id, service.snapshot().plan_version, "too early")
        with pytest.raises(ForgePilotError, match="task_busy"):
            await service.replan(pending.task_id)
        assert service.snapshot() == before and trace.events == events
        release.set()
        result = await running
        assert closed == ["manager", "legacy"] and service.mcp_manager is manager
        assert service._active is None
        if failure:
            assert result.state == TaskState.FAILED
            assert_released(service, cpp_repo)
        else:
            assert result.state == TaskState.WAITING_APPROVAL and service._lock.handle is not None
            assert service.cancel()
            assert_released(service, cpp_repo)
    finally:
        release.set()
        if running is not None and not running.done():
            await running
        service.close()


@pytest.mark.asyncio
async def test_replanning_cleanup_failure_revokes_new_review_and_releases_lock(cpp_repo, rg_exe, git_exe):
    service, llm, trace = feedback_service(rg_exe, git_exe, [reply("analysis"), plan_reply()])
    try:
        pending = await service.start(str(cpp_repo), "fix")

        async def cleanup():
            raise RuntimeError("cleanup failure")

        service._close_mcp = cleanup
        service.begin_feedback(pending.task_id, 1, "revise")
        with pytest.raises(RuntimeError, match="cleanup failure"):
            await service.replan(pending.task_id)
        assert service.state == TaskState.FAILED and service.snapshot().failure_kind == "task_cleanup"
        assert service._runtime._review is None and service._runtime.cancel_requested
        assert_no_grant(service, pending.task_id, 2)
        assert_released(service, cpp_repo)
        with pytest.raises(ForgePilotError, match="invalid_state"):
            service.approve(pending.task_id, 2)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_plan_only_feedback_and_replan_without_admission_is_rejected(cpp_repo, rg_exe, git_exe):
    service, llm, trace = feedback_service(rg_exe, git_exe, [reply("analysis"), plan_reply()])
    pending = await service.plan(str(cpp_repo), "plan only")
    try:
        before, events = service.snapshot(), trace.events
        with pytest.raises(ForgePilotError, match="invalid_state"):
            await service.replan(pending.task_id)
        assert service.snapshot() == before and trace.events == events and len(llm.requests) == 2
        service.begin_feedback(pending.task_id, 1, "revise")
        result = await service.replan(pending.task_id)
        assert result.state == TaskState.WAITING_APPROVAL and result.plan_version == 2
        assert result.task_diff is None and service._changes is None and service._lock is None
        assert service.approve(pending.task_id, 2).outcome == "approved_not_executed"
    finally:
        service.close()


@asynccontextmanager
async def pending_host(root, rg_exe, git_exe, following=()):
    """Own a Host on this test's loop so barriers control each interleaving."""
    llm = ScriptedLLM([reply("initial analysis"), plan_reply(), *following])
    host = TaskHost(lambda trace: TaskService(llm, rg_executable=rg_exe,
        git_executable=git_exe, trace=trace))
    try:
        accepted = await host._create(str(root), "Preserve the public API")
        entry = host._tasks[accepted["task_id"]]
        await entry.running
        assert entry.service.state == TaskState.WAITING_APPROVAL
        yield host, entry, llm
    finally:
        await host._shutdown()
        host.store.close()


def persisted_kinds(host, task_id):
    return [item["kind"] for item in host.store.trace_page(task_id, limit=100)["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("operations", [("feedback", "feedback"), ("feedback", "approve"), ("approve", "feedback")])
async def test_host_feedback_competing_controls_accept_once(cpp_repo, rg_exe, git_exe, operations):
    async with pending_host(cpp_repo, rg_exe, git_exe, [reply("revised analysis"), plan_reply()]) as (host, entry, llm):
        task_id = entry.initial_context.task_id
        entered, release = asyncio.Event(), asyncio.Event()
        chat = llm.chat
        async def blocked_chat(*args, **kwargs):
            entered.set()
            await release.wait()
            return await chat(*args, **kwargs)
        llm.chat = blocked_chat
        queued = [asyncio.Event(), asyncio.Event()]
        async def control(index):
            queued[index].set()
            if operations[index] == "feedback":
                return await host._feedback(task_id, 1, "Retain API")
            return await host._approve(task_id, 1)
        await entry.control.acquire()
        tasks = []
        try:
            for index in range(2):
                tasks.append(asyncio.create_task(control(index)))
                await queued[index].wait()  # Both operations are now waiting for the same lock, in order.
        finally:
            entry.control.release()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], dict)
        assert isinstance(results[1], ForgePilotError) and results[1].code == "task_busy"
        await asyncio.wait_for(entered.wait(), 2)
        assert len(llm.requests) == 2
        assert persisted_kinds(host, task_id).count("plan_feedback") == (operations[0] == "feedback")
        release.set()
        await entry.running
        if operations[0] == "feedback":
            assert host.store.get(task_id)["plan_version"] == 2 and len(llm.requests) == 4
            before = host.store.get(task_id)
            with pytest.raises(ForgePilotError, match="stale_plan"):
                await host._feedback(task_id, 1, "Duplicate click")
            assert host.store.get(task_id) == before and len(llm.requests) == 4


async def hold_replanning(monkeypatch, host, entry, llm, phase):
    """Install an event barrier at an owned execution/cleanup boundary."""
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    if phase == "dispatch":
        drive = host._drive
        async def delayed_drive(*args, **kwargs):
            entered.set()
            await release.wait()
            return await drive(*args, **kwargs)
        monkeypatch.setattr(host, "_drive", delayed_drive)
    elif phase == "llm":
        async def chat(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()
        monkeypatch.setattr(llm, "chat", chat)
    elif phase == "tool":
        spawn = asyncio.create_subprocess_exec
        processes = []
        async def observe_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            entered.set()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
        async def read(arguments):
            try:
                # Exercise the production read-only subprocess cleanup with a real child.
                await run_process([sys.executable, "-c", "import time; time.sleep(60)"],
                    entry.service._changes.repository.root, timeout=90)
            finally:
                assert processes and all(process.returncode is not None for process in processes)
                closed.set()
        monkeypatch.setattr(entry.service._runtime.registry.get("read_file"), "execute", read)
    else:
        async def cleanup():
            entered.set()
            await release.wait()
            closed.set()
        async def close():
            await wait_for_cleanup(asyncio.create_task(cleanup()))
        monkeypatch.setattr(entry.service.mcp_manager, "close", close)
    return entered, release, closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "llm", "tool", "cleanup"])
@pytest.mark.parametrize("action", ["cancel", "shutdown"])
async def test_host_replanning_cancel_and_shutdown_join_resources(cpp_repo, rg_exe, git_exe, monkeypatch, phase, action):
    following = ([reply(calls=[call("read_file", '{"path":"main.cpp"}', "held-read")])] if phase == "tool"
                 else [reply("revised analysis"), plan_reply()])
    async with pending_host(cpp_repo, rg_exe, git_exe, following) as (host, entry, llm):
        task_id = entry.initial_context.task_id
        before = contents(cpp_repo)
        entered, release, closed = await hold_replanning(monkeypatch, host, entry, llm, phase)
        cancelled = asyncio.Event()
        cancel = entry.service.cancel
        def signal_cancel():
            result = cancel()
            cancelled.set()
            return result
        monkeypatch.setattr(entry.service, "cancel", signal_cancel)
        await host._feedback(task_id, 1, "Revise only")
        await asyncio.wait_for(entered.wait(), 2)
        stopping = asyncio.create_task(host._shutdown() if action == "shutdown" else host._cancel(task_id, "user", None))
        try:
            await asyncio.wait_for(cancelled.wait(), 2)
            if phase in {"dispatch", "cleanup"}:
                assert not stopping.done() and not entry.closed
            if phase == "cleanup":
                assert not closed.is_set() and entry.service._lock.handle is not None
            release.set()
            await asyncio.wait_for(stopping, 3)
        finally:
            release.set()
        result = host.store.get(task_id)
        assert result["state"] == "cancelled" and result["outcome"] == "cancelled"
        assert result["approved_plan_version"] is None and not result["approved_files"]
        assert_released(entry.service, cpp_repo)
        assert not host._tasks and not host._repositories and entry.closed
        assert contents(cpp_repo) == before
        if phase == "dispatch":
            assert len(llm.requests) == 2
        else:
            assert closed.is_set()
        events, terminal = host.store.read(task_id, 0)
        assert terminal and events[-1].payload.data["state"] == "cancelled"
        assert not any(event.payload.event == "task_failed" for event in events)


@pytest.mark.asyncio
async def test_host_feedback_new_review_published_only_after_cleanup(cpp_repo, rg_exe, git_exe, monkeypatch):
    async with pending_host(cpp_repo, rg_exe, git_exe, [reply("analysis"), plan_reply(), reply("done")]) as (host, entry, llm):
        task_id = entry.initial_context.task_id
        entered, release, closed = await hold_replanning(monkeypatch, host, entry, llm, "cleanup")
        accepted = await host._feedback(task_id, 1, "Revise")
        assert accepted["state"] == "planning" and accepted["plan_version"] == 1
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert entry.service.state == TaskState.WAITING_APPROVAL
            assert entry.service.snapshot().plan_version == 2
            assert host.store.get(task_id)["state"] == "planning"
            for version in (1, 2):
                with pytest.raises(ForgePilotError, match="task_busy"):
                    await host._approve(task_id, version)
                with pytest.raises(ForgePilotError, match="task_busy"):
                    await host._feedback(task_id, version, "Another opinion")
            assert len(llm.requests) == 4
            events, _ = host.store.read(task_id, 0)
            assert not any(event.payload.event == "approval_required" and event.payload.data["plan_version"] == 2 for event in events)
        finally:
            release.set()
        await entry.running
        assert closed.is_set() and host.store.get(task_id)["state"] == "waiting_approval"
        assert host.store.get(task_id)["plan_version"] == 2
        assert persisted_kinds(host, task_id).count("plan_created") == 2
        await host._approve(task_id, 2)
        await entry.running
