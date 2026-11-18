"""Small, explicitly authorized live-model functional check using disposable data.

Credentials stay in the process environment. No user database/config is changed.
An exact generated plan requires a separate human approval file before execution.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from threading import Lock
from time import monotonic
from uuid import uuid4

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from forgepilot.api import create_app
from forgepilot.conversations import ChatService
from forgepilot.errors import ForgePilotError
from forgepilot.llm import OpenAICompatibleClient
from forgepilot.models.gateway import ModelGateway
from forgepilot.models.profiles import ModelConfiguration
from forgepilot.presets import BuildConfig
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_service import TaskService


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class CallLimit:
    def __init__(self, output, maximum):
        self.output, self.maximum = output, maximum
        self.calls, self.failed = [], False
        self.lock = Lock()

    def begin(self, profile, stream):
        with self.lock:
            if self.failed or len(self.calls) >= self.maximum:
                raise ForgePilotError("live_check_limit", "Live functional request limit reached; no request sent")
            row = {"number": len(self.calls) + 1, "model": profile.model,
                   "stream": stream, "started_utc": datetime.now(timezone.utc).isoformat(),
                   "status": "started"}
            self.calls.append(row)
            save(self.output / "model-calls.json", self.calls)
            return row

    def finish(self, row, started, error=None):
        with self.lock:
            row.update(status="failed" if error else "completed", duration_ms=int((monotonic()-started)*1000))
            if error:
                row["error_code"] = error.code if isinstance(error, ForgePilotError) else type(error).__name__
                self.failed = True  # Gateway retry must not send another paid request.
            save(self.output / "model-calls.json", self.calls)


class CountedProvider:
    supports_stream_diagnostics = True

    def __init__(self, profile, key, limit):
        self.profile, self.limit = profile, limit
        self.client = OpenAICompatibleClient(profile.base_url, key, profile.model,
            timeout=min(profile.timeout_seconds, 45),
            temperature=profile.temperature, stream_include_usage=True)

    async def chat(self, messages, tools=None):
        row, started = self.limit.begin(self.profile, False), monotonic()
        error = None
        try:
            return await self.client.chat(messages, tools=tools)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.limit.finish(row, started, error)

    async def stream_chat(self, messages, tools=None, **kwargs):
        row, started = self.limit.begin(self.profile, True), monotonic()
        error = None
        try:
            async for event in self.client.stream_chat(messages, tools=tools, **kwargs):
                yield event
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.limit.finish(row, started, error)

    async def aclose(self):
        await self.client.aclose()


async def main(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = ModelConfiguration.load(args.model_config)
    if any("daybreak" in p.model.casefold() for p in config.profiles):
        raise SystemExit("Daybreak is not permitted for this check")
    limit = CallLimit(output, args.max_calls)
    gateway = ModelGateway(config, provider_factory=lambda p, k: CountedProvider(p, k, limit))
    fixture = output / "fixture"
    fixture.mkdir()
    source = "int main() { return 1; }\n// preserve this user comment\n"
    (fixture / "main.cpp").write_text(source, encoding="utf-8")
    (fixture / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.21)\nproject(LiveFunctionCheck LANGUAGES CXX)\n"
        "enable_testing()\nadd_executable(function_check main.cpp)\n"
        "add_test(NAME returns_success COMMAND function_check)\n", encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", "."],
                    ["git", "-c", "user.name=ForgePilot Smoke", "-c", "user.email=smoke@localhost",
                     "commit", "-qm", "Disposable live check baseline"]):
        subprocess.run(command, cwd=fixture, capture_output=True, check=True)
    store = SqliteEventStore(str(output / "test-history.sqlite3"), redactor=gateway.redactor)
    build = BuildConfig(args.cmake, args.ctest, generator="Visual Studio 17 2022",
                        architecture="x64", configuration="Release")
    services = []
    def factory(trace):
        service = TaskService(gateway.for_route("task"), rg_executable=shutil.which("rg"),
            git_executable=shutil.which("git"), trace=trace, redactor=gateway.redactor, build_config=build)
        services.append(service)
        return service
    app = create_app(factory, store=store,
        chat_factory=lambda: ChatService(gateway.for_route("chat"), redactor=gateway.redactor),
        summary_factory=lambda: ChatService(gateway.for_route("summary"), redactor=gateway.redactor),
        mcp_disabled=True, model_client_closer=gateway.aclose)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="error", access_log=False, log_config=None))
    server_task = asyncio.create_task(server.serve())
    results = {"mode": "live_functional_smoke", "max_paid_requests": args.max_calls,
               "fixture": str(fixture), "user_database_changed": False, "checks": {}}
    def record(name, passed, **details):
        results["checks"][name] = {"status": "passed" if passed else "failed", **details}
        save(output / "results.json", results)
        print(name, results["checks"][name]["status"], flush=True)
    async def get(client, path):
        response = await client.get(path)
        response.raise_for_status()
        return response.json()
    async def post(client, path, body):
        response = await client.post(path, json=body)
        response.raise_for_status()
        return response.json()
    async def wait_state(client, path, field, active, seconds=180):
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            data = await get(client, path)
            if data[field] not in active:
                return data
            await asyncio.sleep(.15)
        raise TimeoutError("Functional check did not reach a review or terminal state")
    async def chat(client, cid, prompt, tool_mode):
        rid = str(uuid4())
        await post(client, f"/api/v1/conversations/{cid}/requests", {
            "request_id": rid, "mode": "chat", "tool_mode": tool_mode, "prompt": prompt, "use_rag": False})
        state = await wait_state(client, f"/api/v1/conversations/{cid}/requests/{rid}",
                                 "status", {"running"})
        detail = await get(client, f"/api/v1/conversations/{cid}")
        answer = next((m["content"] for m in reversed(detail["messages"])
                       if m["request_id"] == rid and m["role"] == "assistant"), "")
        events = await client.get(f"/api/v1/conversations/{cid}/requests/{rid}/events")
        (output / f"chat-{rid}-events.txt").write_text(events.text, encoding="utf-8")
        save(output / f"chat-{rid}.json", {"state": state, "answer": answer})
        return state, answer, events.text
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("Test server stopped before startup")
            await asyncio.sleep(.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=190) as client:
            schema = await get(client, "/openapi.json")
            record("backend_http", "/api/v1/conversations" in schema["paths"])
            task = await post(client, "/api/v1/tasks", {"repository_path": str(fixture),
                "prompt": "这是临时功能测试仓库。请先只读检查 main.cpp。它本应正常退出但返回1。"
                    "只计划将 main.cpp 的 return 1 改为 return 0，保留原有注释，不改其他文件或测试。"
                    "分析尽量简短，随后生成方案等待人工批准。批准后的实现只改这一处并简短结束，由系统构建和CTest。",
                "use_rag": False})
            tid = task["task_id"]
            plan = await wait_state(client, f"/api/v1/tasks/{tid}", "state", {"created", "analyzing", "planning"})
            save(output / "task-plan.json", plan)
            record("task_analysis_plan", plan["state"] == "waiting_approval", state=plan["state"], error=plan.get("error"))
            if plan["state"] == "waiting_approval":
                print("PLAN_READY", output / "task-plan.json", flush=True)
            memory = await post(client, "/api/v1/memories", {"content": "功能测试的首选颜色是青色。", "scope": "global"})
            conversation = await post(client, "/api/v1/conversations", {"title": "真实模型功能测试", "repository_path": str(fixture)})
            cid = conversation["conversation_id"]
            state, answer, events = await chat(client, cid,
                "记住本对话测试编号314159。请使用项目只读工具读取main.cpp，告诉我当前返回值并引用文件行号；中文简短回答。", "project")
            record("project_chat_tools_and_stream", state["status"] == "completed" and state["error"] is None and
                   "main.cpp" in answer and "1" in answer and "event: tool_activity" in events,
                   state=state["status"], error=state.get("error"))
            state, answer, events = await chat(client, cid,
                "刚才的测试编号是什么？我在手动长期记忆中保存的首选颜色是什么？只回答编号与颜色。", "fast")
            record("multi_turn_and_saved_memory", state["status"] == "completed" and
                   "314159" in answer and "青" in answer, state=state["status"], error=state.get("error"))
            saved = await get(client, f"/api/v1/conversations/{cid}")
            record("conversation_history", len(saved["messages"]) >= 4)
            response = await client.patch(f"/api/v1/conversations/{cid}", json={"title": "已验证的临时对话"})
            record("conversation_rename", response.status_code == 200 and response.json()["title"] == "已验证的临时对话")
            response = await client.patch(f"/api/v1/memories/{memory['memory_id']}", json={
                "content": "功能测试的首选颜色是蓝色。", "scope": "global", "revision": memory["revision"]})
            record("memory_edit", response.status_code == 200 and "蓝" in response.json().get("content", ""))
            response = await client.delete(f"/api/v1/memories/{memory['memory_id']}")
            record("memory_delete", response.status_code == 200)
            if plan["state"] == "waiting_approval":
                deadline = monotonic() + 600
                while not (output / "human-approval.json").is_file() and monotonic() < deadline:
                    await asyncio.sleep(.5)
                approval = json.loads((output / "human-approval.json").read_text()) if (output / "human-approval.json").is_file() else {}
                if approval.get("approved") is True and approval.get("task_id") == tid and approval.get("plan_version") == plan["plan_version"]:
                    approved = await post(client, f"/api/v1/tasks/{tid}/approve", {"plan_version": plan["plan_version"]})
                    final = await wait_state(client, f"/api/v1/tasks/{tid}", "state",
                        {"modifying", "building", "testing", "reflecting"}, seconds=300)
                    save(output / "task-final.json", final)
                    record("approved_modification_build_ctest", final["state"] == "completed" and
                        "return 0" in (fixture / "main.cpp").read_text(), state=final["state"],
                        outcome=final.get("outcome"), error=final.get("error"))
                else:
                    await post(client, f"/api/v1/tasks/{tid}/cancel", {})
                    results["checks"]["approved_modification_build_ctest"] = {"status": "not_verified", "reason": "human_approval_not_received"}
            response = await client.delete(f"/api/v1/conversations/{cid}")
            record("conversation_delete", response.status_code == 200)
    except BaseException as exc:
        results["run_error"] = gateway.redactor.text(str(exc)) if isinstance(exc, ForgePilotError) else type(exc).__name__
    finally:
        results["paid_requests_sent"] = len(limit.calls)
        results["accounting"] = [asdict(item) for item in gateway.accounting.snapshot()]
        results["fixture_main_sha256"] = hashlib.sha256((fixture / "main.cpp").read_bytes()).hexdigest()
        save(output / "results.json", results)
        server.should_exit = True
        await server_task
        store.close()
    print("DONE", len(limit.calls), "paid requests", flush=True)
    return 1 if results.get("run_error") or any(c["status"] == "failed" for c in results["checks"].values()) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--cmake", default="D:/CMake/bin/cmake.exe")
    parser.add_argument("--ctest", default="D:/CMake/bin/ctest.exe")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
