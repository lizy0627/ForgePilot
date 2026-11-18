"""Bounded live checks of configured MCP, RAG, summary, and Mission functions."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_functional_smoke import (ROOT, CallLimit, CountedProvider, save, ModelGateway,
    ModelConfiguration, OpenAICompatibleClient, SqliteEventStore, TaskService, ChatService,
    ForgePilotError, create_app)
from forgepilot.conversations import ReadOnlyRegistry
from forgepilot.mcp_manager import MCPManager
from forgepilot.mcp_service import MCPService
from forgepilot.rag import (RagConfig, RagManager, RagFileStore, RagIndexCoordinator,
    RagRuntimeSettings, OpenAICompatibleEmbeddingProvider, RetrievalQuery)
from forgepilot.repository import Repository
from forgepilot.server import parser as server_parser


class AdvancedProvider(CountedProvider):
    def __init__(self,profile,key,limit):
        self.profile,self.limit=profile,limit
        self.client=OpenAICompatibleClient(profile.base_url,key,profile.model,
            timeout=min(profile.timeout_seconds,60),
            temperature=profile.temperature,stream_include_usage=True)


class CountedEmbedding(OpenAICompatibleEmbeddingProvider):
    def __init__(self,*args,output,**kwargs):
        super().__init__(*args,**kwargs)
        self.output,self.calls=output,[]

    async def _send_request(self,payload,expected):
        if len(self.calls)>=2:
            raise ForgePilotError("live_embedding_limit","Two real Embedding request limit reached")
        row={"number":len(self.calls)+1,"model":self.model,"expected_vectors":expected,"status":"started"}
        self.calls.append(row);save(self.output/"embedding-calls.json",self.calls)
        started=monotonic()
        try:
            result=await super()._send_request(payload,expected)
            row.update(status="passed",dimensions=len(result[0]) if result else 0)
            return result
        except Exception as exc:
            row.update(status="failed",error_code=exc.code if isinstance(exc,ForgePilotError) else type(exc).__name__)
            raise
        finally:
            row["duration_ms"]=int((monotonic()-started)*1000)
            save(self.output/"embedding-calls.json",self.calls)


def clone_mcp_rows(source,store):
    """Copy encrypted MCP settings only, never the user's conversations."""
    db=sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)
    try:
        columns=[r[1] for r in db.execute("PRAGMA table_info(mcp_servers)")]
        rows=db.execute("SELECT * FROM mcp_servers WHERE enabled=1 AND transport='streamable_http'").fetchall()
    finally:db.close()
    if not columns:return 0
    with store._transaction():
        for row in rows:
            values=dict(zip(columns,row))
            policy=json.loads(values["policy_json"])
            choices=[n for n in policy["selected_tools"] if n in {"tavily_search","tavily-search"}]
            if not choices:continue
            policy["selected_tools"]=choices[:1]
            values["policy_json"]=json.dumps(policy)
            store._db.execute("INSERT INTO mcp_servers ("+",".join(columns)+") VALUES ("+",".join("?" for _ in columns)+")",
                              [values[c] for c in columns])
    return len(rows)


