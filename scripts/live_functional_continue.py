"""Continue an explicitly human-approved live check with four further requests.

Replay the previously paid analysis and exact plan rather than paying twice.
Implementation and conversation answers still come from the real configured model.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
from time import monotonic
from uuid import uuid4

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_functional_smoke import (CallLimit, CountedProvider, ModelGateway,
    ModelConfiguration, SqliteEventStore, TaskService, ChatService, BuildConfig, create_app, save,
    ForgePilotError)
from forgepilot.llm import ChatReply, AssistantMessage


class ApprovedPreparation:
    def __init__(self, client, previous):
        self.client, self.previous, self.count = client, previous, 0

    async def chat(self, messages, tools=None):
        self.count += 1
        if self.count <= 2:
            content = (self.previous["analysis_result"] if self.count == 1 else
                       json.dumps(self.previous["plan"], ensure_ascii=False))
            return ChatReply(message=AssistantMessage(role="assistant", content=content), usage=None)
        return await self.client.chat([*messages, {"role": "system", "content":
            "当前为获人工批准的临时功能补测，实现阶段最多3次真实请求。"
            "文件路径已经确定为main.cpp；不要额外列目录或读构建文件。"
            "第一次直接读取main.cpp取得当前hash，第二次对return 1做精确patch，第三次简短总结结束。"
            "不要修改其他文件、构建或测试，验证由runtime负责。"}], tools=tools)

    async def aclose(self):
        return None


async def main(args):
    original, output = args.original.resolve(), args.output.resolve()
    previous = json.loads((original / "task-plan.json").read_text(encoding="utf-8"))
    plan_hash = hashlib.sha256(json.dumps(previous["plan"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if plan_hash != args.approved_plan_sha256:
        raise SystemExit("Approval must match the exact original plan")
    if [item["path"] for item in previous["plan"]["files_to_modify"]] != ["main.cpp"]:
        raise SystemExit("Only the human-approved fixture/main.cpp is permitted")
    fixture = original / "fixture"
    if (fixture / "main.cpp").read_text(encoding="utf-8") != "int main() { return 1; }\n// preserve this user comment\n":
        raise SystemExit("Approved source changed since plan review")
    output.mkdir(parents=True, exist_ok=False)
    save(output / "human-approval.json", {"approved": True, "source": "explicit user reply in current chat",
        "original_task_id": previous["task_id"], "plan_sha256": plan_hash, "extra_paid_request_limit": 4})
    src = sqlite3.connect(original / "phase2-history.sqlite3")
    dst = sqlite3.connect(output / "test-history.sqlite3")
    src.backup(dst); dst.close(); src.close()
    configuration = ModelConfiguration.load(args.model_config)
    if any("daybreak" in p.model.casefold() for p in configuration.profiles):
        raise SystemExit("Daybreak is not permitted")
    limit = CallLimit(output, 4)
    gateway = ModelGateway(configuration, provider_factory=lambda p,k: CountedProvider(p,k,limit))
    store = SqliteEventStore(str(output / "test-history.sqlite3"), redactor=gateway.redactor)
    build = BuildConfig("D:/CMake/bin/cmake.exe", "D:/CMake/bin/ctest.exe",
        generator="Visual Studio 17 2022", architecture="x64", configuration="Release")
    def factory(trace):
        return TaskService(ApprovedPreparation(gateway.for_route("task"), previous),
            rg_executable=shutil.which("rg"), git_executable=shutil.which("git"),
            trace=trace, redactor=gateway.redactor, build_config=build)
    app = create_app(factory, store=store,
        chat_factory=lambda: ChatService(gateway.for_route("chat"), redactor=gateway.redactor),
        mcp_disabled=True, model_client_closer=gateway.aclose)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error",access_log=False,log_config=None))
    serving=asyncio.create_task(server.serve())
    results={"phase":2,"max_extra_paid_requests":4,"replayed_preparation_messages":2,"checks":{}}
    def record(name,passed,**details):
        results["checks"][name]={"status":"passed" if passed else "failed",**details}
        save(output/"results.json",results)
        print(name,results["checks"][name]["status"],flush=True)
    async def get(c,p):
        r=await c.get(p);r.raise_for_status();return r.json()
    async def post(c,p,b):
        r=await c.post(p,json=b);r.raise_for_status();return r.json()
    async def until(c,p,field,active,seconds=180):
        end=monotonic()+seconds
        while monotonic()<end:
            r=await get(c,p)
            if r[field] not in active:return r
            await asyncio.sleep(.15)
        raise TimeoutError("Functional operation timed out")
    try:
        while not server.started:
            if serving.done():await serving
            await asyncio.sleep(.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",timeout=190) as c:
            conversations=await get(c,"/api/v1/conversations")
            cid=conversations["items"][0]["conversation_id"]
            detail=await get(c,f"/api/v1/conversations/{cid}")
            record("history_after_restart",any("314159" in m["content"] for m in detail["messages"]))
            await post(c,"/api/v1/memories",{"content":"功能测试的首选颜色是青色。","scope":"global"})
            rid=str(uuid4())
            await post(c,f"/api/v1/conversations/{cid}/requests",{"request_id":rid,"mode":"chat","tool_mode":"fast",
                "prompt":"刚才本对话的测试编号是什么？手动长期记忆里的首选颜色是什么？只回答编号与颜色。","use_rag":False})
            state=await until(c,f"/api/v1/conversations/{cid}/requests/{rid}","status",{"running"})
            detail=await get(c,f"/api/v1/conversations/{cid}")
            answer=next((m["content"] for m in reversed(detail["messages"]) if m["request_id"]==rid and m["role"]=="assistant"),"")
            save(output/"chat-memory.json",{"state":state,"answer":answer})
            record("live_followup_saved_memory_and_clean_success",state["status"]=="completed" and state["error"] is None and
                "314159" in answer and "青" in answer,state=state["status"],error=state.get("error"))
            task=await post(c,"/api/v1/tasks",{"repository_path":str(fixture),"prompt":previous["prompt"],"use_rag":False})
            tid=task["task_id"]
            plan=await until(c,f"/api/v1/tasks/{tid}","state",{"created","analyzing","planning"})
            save(output/"task-plan-replayed.json",plan)
            if plan["state"]!="waiting_approval" or plan["plan"]!=previous["plan"]:
                raise RuntimeError("Replayed plan does not match the human-approved plan")
            await post(c,f"/api/v1/tasks/{tid}/approve",{"plan_version":plan["plan_version"]})
            final=await until(c,f"/api/v1/tasks/{tid}","state",{"modifying","building","testing","reflecting"},seconds=300)
            save(output/"task-final.json",final)
            current=(fixture/"main.cpp").read_text(encoding="utf-8")
            record("approved_live_edit_build_ctest",final["state"]=="completed" and "return 0" in current
                and "// preserve this user comment" in current,state=final["state"],outcome=final.get("outcome"),error=final.get("error"))
    except BaseException as exc:
        results["run_error"]=gateway.redactor.text(str(exc)) if isinstance(exc,ForgePilotError) else type(exc).__name__
    finally:
        results["extra_paid_requests_sent"]=len(limit.calls)
        results["accounting"]=[asdict(r) for r in gateway.accounting.snapshot()]
        save(output/"results.json",results)
        server.should_exit=True
        await serving
        store.close()
    print("DONE",len(limit.calls),"extra paid requests",flush=True)
    return 1 if results.get("run_error") or any(x["status"]=="failed" for x in results["checks"].values()) else 0


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--model-config",type=Path,required=True)
    p.add_argument("--approved-plan-sha256",required=True)
    raise SystemExit(asyncio.run(main(p.parse_args())))
