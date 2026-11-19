import asyncio
from contextlib import contextmanager
import json
import socket
import sqlite3
from threading import Event, Thread
import time

import httpx
import pytest
import uvicorn

from conftest import ScriptedLLM, reply
from forgepilot.api import create_app
from forgepilot.security import Redactor
from forgepilot.task_service import TaskService
from test_api import frames, messages, until
from test_build import build_config, build_repo
from test_changes import write
from test_repair import reflection, script


@contextmanager
def serving(factory, *, heartbeat=0.05, **app_options):
    app = create_app(factory, heartbeat_seconds=heartbeat, **app_options)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                          timeout_graceful_shutdown=1))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "HTTP server failed to start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive(), "HTTP server or runtime worker failed to stop"


async def next_block(lines):
    block = []
    async with asyncio.timeout(10):
        while True:
            line = await anext(lines)
            if line == "":
                return "\n".join(block) + "\n\n"
            block.append(line)


@pytest.mark.asyncio
async def test_open_sse_ends_on_storage_failure_without_terminal_fiction(cpp_repo, rg_exe, git_exe, monkeypatch):
    release = Event()
    class Delayed(ScriptedLLM):
        async def chat(self, messages, tools=None):
            while not release.is_set(): await asyncio.sleep(.01)
            return await super().chat(messages, tools)
        async def aclose(self): pass
    def factory(trace):
        return TaskService(Delayed(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    with serving(factory) as (url, app):
        store = app.state.task_host.store
        publish = store._publish
        def fail(tid, snapshot, events, traces, closed, **kwargs):
            result = publish(tid, snapshot, events, traces, closed, **kwargs)
            if any(t.kind == 'llm_response' for t in traces):
                raise sqlite3.OperationalError('injected commit failure')
            return result
        monkeypatch.setattr(store, '_publish', fail)
        async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
            response = await client.post('/api/v1/tasks', json=dict(repository_path=str(cpp_repo), prompt='fixture'))
            tid = response.json()['task_id']
            async with client.stream('GET', f'/api/v1/tasks/{tid}/events') as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await next_block(lines) == 'retry: 1000\n\n'
                async with asyncio.timeout(2):
                    while not store.waiter_count(tid): await asyncio.sleep(.01)
                release.set()
                async with asyncio.timeout(2):
                    remaining = '\n'.join([line async for line in lines])
                assert 'event: task_completed' not in remaining and 'event: task_failed' not in remaining
            assert store.waiter_count(tid) == 0
            assert (await client.get(f'/api/v1/tasks/{tid}/events')).status_code == 503
            assert (await client.get(f'/api/v1/tasks/{tid}')).status_code == 503


@pytest.mark.asyncio
async def test_live_sse_heartbeat_disconnect_and_reconnect(cpp_repo, rg_exe, git_exe):
    release = Event()
    class DelayedLLM(ScriptedLLM):
        async def chat(self, messages, tools=None):
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await super().chat(messages, tools)
    def factory(trace):
        return TaskService(DelayedLLM(messages()), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    with serving(factory) as (url, app):
        async with httpx.AsyncClient(base_url=url, timeout=15, trust_env=False) as client:
            task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
            seen = []
            async with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await next_block(lines) == "retry: 1000\n\n"
                while True:
                    block = await next_block(lines)
                    if block.startswith(": heartbeat"):
                        if seen:
                            break
                        continue  # Admission may precede Git baseline capture and the first event.
                    seen += frames(block)
            assert seen and seen[-1][1]["data"]["state"] == "analyzing"
            assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["state"] == "analyzing"
            release.set()
            await until(client, task_id, {"waiting_approval"})
            # The previous connection disappearing did not cancel or lose events.
            async with client.stream("GET", f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": seen[-1][0]}) as response:
                lines = response.aiter_lines()
                while True:
                    items = frames(await next_block(lines))
                    seen += items
                    if items and items[-1][1]["event"] == "approval_required":
                        break
            assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["state"] == "waiting_approval"
            assert (await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})).status_code == 202
            async with client.stream("GET", f"/api/v1/tasks/{task_id}/events", headers={"Last-Event-ID": seen[-1][0]}) as response:
                seen += frames((await response.aread()).decode())
            assert seen[-1][1]["event"] == "task_completed"
            assert len({i for i, _ in seen}) == len(seen)
            late = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
            assert late == seen
            # Client dedup convention: key by (task_id, sequence), ignore replayed IDs.
            dedup = dict(seen)
            dedup.update(late)
            assert len(dedup) == len(seen)
            async with asyncio.timeout(3):
                while app.state.task_host.store.waiter_count(task_id):
                    await asyncio.sleep(0.01)
            print("REAL HTTP SSE: heartbeat, disconnect survival, Last-Event-ID replay, late subscription and terminal EOF passed")


@pytest.mark.asyncio
async def test_live_cancel_event_during_analysis(cpp_repo, rg_exe, git_exe):
    class WaitingLLM:
        async def chat(self, messages, tools=None):
            await asyncio.Future()
    def factory(trace):
        return TaskService(WaitingLLM(), rg_executable=rg_exe, git_executable=git_exe, trace=trace)
    with serving(factory) as (url, app):
        async with httpx.AsyncClient(base_url=url, timeout=15, trust_env=False) as client:
            task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(cpp_repo), "prompt": "fix"})).json()["task_id"]
            await until(client, task_id, {"analyzing"})
            async with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as response:
                cancel = await client.post(f"/api/v1/tasks/{task_id}/cancel")
                assert cancel.status_code == 200 and cancel.json()["state"] == "cancelled"
                stream = frames((await response.aread()).decode())
            assert stream[-1][1]["event"] == "task_state_changed"
            assert stream[-1][1]["data"]["state"] == "cancelled"
            assert not any(p["event"] in {"task_completed", "task_failed"} for _, p in stream)
            assert cancel.json()["finished_at"] and app.state.task_host.store.read(task_id, len(stream))[1]