async def services(args,configuration,limit,output,settings):
    gateway=ModelGateway(configuration,provider_factory=lambda p,k:AdvancedProvider(p,k,limit))
    store=SqliteEventStore(str(output/"test.sqlite3"),redactor=gateway.redactor)
    clone_mcp_rows(Path(settings.database),store)
    fixture=output/"rag-fixture";fixture.mkdir()
    (fixture/"price.cpp").write_text(
        "// Calculate the total after applying a percentage discount.\n"
        "double discounted_total(double subtotal, double discount_percent) {\n"
        "    return subtotal * (1.0 - discount_percent / 100.0);\n}\n",encoding="utf-8")
    for command in (["git","init","-q"],["git","add","."],
        ["git","-c","user.name=ForgePilot Smoke","-c","user.email=smoke@localhost","commit","-qm","RAG fixture"]):
        subprocess.run(command,cwd=fixture,capture_output=True,check=True)
    rag_config=RagConfig(enabled=True,include=("*.cpp",),embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url)
    key=os.environ.get("FORGEPILOT_EMBEDDING_API_KEY") or os.environ["FORGEPILOT_API_KEY"]
    embedding=CountedEmbedding(settings.embedding_base_url,key,settings.embedding_model,timeout=45,output=output)
    rag=RagManager(RagRuntimeSettings(config=rag_config,embedding_api_key=key),RagFileStore(store),
        embedding_provider=embedding,vector_path=output/"qdrant",redactor=gateway.redactor)
    async def open_repository(path):
        return await Repository.open(path,rg_executable=shutil.which("rg"),git_executable=shutil.which("git"))
    coordinator=RagIndexCoordinator(rag,open_repository)
    async def close():
        await rag.aclose();await gateway.aclose()
    app=create_app(lambda trace:TaskService(gateway.for_route("task"),rg_executable=shutil.which("rg"),
        git_executable=shutil.which("git"),trace=trace),store=store,
        chat_factory=lambda:ChatService(gateway.for_route("chat")),
        summary_factory=lambda:ChatService(gateway.for_route("summary")),
        rag_coordinator=coordinator,model_client_closer=close)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error",access_log=False,log_config=None))
    serving=asyncio.create_task(server.serve())
    results={"checks":{},"synthetic_history_pairs":18,"real_embedding_limit":2,"model_request_limit":limit.maximum}
    def record(name,passed,**details):
        results["checks"][name]={"status":"passed" if passed else "failed",**details}
        save(output/"results.json",results);print(name,results["checks"][name]["status"],flush=True)
    async def get(client,path,**kwargs):
        r=await client.get(path,**kwargs);r.raise_for_status();return r.json()
    async def post(client,path,body):
        r=await client.post(path,json=body);r.raise_for_status();return r.json()
    try:
        while not server.started:
            if serving.done():await serving
            await asyncio.sleep(.1)
        host=app.state.task_host
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",timeout=100) as client:
            async def mcp_search():
                snapshots=MCPService(store).snapshots("chat")
                manager=MCPManager(snapshots,redactor=gateway.redactor)
                registry=ReadOnlyRegistry(mcp_allowlist=manager.selected,redactor=manager.redactor)
                manager.register(registry)
                try:
                    if not manager.selected:raise ForgePilotError("mcp_not_configured","No enabled external search tool")
                    name=next(iter(manager.selected))
                    result=await manager.execute(registry,name,json.dumps({"query":"site:cmake.org/cmake/help/latest ctest output-junit","max_results":1}),"live-external-search")
                    save(output/"mcp-search.json",{"success":result.success,"error_code":result.error,
                        "source_count":len(result.mcp_sources),"sources":result.mcp_sources,
                        "output_chars":len(result.output),"actual_tool_calls":manager.calls})
                    record("external_mcp_search",result.success and bool(result.mcp_sources),
                        source_count=len(result.mcp_sources),error_code=result.error)
                finally:await manager.close()
            try:await host._submit(mcp_search())
            except Exception as exc:record("external_mcp_search",False,error_code=exc.code if isinstance(exc,ForgePilotError) else type(exc).__name__)
            try:
                await post(client,"/api/v1/rag/index",{"repository_path":str(fixture)})
                deadline=monotonic()+90
                while monotonic()<deadline:
                    status=await get(client,"/api/v1/rag/status",params={"repository_path":str(fixture)})
                    if not status["running"]:break
                    await asyncio.sleep(.2)
                save(output/"rag-index.json",status)
                record("real_embedding_index",status["status"]=="ready" and status["chunks"]>0,index_status=status)
                async def query():
                    repository=await open_repository(str(fixture))
                    service=rag.for_repository(repository).retrieval
                    chunks=await service.retrieve("怎样按折扣百分比计算总价？")
                    trace=service.last_trace.model_dump()
                    save(output/"rag-retrieval.json",{"results":[{"path":c.path,"line":c.line,"end_line":c.end_line} for c in chunks],"trace":trace})
                    record("real_vector_retrieval",any(c.path=="price.cpp" for c in chunks),source_status=trace.get("sources"))
                if status["status"]=="ready":await host._submit(query())
                r=await client.delete("/api/v1/rag/index",params={"repository_path":str(fixture)})
                r.raise_for_status();record("rag_clear_preserves_source",(fixture/"price.cpp").is_file())
            except Exception as exc:record("real_embedding_rag",False,error_code=exc.code if isinstance(exc,ForgePilotError) else type(exc).__name__)
            conversation=await post(client,"/api/v1/conversations",{"title":"自动摘要功能实测"})
            cid=conversation["conversation_id"]
            for index in range(18):
                rid=str(uuid4())
                store.begin_conversation_request(cid,rid,"chat",f"合成历史第{index+1}轮：项目代号蓝塔2026，目标是保留注释并修复程序退出码。","")
                store.finish_conversation_request(rid,"completed",f"合成历史确认第{index+1}轮：代码修改须经人工审批，公开测试保持。")
            rid=str(uuid4())
            await post(client,f"/api/v1/conversations/{cid}/requests",{"request_id":rid,"mode":"chat","tool_mode":"fast","prompt":"用一句简短中文确认你理解上面的项目目标。","use_rag":False})
            deadline=monotonic()+90
            while monotonic()<deadline:
                request=await get(client,f"/api/v1/conversations/{cid}/requests/{rid}")
                if request["status"]!="running":break
                await asyncio.sleep(.2)
            record("summary_foreground_answer",request["status"]=="completed" and request["error"] is None)
            while monotonic()<deadline:
                summary=await get(client,f"/api/v1/conversations/{cid}/context")
                if summary["summary"] is not None:break
                await asyncio.sleep(.25)
            save(output/"automatic-summary.json",summary)
            record("automatic_background_summary",summary["summary"] is not None and
                "蓝塔" in summary["summary"]["content"],summary_revision=summary["revision"])
    except Exception as exc:
        results["run_error"]=exc.code if isinstance(exc,ForgePilotError) else type(exc).__name__
    finally:
        server.should_exit=True;await serving
        results["paid_model_requests_sent"]=len(limit.calls)
        results["real_embedding_requests_sent"]=len(embedding.calls)
        results["accounting"]=[asdict(r) for r in gateway.accounting.snapshot()]
        save(output/"results.json",results);store.close()
    return results


async def main(args):
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    launcher=json.loads(args.launcher_config.read_text(encoding="utf-8-sig"))
    settings=server_parser().parse_args(launcher["server_args"])
    configuration=ModelConfiguration.load(settings.model_config)
    if any("daybreak" in p.model.casefold() for p in configuration.profiles):raise SystemExit("Daybreak is not permitted")
    limit=CallLimit(output,args.max_calls)
    if args.phase=="services":
        results=await services(args,configuration,limit,output,settings)
    else:
        gateway=ModelGateway(configuration,provider_factory=lambda p,k:AdvancedProvider(p,k,limit))
        if args.phase=="mission-resume":
            from live_mission_resume import run_resume
            results=await run_resume(gateway,output,args.original)
        else:
            from live_mission_functional import run_mission
            results=await run_mission(gateway,output)
    return 1 if results.get("run_error") or any(x.get("status")=="failed" for x in results.get("checks",{}).values()) else 0


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--launcher-config",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--phase",choices=("services","mission","mission-resume"),required=True)
    p.add_argument("--original",type=Path)
    p.add_argument("--max-calls",type=int,choices=range(1,13),default=2)
    raise SystemExit(asyncio.run(main(p.parse_args())))