@pytest.mark.asyncio
async def test_real_http_build_repair_event_contract(build_repo, build_config, rg_exe, git_exe):
    secret = "offline-api-secret"
    redactor = Redactor((secret,))
    replies = script(0, initial="int main(){return missing_http_name;}\n// existing user change\n") + [reflection(),
        reply(calls=[write("main.cpp", "int main(){return 0;}\n// existing user change\n")]), reply("repair complete " + secret)]
    def factory(trace):
        return TaskService(ScriptedLLM(replies), rg_executable=rg_exe, git_executable=git_exe,
                           trace=trace, build_config=build_config, redactor=redactor)
    with serving(factory) as (url, app):
        async with httpx.AsyncClient(base_url=url, timeout=90, trust_env=False) as client:
            task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(build_repo), "prompt": "fix"})).json()["task_id"]
            pending = await until(client, task_id, {"waiting_approval"})
            assert pending["build_result"]["selection"]["generator"] == "Ninja"
            await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
            async with client.stream("GET", f"/api/v1/tasks/{task_id}/events") as response:
                text = (await response.aread()).decode()
            result = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            assert result["state"] == "completed", result["error"]
            assert result["repair_round"] == 1 and result["steps"] == 7
            assert result["validation_history"][0]["stages"]["cmake_build"] == "failed"
            assert result["build_result"]["tests"]["passed"] == 1
            assert secret not in text + json.dumps(result)
            stream = frames(text)
            kinds = [p["event"] for _, p in stream]
            assert {"build_started", "build_finished", "test_started", "test_finished", "reflection", "task_completed"} <= set(kinds)
            assert kinds.count("build_started") == kinds.count("build_finished") == 4
            assert kinds.count("test_started") == kinds.count("test_finished") == 1
            builds = [p["data"] for _, p in stream if p["event"] == "build_finished"]
            assert builds[1]["phase"] == "cmake_build" and not builds[1]["success"]
            assert builds[1]["result"]["stages"]["run_tests"] == "not_run"
            assert kinds[-1] == "task_completed" and result["finished_at"]
            print("REAL HTTP BUILD REPAIR " + json.dumps({"task": result, "events": stream}, ensure_ascii=False))


@pytest.mark.asyncio
async def test_real_http_cancel_owned_build_process(build_repo, build_config, rg_exe, git_exe):
    (build_repo / "slow.cmake").write_text('file(WRITE "${CMAKE_CURRENT_LIST_DIR}/build-started.txt" "started")\nexecute_process(COMMAND "${CMAKE_COMMAND}" -E sleep 60)\n')
    with (build_repo / "CMakeLists.txt").open("a") as output:
        output.write('add_custom_command(TARGET counter POST_BUILD COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_SOURCE_DIR}/slow.cmake")\n')
    def factory(trace):
        return TaskService(ScriptedLLM(messages()), rg_executable=rg_exe, git_executable=git_exe,
                           trace=trace, build_config=build_config)
    with serving(factory) as (url, app):
        async with httpx.AsyncClient(base_url=url, timeout=30, trust_env=False) as client:
            task_id = (await client.post("/api/v1/tasks", json={"repository_path": str(build_repo), "prompt": "fix"})).json()["task_id"]
            await until(client, task_id, {"waiting_approval"})
            await client.post(f"/api/v1/tasks/{task_id}/approve", json={"plan_version": 1})
            async with asyncio.timeout(25):
                while not (build_repo / "build-started.txt").exists():
                    await asyncio.sleep(0.02)
            response = await client.post(f"/api/v1/tasks/{task_id}/cancel")
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["state"] == "cancelled" and result["finished_at"] and result["task_diff"]["files"]
            assert result["build_result"]["stages"]["cmake_build"] == "cancelled"
            assert result["build_result"]["stages"]["run_tests"] == "not_run"
            assert "owned process tree terminated" in result["build_result"]["commands"][-1]["stderr"]
            stream = frames((await client.get(f"/api/v1/tasks/{task_id}/events")).text)
            assert stream[-1][1]["data"]["state"] == "cancelled"
            finished = [p for _, p in stream if p["event"] == "build_finished"]
            assert len(finished) == 2 and not finished[-1]["data"]["success"]
            print("REAL HTTP CANCEL: owned build process tree terminated, terminal cancellation and diff retained")
