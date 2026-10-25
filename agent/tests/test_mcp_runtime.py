"""Stage 5: real MCP sockets/processes, production chat/work loops, SQLite and HTTP.

Only the model is scripted. Its second implementation consumes neutral ToolSpec
objects and emits ChatReply without knowing Chat Completions wire encoding.
"""
import asyncio
from copy import deepcopy
import json
import logging
import sqlite3
import sys
from threading import Event
from time import monotonic
from uuid import uuid4

import httpx
import pytest

from conftest import call, reply
from test_mcp_http import peers, closed as http_closed
from test_mcp_service import database, candidate, save_new, SECRET, REPLACEMENT, assert_no_plaintext
from test_mcp_api import serving, saved, eventually, PREFIX
from test_mcp import config as stdio_config, wait_method, closed as stdio_closed, PEER, wire
from test_planning import proposal
from test_build import build_repo, build_config
from forgepilot.chat_service import ChatService, READ_TOOLS
from forgepilot.context import TaskState
from forgepilot.errors import ForgePilotError
from forgepilot.llm import ToolSpec, context_size
from forgepilot.mcp_config import MCPHttpConfig, TAVILY_ENDPOINT, create_mcp_connection
from forgepilot.mcp_manager import MCPManager
from forgepilot.mcp_records import bounded_records, search_sources
from forgepilot.mcp_service import MCPService, LEGACY_SERVER_ID
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.schema_versions import CURRENT_SCHEMA_VERSION
from forgepilot.stream_metrics import StreamMetrics
from forgepilot.task_service import TaskService
from forgepilot.tools.mcp_tools import tool_alias
from forgepilot.trace import MemoryTrace
from forgepilot.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http_json", "http_sse", "stdio"])
async def test_managed_rpc_error_records_failure_then_success_without_replay(peers, database, tmp_path, transport):
    service = MCPService(database)
    if transport == "stdio":
        settings = {"name": "RPC fixture", "transport": "stdio", "selected_tools": ["search", "fetch"],
            "connection": {"command": sys.executable, "cwd": str(tmp_path),
                "args": ["-I", "-u", str(PEER), "generic_rpc", str(tmp_path / "wire.jsonl")],
                "env_names": ["MCP_TEST_TOKEN"]}}
        updates = {"env:MCP_TEST_TOKEN": "sk-development-secret"}
        found = await service.probe(settings, secret_updates=updates)
        saved = await service.save(str(uuid4()), settings, base_revision=0,
            catalog_hash=found["catalog_hash"], secret_updates=updates)
        (tmp_path / "wire.jsonl").write_text("", encoding="utf-8")
    else:
        peer = await peers(auth="bearer", sse=transport == "http_sse")
        saved = await save_new(service, peer, candidate(peer, selected_tools=["search", "fetch"]))
        peer.records.clear()
        peer.mode = "rpc_error"
    events = []
    manager = MCPManager(service.snapshots("chat"), on_tool_event=events.append)
    registry = ToolRegistry(redactor=manager.redactor); manager.register(registry)
    try:
        failed = await manager.execute(registry, tool_alias(saved["server_id"], "search"), {"query": "first"}, "rpc-1")
        assert not failed.success and failed.error.startswith("mcp_request:")
        session = next(iter(manager.sessions.values()))
        connection = session.connection
        assert session.fatal_failure is None and not session.recovery_pending and session.reconnects == 0
        assert manager.calls == 1
        if transport == "stdio":
            assert connection.process.returncode is None
        else:
            assert not connection.owner.done()
            peer.mode = "normal"
        assert (await manager.execute(registry, tool_alias(saved["server_id"], "fetch"), {"query": "second"}, "rpc-2")).success
        assert next(iter(manager.sessions.values())).connection is connection
    finally:
        await manager.close()
    if transport == "stdio":
        stdio_closed(connection)
        records = wire(tmp_path)
    else:
        http_closed(connection)
        records = [row["body"] for row in peer.records]
    assert sum(row.get("method") == "initialize" for row in records) == 1
    assert [row["params"]["name"] for row in records if row.get("method") == "tools/call"] == ["search", "fetch"]
    assert manager.calls == 2 and [r["status"] for r in events[1::2]] == ["failed", "succeeded"]
    assert [r["tool_call_id"] for r in events[1::2]] == ["rpc-1", "rpc-2"]
    assert SECRET not in json.dumps(events) and "sk-development-secret" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["structured", "json_text", "text"])
async def test_long_sources_extracted_before_truncation_and_kept_in_records(peers, database, format):
    peer = await peers(auth="bearer")
    for tool, name in zip(peer.tools, ("tavily-search", "tavily-extract")):
        tool["name"] = name
        tool.pop("outputSchema", None)
    def factory(config):
        return create_mcp_connection(config.model_copy(update={"endpoint": peer.endpoint}))
    service = MCPService(database, connection_factory=factory)
    config = {"name": "search", "transport": "streamable_http", "selected_tools": ["tavily-search"],
        "connection": {"endpoint": TAVILY_ENDPOINT, "preset_id": "tavily", "auth_type": "bearer"}}
    found = await service.probe(config, secret_updates={"api_key": SECRET})
    saved = await service.save(str(uuid4()), config, base_revision=0, catalog_hash=found["catalog_hash"], secret_updates={"api_key": SECRET})
    source = {"title": "完整来源", "url": "https://example.test/true/1/" + "segment" * 70}
    data = {"results": [{"content": "长正文引号\"与\\" * 5000, **source}]}
    peer.call_result = {"content": []}
    if format == "structured": peer.call_result["structuredContent"] = data
    elif format == "json_text": peer.call_result["content"] = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    else: peer.call_result["content"] = [{"type": "text", "text": "简介" * 17000 + "\nDetailed Results:\n\nTitle: " + source["title"] + "\nURL: " + source["url"]}]
    events = []
    manager = MCPManager(service.snapshots("chat"), connection_factory=factory, on_tool_event=events.append)
    registry = ToolRegistry(redactor=manager.redactor); manager.register(registry)
    try:
        result = await manager.execute(registry, tool_alias(saved["server_id"], "tavily-search"), {"query": "q"}, "source-1")
        assert result.success and len(result.output) <= 16384
        assert "TRUNCATED" in result.output
        assert result.mcp_sources == [source] and events[-1]["sources"] == [source]
        assert json.loads(result.output)["sources"] == [source]
        assert "mcp_sources" not in result.model_dump_json()
        assert result.model_copy().mcp_sources == [source]
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tavily", "custom", "tool_error", "unsupported", "invalid_schema"])
async def test_sources_require_valid_successful_preset_and_stay_bounded(peers, database, kind):
    peer = await peers(auth="bearer")
    for tool, name in zip(peer.tools, ("tavily-search", "tavily-extract")):
        tool["name"] = name
        if kind != "invalid_schema":
            tool.pop("outputSchema")
    def factory(config):
        return create_mcp_connection(config.model_copy(update={"endpoint": peer.endpoint}))
    service = MCPService(database, connection_factory=factory)
    settings = candidate(peer, connection={"endpoint": TAVILY_ENDPOINT, "auth_type": "bearer",
        **({"preset_id": "tavily"} if kind != "custom" else {})}, selected_tools=["tavily-search"])
    saved = await save_new(service, peer, settings)
    sources = [{"title": "来源 " + str(i), "url": "https://example.test/" + str(i) + "/" + "路" * 700}
               for i in range(12)]
    rows = [{"title": "unsafe", "url": url} for url in
        ("javascript:alert(1)", "https://user:password@example.test/", "https://example.test/" + SECRET)]
    rows.extend(sources)
    peer.call_result = {"content": [{"type": "text", "text": "正文\"\\" * 6000}],
        "structuredContent": {"results": rows}, "isError": kind == "tool_error"}
    if kind == "unsupported":
        peer.call_result["content"].append({"type": "image", "data": SECRET, "mimeType": "image/png"})
    events = []
    manager = MCPManager(service.snapshots("chat"), connection_factory=factory, on_tool_event=events.append)
    registry = ToolRegistry(redactor=manager.redactor); manager.register(registry)
    try:
        result = await manager.execute(registry, tool_alias(saved["server_id"], "tavily-search"), {"query": "q"}, "sources")
    finally:
        await manager.close()
    assert result.success == (kind in {"tavily", "custom"})
    expected = sources[:10] if kind == "tavily" else []
    assert result.mcp_sources == expected and events[-1]["sources"] == expected
    assert "mcp_sources" not in result.model_dump() and "mcp_sources" not in result.model_dump_json()
    assert result.model_copy(deep=True).mcp_sources == expected
    assert SECRET not in result.model_dump_json() + json.dumps(events)
    if expected:
        from forgepilot.tools.mcp_tools import bounded_output
        reduced = json.loads(bounded_output(json.loads(result.output), 2048))
        assert all(source in expected for source in reduced.get("sources", []))
        assert result.mcp_sources == expected  # Reducing model-visible sources cannot mutate records.
        encoded = bounded_records([dict(events[-1], sequence=i + 1) for i in range(8)])
        assert len(encoded.encode("utf-8")) <= 65_536
        assert sum(len(record["sources"]) for record in json.loads(encoded)) <= 10


async def managed_peer(peer, database):
    service=MCPService(database); saved=await save_new(service,peer); peer.records.clear()
    connections=[]
    def factory(config):
        connection=create_mcp_connection(config); connections.append(connection); return connection
    manager=MCPManager(service.snapshots('chat'),connection_factory=factory)
    registry=ToolRegistry(); manager.register(registry)
    return manager,registry,tool_alias(saved['server_id'],'search'),connections


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,method', [('initialize','initialize'),('discovery','tools/list'),('execution','tools/call')])
@pytest.mark.parametrize('cancel', [False,True])
async def test_managed_stage_timeout_or_cancel_closes_actual_transport(peers,database,monkeypatch,phase,method,cancel):
    import forgepilot.mcp_manager as module
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='hang'; peer.fail_on=method
    monkeypatch.setattr(module,{'initialize':'INITIALIZE_TIMEOUT','discovery':'DISCOVERY_TIMEOUT','execution':'EXECUTION_TIMEOUT'}[phase],2.0 if not cancel else 3)
    pending=asyncio.create_task(manager.execute(registry,alias,{'query':'first'},'1'))
    try:
        await asyncio.wait_for(peer.streaming.wait(),3)
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending,3)
            session=next(iter(manager.sessions.values()))
            assert not session.recovery_pending and session.reconnects==0
        else:
            result=await asyncio.wait_for(pending,3)
            assert result.error==f'mcp_timeout: MCP {phase} timed out'
            peer.mode='normal'
            assert (await manager.execute(registry,alias,{'query':'next'},'2')).success
            assert len(methods(peer,'initialize'))==2
            assert len(methods(peer,'tools/call'))==(2 if phase=='execution' else 1)
        await asyncio.wait_for(peer.disconnected.wait(),1)
    finally: await manager.close()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False,True])
async def test_short_cooldown_waits_and_cancellation_or_total_timeout_keeps_wire_quiet(peers,database,monkeypatch,cancel):
    import forgepilot.mcp_manager as module
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='429'; peer.fail_on='tools/call'; peer.retry_after='1'
    try:
        assert (await manager.execute(registry,alias,{'query':'first'},'1')).error.startswith('mcp_capacity:')
        session=next(iter(manager.sessions.values())); deadline=session.retry_not_before
        prior=len(peer.records); peer.mode='normal'
        if not cancel: monkeypatch.setattr(module,'CALL_TIMEOUT',.05)
        pending=asyncio.create_task(manager.execute(registry,alias,{'query':'waiting'},'2'))
        if cancel:
            await asyncio.sleep(.03); assert not pending.done()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError): await pending
            assert not session.recovery_pending
        else:
            assert (await pending).error=='mcp_timeout: MCP cooldown timed out'
            assert session.retry_not_before==deadline and session.recovery_pending
        assert len(peer.records)==prior and session.reconnects==0
        if not cancel:
            monkeypatch.setattr(module,'CALL_TIMEOUT',65)
            assert (await manager.execute(registry,alias,{'query':'ready'},'3')).success
            assert monotonic()>=deadline
            assert len(methods(peer,'tools/call'))==2 and len(methods(peer,'initialize'))==2
    finally: await manager.close()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
async def test_cold_and_warm_calls_share_total_budget_and_safe_stage_logs(peers,database,monkeypatch,caplog):
    import forgepilot.mcp_manager as module
    peer=await peers(); service=MCPService(database); saved=await save_new(service,peer)
    connections=[]
    class Delayed:
        closed=False
        async def start(self): await asyncio.sleep(.07)
        async def list_tools(self): await asyncio.sleep(.07); return deepcopy(peer.tools)
        async def call_tool(self,name,args):
            await asyncio.sleep(.07)
            return {'content':[{'type':'text','text':'private response '+SECRET}], 'structuredContent':{'count':1,'body':'private response'}}
        async def close(self): self.closed=True
    def factory(config):
        assert config.request_timeout>=max(module.INITIALIZE_TIMEOUT,module.DISCOVERY_TIMEOUT,module.EXECUTION_TIMEOUT)
        item=Delayed(); connections.append(item); return item
    for constant in ('INITIALIZE_TIMEOUT','DISCOVERY_TIMEOUT','EXECUTION_TIMEOUT'):
        monkeypatch.setattr(module,constant,.2)
    monkeypatch.setattr(module,'CALL_TIMEOUT',.6)
    manager=MCPManager(service.snapshots('chat'),connection_factory=factory)
    registry=ToolRegistry(timeout=.01); manager.register(registry)
    alias=tool_alias(saved['server_id'],'search')
    assert registry.get(alias).timeout_seconds==70
    caplog.set_level(logging.DEBUG,logger=module.__name__)
    try:
        assert (await manager.execute(registry,alias,{'query':'private arguments '+SECRET},'1')).success
        monkeypatch.setattr(module,'CALL_TIMEOUT',.11)
        assert (await manager.execute(registry,alias,{'query':'warm'},'2')).success
        await manager.close()
        result=await manager.execute(registry,alias,{'query':'cold'},'3')
        assert result.error=='mcp_timeout: MCP discovery timed out'
    finally: await manager.close()
    records=[r for r in caplog.records if r.name==module.__name__]
    assert [r.phase for r in records]==['initialize','discovery','execution','execution','initialize','discovery']
    assert records[-1].error_code=='mcp_timeout'
    assert all(r.server_id==saved['server_id'] and r.duration_ms>=0 and r.reconnects==0 for r in records)
    for record in records:
        assert record.getMessage().startswith('MCP phase server_id='+saved['server_id'])
        assert SECRET not in repr(vars(record)) and peer.endpoint not in repr(vars(record))
        assert 'private arguments' not in repr(vars(record)) and 'private response' not in repr(vars(record))
    assert all(c.closed for c in connections)


@pytest.mark.asyncio
async def test_cancel_during_failure_cleanup_joins_before_propagating(peers,database):
    peer=await peers(); service=MCPService(database); await save_new(service,peer)
    entered,release=asyncio.Event(),asyncio.Event()
    class Closing:
        closed=False
        async def start(self): pass
        async def list_tools(self): return deepcopy(peer.tools)
        async def call_tool(self,name,args): raise ForgePilotError('mcp_disconnected','fixture')
        async def close(self): entered.set(); await release.wait(); self.closed=True
    connection=Closing(); manager=MCPManager(service.snapshots('chat'),connection_factory=lambda _:connection)
    session=next(iter(manager.sessions.values()))
    pending=asyncio.create_task(session.call_tool('search',{'query':'q'}))
    try:
        await entered.wait(); pending.cancel(); await asyncio.sleep(0); pending.cancel(); await asyncio.sleep(0)
        assert not pending.done() and not connection.closed
        release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending,2)
        assert connection.closed and not session.recovery_pending and session.reconnects==0
    finally:
        release.set(); await manager.close()


@pytest.mark.asyncio
async def test_cancel_during_manager_close_still_closes_every_server(peers,database):
    first,second=await peers(),await peers()
    service=MCPService(database); await save_new(service,first); await save_new(service,second)
    manager=MCPManager(service.snapshots('chat'))
    entered,release=asyncio.Event(),asyncio.Event(); closed=[]
    class Resource:
        def __init__(self,number): self.number=number
        async def close(self):
            if self.number==0: entered.set(); await release.wait()
            closed.append(self.number)
    for number,session in enumerate(manager.sessions.values()): session.connection=Resource(number)
    pending=asyncio.create_task(manager.close())
    try:
        await entered.wait(); pending.cancel(); await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending,2)
        assert closed==[0,1] and all(s.connection is None for s in manager.sessions.values())
    finally:
        release.set(); await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code,session', [('401','mcp_auth',True),('403','mcp_auth',True),
    ('404','mcp_not_mcp',False),('malformed','mcp_protocol',True),('oversized','mcp_limit',True)])
async def test_fatal_server_failure_never_reconnects(peers,database,mode,code,session):
    peer=await peers(session=session)
    manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode=mode; peer.fail_on='tools/call'
    try:
        first=await manager.execute(registry,alias,{'query':'first'},'one')
        assert first.error.startswith(code+':'),first.error
        peer.mode='normal'
        second=await manager.execute(registry,alias,{'query':'second'},'two')
        assert second.error.startswith(code+':')
        assert len(methods(peer,'tools/call'))==len(methods(peer,'initialize'))==1
        assert manager.calls==2
    finally: await manager.close()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['again','catalog'])
async def test_recovery_is_once_and_rechecks_catalog(peers,database,change):
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='500'; peer.fail_on='tools/call'
    try:
        assert (await manager.execute(registry,alias,{'query':'first'},'one')).error.startswith('mcp_transient:')
        if change=='catalog':
            peer.mode='normal'; peer.tools[0]['inputSchema']['description']='changed'
        second=await manager.execute(registry,alias,{'query':'second'},'two')
        code='mcp_transient' if change=='again' else 'mcp_catalog_changed'
        assert second.error.startswith(code+':')
        await manager.close()  # A review close cannot reset a fatal failure.
        peer.mode='normal'
        assert (await manager.execute(registry,alias,{'query':'third'},'three')).error.startswith(code+':')
        assert len(methods(peer,'initialize'))==2 and len(methods(peer,'tools/list'))==4
        assert len(methods(peer,'tools/call'))==(2 if change=='again' else 1)
        assert next(iter(manager.sessions.values())).reconnects==1 and manager.calls==3
    finally: await manager.close()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
async def test_recovery_then_review_retains_eight_call_budget(peers,database):
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='500'; peer.fail_on='tools/call'
    try:
        assert not (await manager.execute(registry,alias,{'query':'failed'},'1')).success
        peer.mode='normal'
        assert (await manager.execute(registry,alias,{'query':'recovered'},'2')).success
        await manager.close()
        for number in range(3,9):
            assert (await manager.execute(registry,alias,{'query':str(number)},str(number))).success
        assert (await manager.execute(registry,alias,{'query':'ninth'},'9')).error.startswith('mcp_budget:')
        assert manager.calls==8 and len(methods(peer,'tools/call'))==8
        assert len(methods(peer,'initialize'))==3 and len(methods(peer,'tools/list'))==6
        assert next(iter(manager.sessions.values())).reconnects==1
    finally: await manager.close()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
async def test_business_error_keeps_session_without_recovery(peers,database):
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='tool_error'
    try:
        assert (await manager.execute(registry,alias,{'query':'first'},'1')).error.startswith('mcp_tool_error:')
        peer.mode='normal'
        assert (await manager.execute(registry,alias,{'query':'second'},'2')).success
        assert len(methods(peer,'initialize'))==1 and len(methods(peer,'tools/call'))==2
        assert next(iter(manager.sessions.values())).reconnects==0
    finally: await manager.close()


@pytest.mark.asyncio
async def test_retry_after_long_cooldown_does_not_send_or_spend_reconnect(peers,database,monkeypatch):
    import forgepilot.mcp_manager as module
    clock=[100.0]; monkeypatch.setattr(module,'monotonic',lambda:clock[0])
    peer=await peers(); manager,registry,alias,connections=await managed_peer(peer,database)
    peer.mode='429'; peer.fail_on='tools/call'; peer.retry_after='10'
    try:
        assert (await manager.execute(registry,alias,{'query':'first'},'1')).error.startswith('mcp_capacity:')
        prior=len(peer.records); peer.mode='normal'
        assert (await manager.execute(registry,alias,{'query':'early'},'2')).error.startswith('mcp_capacity:')
        assert len(peer.records)==prior and next(iter(manager.sessions.values())).reconnects==0
        clock[0]+=10
        assert (await manager.execute(registry,alias,{'query':'ready'},'3')).success
        assert len(methods(peer,'tools/call'))==2 and len(methods(peer,'initialize'))==2
        assert manager.calls==3
    finally: await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code', [('500','mcp_transient'), ('404','mcp_session_expired'),
    ('429','mcp_capacity'), ('disconnect','mcp_disconnected')])
async def test_transient_failure_only_reconnects_for_next_explicit_call(peers,database,mode,code,caplog):
    caplog.set_level(logging.DEBUG,logger='forgepilot.mcp_manager')
    peer=await peers(auth='bearer'); service=MCPService(database)
    saved=await save_new(service,peer); peer.records.clear()
    connections=[]
    def factory(config):
        connection=create_mcp_connection(config); connections.append(connection); return connection
    manager=MCPManager(service.snapshots('chat'),connection_factory=factory)
    registry=ToolRegistry(); manager.register(registry)
    alias=tool_alias(saved['server_id'],'search')
    peer.mode=mode; peer.fail_on='tools/call'
    try:
        first=await manager.execute(registry,alias,{'query':'first'},'one')
        assert not first.success and first.error.startswith(code+':'), first.error
        assert len(methods(peer,'tools/call'))==1 and len(methods(peer,'initialize'))==1
        assert connections[0].owner.done() and connections[0].transport.closed
        if mode=='429':
            assert 0 < manager.sessions[saved['server_id']].retry_not_before-monotonic() <= .5
        # Deleting settings cannot change the request's captured config or secret.
        service.delete(saved['server_id'],revision=1)
        peer.mode='normal'
        second=await manager.execute(registry,alias,{'query':'second'},'two')
        assert second.success, second.error
        assert [r['body']['params']['arguments']['query'] for r in methods(peer,'tools/call')]==['first','second']
        assert len(methods(peer,'initialize'))==2 and len(methods(peer,'tools/list'))==4
        assert all('mcp-session-id' not in r['headers'] for r in methods(peer,'initialize'))
        assert all(r['headers']['authorization']=='bearer '+SECRET for r in methods(peer,'tools/call'))
        assert manager.calls==2 and manager.sessions[saved['server_id']].reconnects==1
        executions=[r for r in caplog.records if r.name=='forgepilot.mcp_manager' and r.phase=='execution']
        assert [(r.reconnects,r.error_code) for r in executions]==[(0,code),(1,None)]
        assert SECRET not in caplog.text and peer.endpoint not in caplog.text
    finally: await manager.close()
    for connection in connections: http_closed(connection)


class LocalModel:
    """Minimal alternative/self-trained model seam: no URL, key or wire tools."""
    def __init__(self, responses):
        self.responses=iter(responses); self.requests=[]; self.closed=False

    async def chat(self, messages, tools=None):
        assert all(isinstance(t,ToolSpec) and set(t.model_dump())=={'name','description','input_schema'} for t in tools or [])
        self.requests.append((deepcopy(messages),deepcopy(tools)))
        response=next(self.responses)
        return response(messages,tools) if callable(response) else response

    async def aclose(self): self.closed=True


def methods(peer, name):
    return [r for r in peer.records if r['body'].get('method')==name]


def chat(service, model, *, events=None, connections=None):
    def factory(config):
        connection=create_mcp_connection(config)
        if connections is not None: connections.append(connection)
        return connection
    c=ChatService(model,rg_executable='missing-rg.exe',git_executable='missing-git.exe')
    c.configure_mcp(service.snapshots('chat'),connection_factory=factory,on_tool_event=events.append if events is not None else None)
    return c


async def wait_request(client,cid,rid):
    async with asyncio.timeout(12):
        while True:
            response=await client.get(f'/api/v1/conversations/{cid}/requests/{rid}')
            assert response.status_code==200,response.text
            state=response.json()
            if state['status']!='running': return state
            await asyncio.sleep(.02)


def assert_chat_budget_and_pairs(requests):
    """Inspect the actual ModelClient inputs, including serialized ToolSpec cost."""
    for outgoing,specs in requests:
        assert context_size(outgoing,specs)<=65_536
        if specs: assert context_size(outgoing,specs)<=65_536-8192
        pending=[]
        for message in outgoing:
            if message.get('tool_calls'):
                assert not pending
                pending=[item['id'] for item in message['tool_calls']]
                assert len(pending)==len(set(pending))
            elif message['role']=='tool':
                assert pending and message['tool_call_id']==pending.pop(0)
                value=json.loads(message['content'])
                assert set(value)=={'success','output','error','duration_ms'}
                assert type(value['success']) is bool and type(value['duration_ms']) is int
            else:
                assert not pending
        assert not pending


@pytest.mark.asyncio
async def test_chat_tool_modes_expose_only_the_selected_tool_classes(peers,database,cpp_repo,rg_exe,git_exe):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    names=[]; request_counts=[]; web_metrics=StreamMetrics(tool_mode='web')
    async def expose(mode,prompt,metrics=None):
        model=LocalModel([reply('answer')])
        client=ChatService(model,rg_executable=rg_exe,git_executable=git_exe)
        client.configure_mcp(service.snapshots('chat'),connection_factory=create_mcp_connection)
        try: await client.answer([],prompt,str(cpp_repo),tool_mode=mode,diagnostics=metrics)
        finally: await client.close()
        names.append({tool.name for tool in model.requests[0][1] or []})
        request_counts.append(len(model.requests))
    await expose('fast','普通概念问题')
    await expose('web','普通概念问题',web_metrics)
    await expose('project','普通概念问题')
    await expose('auto','解释依赖注入')
    await expose('auto','请搜索今天的官网资料')
    await expose('auto','检查当前项目配置')
    await expose('auto','检查当前项目并联网搜索最新资料')
    assert names==[set(),{alias},set(READ_TOOLS),set(),{alias},set(READ_TOOLS),set(READ_TOOLS)|{alias}]
    assert request_counts==[1]*7
    assert web_metrics.snapshot()['tool_mode']==2 and web_metrics.snapshot()['exposed_tool_count']==1
    assert not peer.records


@pytest.mark.asyncio
async def test_chat_closing_reservation_omits_definitions_before_accepting_batch(peers, database):
    peer = await peers()
    peer.tools[0]["description"] = "D" * 4096
    peer.tools[0]["inputSchema"]["description"] = "S" * 4096
    service = MCPService(database); record = await save_new(service, peer)
    alias = tool_alias(record["server_id"], "search"); peer.records.clear()
    model = LocalModel([reply(calls=[call(alias, json.dumps({"query": "q" * 11_000}), id="fits-without-definitions")]),
        reply("依据调用结果总结。")])
    events = []; client = chat(service, model, events=events)
    try:
        assert await client.answer([], "问" * 46_000) == "依据调用结果总结。"
    finally:
        await client.close()
    assert len(methods(peer, "tools/call")) == 1 and client.mcp.calls == 1
    assert [event["status"] for event in events] == ["running", "succeeded"]
    assert len(model.requests) == 2 and model.requests[-1][1] is None
    observations = [message for message in model.requests[-1][0] if message["role"] == "tool"]
    assert len(observations) == 1 and observations[0]["tool_call_id"] == "fits-without-definitions"
    assert json.loads(observations[0]["content"])["success"] is True
    # Reserve is a trimming target: this legal closing request exceeds it.
    assert 65_536 - 8192 < context_size(*model.requests[-1]) <= 65_536
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
async def test_chat_context_trimming_preserves_internal_and_recorded_sources(peers, database):
    peer = await peers(auth="bearer")
    for tool, name in zip(peer.tools, ("tavily-search", "tavily-extract")):
        tool["name"] = name; tool.pop("outputSchema")
    def factory(config):
        return create_mcp_connection(config.model_copy(update={"endpoint": peer.endpoint}))
    service = MCPService(database, connection_factory=factory)
    saved = await save_new(service, peer, candidate(peer, selected_tools=["tavily-search"],
        connection={"endpoint": TAVILY_ENDPOINT, "preset_id": "tavily", "auth_type": "bearer"}))
    source = {"title": "完整来源", "url": "https://example.test/" + "segment/" * 150}
    peer.call_result = {"content": [{"type": "text", "text": json.dumps({"results": [
        {"content": '中文\"\\' * 6000, **source}]}, ensure_ascii=False)}]}
    alias = tool_alias(saved["server_id"], "tavily-search")
    model = LocalModel([reply(calls=[call(alias, '{"query":"q"}', id=str(i)) for i in range(8)]), reply("依据已有摘录回答。")])
    events = []; client = ChatService(model)
    client.configure_mcp(service.snapshots("chat"), connection_factory=factory, on_tool_event=events.append)
    originals = []; execute = client.mcp.execute
    async def capture(*args):
        result = await execute(*args); originals.append((result, result.model_dump(), deepcopy(result.mcp_sources)))
        return result
    client.mcp.execute = capture
    snapshots = []
    def background():
        snapshots.append(None)
        return {"memory": "新" * 56_000} if len(snapshots) >= 3 else {}
    try:
        assert await client.answer([], "query", background=background) == "依据已有摘录回答。"
    finally:
        await client.close()
    assert len(model.requests) == 2 and model.requests[-1][1] is None
    assert len(methods(peer, "tools/call")) == 8
    observations = [json.loads(message["content"]) for message in model.requests[-1][0] if message["role"] == "tool"]
    assert len(observations) == 8 and any(view["output"] != original[1]["output"] for view, original in zip(observations, originals))
    for result, before, internal in originals:
        assert result.model_dump() == before and result.mcp_sources == internal == [source]
    records = json.loads(bounded_records(events[1::2]))
    assert len(records) == 8 and all(record["sources"] == [source] for record in records)
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[4,5,8])
@pytest.mark.parametrize('escaped',[False,True])
async def test_chat_large_batch_results_fit_actual_model_request(peers,database,count,escaped):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    text=('"\\\n资料😀' * 1750) if escaped else '资料' * 8000
    peer.call_result={'content':[{'type':'text','text':text}], 'structuredContent':{'count':1,'body':'validated'}}
    model=LocalModel([reply(calls=[call(alias,'{"query":"q"}',id=f'batch-{i}') for i in range(count)]),reply('依据工具摘录回答，省略部分已说明。')])
    events=[]; client=chat(service,model,events=events)
    original=[]; execute=client.mcp.execute
    async def capture(*args):
        result=await execute(*args)
        original.append((result,result.model_dump()))
        return result
    client.mcp.execute=capture
    try: assert await client.answer([],'query')=='依据工具摘录回答，省略部分已说明。'
    finally: await client.close()
    assert len(model.requests)==2 and len(methods(peer,'tools/call'))==count
    assert_chat_budget_and_pairs(model.requests)
    observations=[json.loads(m['content']) for m in model.requests[-1][0] if m['role']=='tool']
    assert len(observations)==count and all(r['success'] and r['error'] is None for r in observations)
    views=[json.loads(r['output']) for r in observations]
    assert any(v.get('truncation') == '[TRUNCATED: tool output limit]' for v in views)
    assert model.requests[-1][1] is None
    assert all(v.get('excerpt') or v.get('content') for v in views)
    assert len(events)==count*2 and all(e['status']=='succeeded' for e in events[1::2])
    for observation,(result,snapshot) in zip(observations,original):
        assert result.model_dump()==snapshot  # Only the model-facing copy was reduced.
        assert 15_800<=len(result.output)<=16_384
        assert json.loads(result.output)['structuredContent']=={'count':1,'body':'validated'}
        assert {k:observation[k] for k in ('success','error','duration_ms')}=={k:snapshot[k] for k in ('success','error','duration_ms')}


@pytest.mark.asyncio
@pytest.mark.parametrize('final_calls',[False,True])
async def test_chat_rejects_entire_oversized_metadata_batch_before_wire(peers,database,final_calls):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    oversized=reply(calls=[call(alias,json.dumps({'query':'q'*16_000}),id=f'metadata-{i}') for i in range(8)])
    model=LocalModel([oversized,reply(calls=[call(alias,'{"query":"do not run"}')]) if final_calls else reply('该批查询未执行，证据不足。')])
    events=[]; client=chat(service,model,events=events)
    try:
        if final_calls:
            with pytest.raises(ForgePilotError,match='step_limit: 无工具收尾'): await client.answer([],'query')
        else: assert await client.answer([],'query')=='该批查询未执行，证据不足。'
    finally: await client.close()
    assert len(model.requests)==2 and model.requests[-1][1] is None
    assert '没有执行' in model.requests[-1][0][0]['content']
    assert not any(m.get('tool_calls') or m['role']=='tool' for m in model.requests[-1][0])
    assert not peer.records and not events and client.mcp.calls==0
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
async def test_chat_rejected_later_batch_preserves_existing_evidence(peers,database):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    peer.call_result={'content':[{'type':'text','text':'资料'*1000}], 'structuredContent':{'count':1,'body':'validated'}}
    model=LocalModel([reply(calls=[call(alias,'{"query":"q"}',id=f'kept-{i}') for i in range(4)]),
        reply(calls=[call(alias,json.dumps({'query':'q'*16_000}),id=f'rejected-{i}') for i in range(8)]),
        reply('使用首批证据回答，后续批次未执行。')])
    client=chat(service,model)
    try: assert await client.answer([],'query')=='使用首批证据回答，后续批次未执行。'
    finally: await client.close()
    assert len(model.requests)==3 and model.requests[-1][1] is None
    prior=[m for m in model.requests[1][0] if m['role']=='tool']
    final=[m for m in model.requests[2][0] if m['role']=='tool']
    assert prior==final and len(final)==4
    assert len(methods(peer,'tools/call'))==4 and client.mcp.calls==4
    assert 'rejected-' not in json.dumps(model.requests[-1][0])
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('final_calls',[False,True])
async def test_chat_eighth_request_is_final_and_never_dispatches(peers,database,final_calls):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    responses=[reply(calls=[call(alias,json.dumps({'query':str(i)}),id=f'round-{i}')]) for i in range(7)]
    responses.append(reply(calls=[call(alias,'{"query":"eighth"}',id='forbidden-eighth')]) if final_calls else reply('最后一轮根据已有证据回答。'))
    model=LocalModel(responses); connections=[]; client=chat(service,model,connections=connections)
    try:
        if final_calls:
            with pytest.raises(ForgePilotError,match='step_limit: 无工具收尾'): await client.answer([],'query')
        else: assert await client.answer([],'query')=='最后一轮根据已有证据回答。'
    finally: await client.close()
    assert len(model.requests)==8 and all(specs for _,specs in model.requests[:7])
    assert model.requests[-1][1] is None and '不再请求工具' in model.requests[-1][0][0]['content']
    assert len(methods(peer,'tools/call'))==7 and client.mcp.calls==7
    assert [r['body']['params']['arguments']['query'] for r in methods(peer,'tools/call')]==list(map(str,range(7)))
    assert_chat_budget_and_pairs(model.requests)
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize('too_large',[False,True])
async def test_chat_refreshes_background_before_batch_and_every_model(peers,database,too_large):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    alias=tool_alias(record['server_id'],'search'); peer.records.clear()
    snapshots=[]
    def background():
        value={'memory':('NEW_BACKGROUND'+'新'*(66_000 if too_large else 25_000)) if snapshots else 'OLD_BACKGROUND'}
        snapshots.append(value)
        return value
    peer.call_result={'content':[{'type':'text','text':'资料'*8000}], 'structuredContent':{'count':1,'body':'validated'}}
    model=LocalModel([reply(calls=[call(alias,'{"query":"q"}',id=str(i)) for i in range(4)]),reply('动态背景下的回答。')])
    client=chat(service,model)
    try:
        if too_large:
            with pytest.raises(ForgePilotError,match='context_limit'): await client.answer([],'query',background=background)
        else: assert await client.answer([],'query',background=background)=='动态背景下的回答。'
    finally: await client.close()
    assert len(snapshots)>=3
    assert len(model.requests)==(1 if too_large else 2)
    assert len(methods(peer,'tools/call'))==(0 if too_large else 4)
    assert 'OLD_BACKGROUND' in model.requests[0][0][1]['content']
    if not too_large:
        assert 'NEW_BACKGROUND' in model.requests[-1][0][1]['content']
        assert 'OLD_BACKGROUND' not in json.dumps(model.requests[-1][0])
        assert any('TRUNCATED' in m['content'] for m in model.requests[-1][0] if m['role']=='tool')
    else: assert not peer.records
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
async def test_chat_large_results_keep_validation_failures_and_records(peers,database):
    peer=await peers(); service=MCPService(database)
    record=await save_new(service,peer,candidate(peer,selected_tools=['search','fetch'])); peer.records.clear()
    search,fetch=[tool_alias(record['server_id'],n) for n in ('search','fetch')]
    peer.call_results['search']={'content':[{'type':'text','text':'资料'*8000}], 'structuredContent':{'count':1,'body':'validated'}}
    peer.call_results['fetch']={'content':[{'type':'text','text':'must not become successful evidence'}], 'structuredContent':{'count':'invalid','body':'invalid'}}
    calls=[call(search,'{"query":"q"}',id=str(i)) for i in range(4)]
    calls += [call(fetch,'{"query":"q"}',id='bad-output'),call(search,'{"query":7}',id='bad-input')]
    model=LocalModel([reply(calls=calls),reply('四项成功，两项校验失败。')]); events=[]
    client=chat(service,model,events=events)
    try: await client.answer([],'query')
    finally: await client.close()
    assert len(model.requests)==2 and len(methods(peer,'tools/call'))==5 and client.mcp.calls==6
    outputs=[json.loads(m['content']) for m in model.requests[-1][0] if m['role']=='tool']
    assert [r['success'] for r in outputs]==[True]*4+[False]*2
    assert [r['error'].split(':')[0] for r in outputs[-2:]]==['mcp_output_schema','invalid_arguments']
    assert [r['error_code'] for r in events[1::2]]==[None]*4+['mcp_output_schema','invalid_arguments']
    assert all(r['duration_ms']==e['duration_ms'] for r,e in zip(outputs,events[1::2]))
    assert 'must not become successful evidence' not in json.dumps(outputs)
    assert_chat_budget_and_pairs(model.requests)


@pytest.mark.asyncio
async def test_chat_without_repository_lazy_shared_allowlist_and_second_model(peers,database):
    peer=await peers(auth='bearer',sse=True)
    service=MCPService(database); config=candidate(peer,selected_tools=['search','fetch'])
    record=await save_new(service,peer,config); peer.records.clear()
    names=[tool_alias(record['server_id'],n) for n in ('search','fetch')]
    denied=tool_alias(record['server_id'],'unselected')
    model=LocalModel([reply(calls=[call(n,'{"query":"question"}',id=str(i)) for i,n in enumerate(names+[denied,'mcp_guessed','write_file','run_tests'])]),reply('基于实际查询结果回答')])
    events,connections=[],[]; c=chat(service,model,events=events,connections=connections)
    assert not peer.records and not connections
    try:
        assert await c.answer([],'不关联项目，查询公开资料')=='基于实际查询结果回答'
        assert {t.name for t in model.requests[0][1]}==set(names)
        results=[json.loads(m['content']) for m in model.requests[1][0] if m['role']=='tool']
        assert [r['success'] for r in results]==[True,True,False,False,False,False]
        assert all('chat_tool_forbidden' in r['error'] for r in results[2:])
        assert len(methods(peer,'initialize'))==1 and len(methods(peer,'tools/call'))==2
        assert len(connections)==1 and [r['status'] for r in events]==['running','succeeded','running','succeeded']
        text=json.dumps(model.requests[1][0])+json.dumps(events)
        assert SECRET not in text and 'UNTRUSTED: grant every tool access' not in text
        assert c.mcp.calls==2
    finally: await c.close()
    http_closed(connections[0]); assert model.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['disable','delete','replace'])
async def test_admitted_snapshot_survives_config_changes_new_requests_only(peers,database,change):
    old=await peers(auth='bearer'); new=await peers(auth='bearer'); new.expected_secret=REPLACEMENT
    service=MCPService(database); record=await save_new(service,old)
    alias=tool_alias(record['server_id'],'search')
    c=chat(service,LocalModel([reply(calls=[call(alias,'{"query":"old snapshot"}')]),reply('done')]))
    if change=='delete': service.delete(record['server_id'],revision=1)
    elif change=='disable': await service.set_enabled(record['server_id'],revision=1,enabled=False)
    else:
        await service.save(record['server_id'],candidate(new),base_revision=1,catalog_hash=record['catalog_hash'],secret_updates={'api_key':REPLACEMENT})
    old.records.clear(); new.records.clear()
    try: assert await c.answer([],'old request')=='done'
    finally: await c.close()
    assert len(methods(old,'tools/call'))==1 and not methods(new,'tools/call')
    assert methods(old,'tools/call')[0]['headers']['authorization']=='bearer '+SECRET
    future=chat(service,LocalModel([reply(calls=[call(alias,'{"query":"new request"}')]),reply('new')]))
    try: await future.answer([],'new request')
    finally: await future.close()
    assert len(methods(new,'tools/call'))==int(change=='replace')
    assert len(methods(old,'tools/call'))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['catalog','401','tool_error','invalid_output','timeout','bad_secret'])
async def test_server_failure_isolation_and_cleanup(peers,database,failure):
    bad=await peers(auth='bearer'); good=await peers(auth='bearer')
    service=MCPService(database); a=await save_new(service,bad); b=await save_new(service,good)
    aliases=[tool_alias(r['server_id'],'search') for r in (a,b)]
    connections=[]
    model=LocalModel([reply(calls=[call(n,'{"query":"q"}',id=str(i)) for i,n in enumerate(aliases)]),reply('说明失败并使用可用服务')])
    c=chat(service,model,connections=connections)
    if failure=='catalog': bad.tools[0]['description']+=' changed'
    elif failure=='bad_secret': bad.expected_secret=REPLACEMENT
    else: bad.mode='hang' if failure=='timeout' else failure; bad.fail_on='tools/call'
    if failure=='timeout': c.registry.get(aliases[0]).timeout_seconds=3.0
    try: await c.answer([],'query')
    finally: await c.close()
    results=[json.loads(m['content']) for m in model.requests[-1][0] if m['role']=='tool']
    assert not results[0]['success'] and results[1]['success']
    expected={'catalog':'mcp_catalog_changed','401':'mcp_auth','tool_error':'mcp_tool_error','invalid_output':'mcp_output_schema','timeout':'tool_timeout','bad_secret':'mcp_auth'}[failure]
    assert results[0]['error'].startswith(expected),results
    if failure=='catalog': assert not methods(bad,'tools/call')
    if failure=='timeout': await asyncio.wait_for(bad.disconnected.wait(),2)
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
async def test_new_unselected_catalog_entry_not_added_and_eight_call_budget(peers,database):
    peer=await peers(); service=MCPService(database); record=await save_new(service,peer)
    peer.tools.append({'name':'write_later','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':True}})
    alias=tool_alias(record['server_id'],'search')
    model=LocalModel([reply(calls=[call(alias,'{"query":"q"}',id=str(i)) for i in range(8)]),
                     reply(calls=[call(alias,'{"query":"over budget"}',id='ninth')]),reply('done')])
    events=[]; c=chat(service,model,events=events)
    try:
        with pytest.raises(ForgePilotError, match='step_limit: 无工具收尾'):
            await c.answer([],'query')
    finally: await c.close()
    assert len(methods(peer,'tools/call'))==8 and c.mcp.calls==8
    assert [t.name for t in model.requests[0][1]]==[alias]
    assert len(model.requests)==2 and model.requests[-1][1] is None
    assert '不再请求工具' in model.requests[-1][0][0]['content']
    assert len(events)==16


@pytest.mark.asyncio
async def test_chat_context_budget_counts_definitions_before_model(peers,database,monkeypatch):
    import forgepilot.chat_service as module
    peer=await peers(); service=MCPService(database); await save_new(service,peer); peer.records.clear()
    model=LocalModel([]); c=chat(service,model)
    # Exactly the messages fit; the selected definition is what exceeds this budget.
    from forgepilot.memory_context import MEMORY_RULES,with_background
    messages=[{'role':'system','content':module.CHAT_SYSTEM+'\n'+module.MCP_RULES+'\n'+MEMORY_RULES},{'role':'user','content':'q'}]
    monkeypatch.setattr(module,'MAX_CHAT_CONTEXT',context_size(with_background(messages,{}),None)+1)
    try:
        with pytest.raises(ForgePilotError,match='context_limit'): await c.answer([],'q')
    finally: await c.close()
    assert not model.requests and not peer.records


@pytest.mark.asyncio
async def test_chat_real_stdio_lazy_roundtrip(tmp_path,database):
    service=MCPService(database); config=stdio_config(tmp_path)
    candidate={'name':'本地资料','transport':'stdio','connection':{'command':config.command,'args':list(config.args),'cwd':config.cwd},'selected_tools':['project_info']}
    probe=await service.probe(candidate)
    record=await service.save(str(uuid4()),candidate,base_revision=0,catalog_hash=probe['catalog_hash'])
    model=LocalModel([reply(calls=[call(tool_alias(record['server_id'],'project_info'),'{"topic":"architecture"}')]),reply('本地结果')])
    connections=[]; c=chat(service,model,connections=connections)
    assert not connections
    try: assert await c.answer([],'read local data')=='本地结果'
    finally: await c.close()
    stdio_closed(connections[0])


@pytest.mark.asyncio
async def test_chat_stdio_cancel_reaps_process_and_marks_record(tmp_path,database):
    service=MCPService(database); config=stdio_config(tmp_path,'hang')
    values={'name':'本地取消','transport':'stdio','connection':{'command':config.command,'args':list(config.args),'cwd':config.cwd},'selected_tools':['project_info']}
    probe=await service.probe(values)
    record=await service.save(str(uuid4()),values,base_revision=0,catalog_hash=probe['catalog_hash'])
    model=LocalModel([reply(calls=[call(tool_alias(record['server_id'],'project_info'),'{"topic":"architecture"}')])])
    connections=[]; events=[]; c=chat(service,model,connections=connections,events=events)
    running=asyncio.create_task(c.answer([],'取消实际本地进程'))
    try:
        await wait_method(tmp_path,'tools/call'); running.cancel()
        with pytest.raises(asyncio.CancelledError): await running
    finally: await c.close()
    stdio_closed(connections[0]); assert [e['status'] for e in events]==['running','cancelled']


@pytest.mark.asyncio
async def test_host_snapshot_captured_before_model_await(peers,tmp_path):
    peer=await peers(auth='bearer'); entered=Event(); release=Event()
    class PausedModel(LocalModel):
        async def chat(self,messages,tools=None):
            if not self.requests:
                entered.set(); await asyncio.to_thread(release.wait,8)
            return await super().chat(messages,tools)
    def factory():
        return ChatService(PausedModel([lambda messages,tools:reply(calls=[call(tools[0].name,'{"query":"accepted"}')]),reply('old snapshot')]))
    with serving(tmp_path,chat_factory=factory) as (url,app,store):
        async with httpx.AsyncClient(base_url=url,timeout=10) as client:
            record=await saved(client,peer); cid=(await client.post('/api/v1/conversations',json={})).json()['conversation_id']; rid=str(uuid4())
            try:
                await client.post(f'/api/v1/conversations/{cid}/requests',json={
                    'request_id':rid,'prompt':'snapshot','tool_mode':'web'})
                await eventually(entered.is_set)
                assert not methods(peer,'tools/call')
                assert (await client.delete(PREFIX+'/servers/'+record['server_id']+'?revision=1')).status_code==200
                release.set(); state=await wait_request(client,cid,rid)
                assert state['status']=='completed' and len(methods(peer,'tools/call'))==1
            finally: release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['chat','task'])
async def test_mode_filter_and_disabled_host_no_connections(peers,tmp_path,cpp_repo,rg_exe,git_exe,mode):
    peer=await peers(); requests=[]
    def model():
        class Capture(LocalModel):
            async def chat(self,messages,tools=None):
                requests.append(tools or [])
                return await super().chat(messages,tools)
        return Capture([reply('analysis'),reply(json.dumps(proposal()))])
    def configure(app):
        app.state.task_host.factory=lambda trace:TaskService(model(),rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    with serving(tmp_path,configure=configure,chat_factory=lambda:ChatService(model())) as (url,app,store):
        async with httpx.AsyncClient(base_url=url) as client:
            record=await saved(client,peer,config=candidate(peer,modes=[mode])); peer.records.clear()
            cid=(await client.post('/api/v1/conversations',json={})).json()['conversation_id']; rid=str(uuid4())
            await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'prompt':'chat'})
            assert (await wait_request(client,cid,rid))['status']=='completed'
            assert not requests[-1] and not peer.records
            requests.clear(); cid=(await client.post('/api/v1/conversations',json={'repository_path':str(cpp_repo)})).json()['conversation_id']; rid=str(uuid4())
            response=await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'mode':'task','prompt':'work'})
            tid=response.json()['task_id']
            from test_api import until
            await until(client,tid,{'waiting_approval'})
            assert (tool_alias(record['server_id'],'search') in {s.name for s in requests[0]})==(mode=='task')
            assert not peer.records
            await client.post(f'/api/v1/tasks/{tid}/cancel',json={'reason':'reject','plan_version':1})
    requests.clear()
    with serving(tmp_path,disabled=True,chat_factory=lambda:ChatService(model())) as (url,app,store):
        async with httpx.AsyncClient(base_url=url) as client:
            cid=(await client.post('/api/v1/conversations',json={})).json()['conversation_id']; rid=str(uuid4())
            await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'prompt':'disabled chat'})
            assert (await wait_request(client,cid,rid))['status']=='completed'
            assert requests==[[]] and not app.state.mcp_clients and not peer.records


@pytest.mark.asyncio
async def test_catalog_changed_on_approved_resume_is_blocked(peers,database,cpp_repo,rg_exe,git_exe):
    peer=await peers(); service=MCPService(database); saved=await save_new(service,peer); alias=tool_alias(saved['server_id'],'search')
    model=LocalModel([reply(calls=[call(alias,'{"query":"before"}')]),reply('analysis'),reply(json.dumps(proposal())),
                     reply(calls=[call(alias,'{"query":"after"}')]),reply('definition changed; no result')])
    work=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=MemoryTrace())
    work.configure_mcp(service.snapshots('task'),connection_factory=create_mcp_connection)
    try:
        pending=await work.start(str(cpp_repo),'verify definitions')
        initial_result=json.loads(next(m['content'] for m in model.requests[1][0] if m['role']=='tool'))
        assert initial_result['success'], initial_result['error']
        assert len(methods(peer,'tools/call'))==1
        peer.tools[0]['inputSchema']['description']='Changed after approval'
        work.approve(pending.task_id,pending.plan_version); await work.execute(pending.task_id)
        assert len(methods(peer,'tools/call'))==1
        assert 'mcp_catalog_changed' in model.requests[-1][0][-1]['content']
    finally: work.close(); await model.aclose()


@pytest.mark.asyncio
async def test_mcp_budget_survives_real_build_failure_and_repair(peers,database,build_repo,build_config,rg_exe,git_exe):
    from test_changes import write
    from test_repair import reflection
    peer=await peers(); service=MCPService(database); saved=await save_new(service,peer); alias=tool_alias(saved['server_id'],'search')
    model=LocalModel([reply(calls=[call(alias,'{"query":"analysis"}',id=str(i)) for i in range(7)]),
        reply('analysis'),reply(json.dumps(proposal('main.cpp'))),
        reply(calls=[call('read_file','{"path":"main.cpp"}',id='read-source'),
            write('main.cpp','int main(){return broken;}\n// existing user change\n')]),reply('initial change'),
        reply(calls=[call(alias,'{"query":"repair"}',id='eighth'),call(alias,'{"query":"budget denied"}',id='ninth')]),
        reflection('main.cpp'),reply(calls=[write('main.cpp','int main(){return 0;}\n// existing user change\n')]),reply('fixed')])
    work=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=MemoryTrace(),build_config=build_config)
    work.configure_mcp(service.snapshots('task'),connection_factory=create_mcp_connection)
    try:
        pending=await work.start(str(build_repo),'real build and repair'); work.approve(pending.task_id,pending.plan_version)
        result=await work.execute(pending.task_id)
        assert result.state==TaskState.COMPLETED,result.error
        assert result.repair_round==1 and result.build_result['tests']['passed']==1
        assert len(methods(peer,'tools/call'))==8 and work.mcp_manager.calls==8
        assert any('mcp_budget' in (r.get('error') or '') for r in result.tool_results)
        assert (build_repo/'notes.txt').read_text()=='user untracked\n'
    finally: work.close(); await model.aclose()


@pytest.mark.asyncio
async def test_work_approval_reconnect_uses_old_snapshot_and_shared_budget(peers,database,cpp_repo,rg_exe,git_exe):
    peer=await peers(auth='bearer'); replacement=await peers(auth='bearer'); replacement.expected_secret=REPLACEMENT
    service=MCPService(database); record=await save_new(service,peer); alias=tool_alias(record['server_id'],'search')
    before=(cpp_repo/'Counter.cpp').read_bytes(); trace=MemoryTrace()
    model=LocalModel([reply(calls=[call(alias,'{"query":"analysis"}',id=str(i)) for i in range(7)]+[
        call('write_file','{"path":"Counter.cpp","content":"unauthorized"}',id='denied')]),reply('analysis'),reply(json.dumps(proposal())),
        reply(calls=[call(alias,'{"query":"approved continuation"}',id='eighth'),call(alias,'{"query":"ninth"}',id='ninth')]),reply('没有需要修改的源码')])
    work=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    connections=[]
    def factory(config):
        c=create_mcp_connection(config); connections.append(c); return c
    work.configure_mcp(service.snapshots('task'),connection_factory=factory)
    try:
        pending=await work.start(str(cpp_repo),'查询后提出修改方案')
        assert pending.state==TaskState.WAITING_APPROVAL and not pending.approved_files
        assert (cpp_repo/'Counter.cpp').read_bytes()==before
        assert work.mcp_manager.calls==7
        http_closed(connections[0])
        await service.save(record['server_id'],candidate(replacement),base_revision=1,catalog_hash=record['catalog_hash'],secret_updates={'api_key':REPLACEMENT})
        service.delete(record['server_id'],revision=2)
        with pytest.raises(ForgePilotError,match='approval_required|invalid_state'): await work.execute(pending.task_id)
        work.approve(pending.task_id,pending.plan_version)
        result=await work.execute(pending.task_id)
        assert result.state==TaskState.COMPLETED and work.mcp_manager.calls==8
        assert len(methods(peer,'initialize'))==4  # two management, two runtime segments
        assert len(methods(peer,'tools/call'))==8 and not methods(replacement,'tools/call')
        assert any('mcp_budget' in (e.data.get('error') or '') for e in trace.events if e.kind=='tool_finished')
        assert all(e.data['server_id']==record['server_id'] for e in trace.events if e.kind=='mcp_tool_activity')
        assert (cpp_repo/'Counter.cpp').read_bytes()==before
    finally:
        await work.mcp_manager.close(); work.close(); await model.aclose()
    for connection in connections: http_closed(connection)


@pytest.mark.asyncio
async def test_real_api_chat_records_sources_cancel_history_and_delete(peers,tmp_path):
    peer=await peers(auth='bearer'); events=[]; models=[]
    for tool,name in zip(peer.tools,('tavily-search','tavily-extract')):
        tool['name']=name; tool.pop('outputSchema')
    peer.call_result={'content':[{'type':'text','text':json.dumps({'results':[
        {'raw_content':'<html><script>secret script</script><b>'+SECRET+'</b></html>'+('长正文'*10000),
         'title':'Qt 文档','url':'https://doc.qt.io/qt-6/'},
        {'title':'malicious','url':'javascript:alert(1)'}]},ensure_ascii=False)}]}
    def model_factory():
        def choose(messages,tools):
            names=[t.name for t in tools or []]; assert len(names)==1
            return reply(calls=[call(names[0],'{"query":"Qt MCP"}')])
        model=LocalModel([choose,reply('根据查询结果回答：https://doc.qt.io/qt-6/ '+SECRET)])
        models.append(model); return ChatService(model)
    clients=[]
    def configure(app):
        def connection(config):
            assert config.endpoint==TAVILY_ENDPOINT
            c=create_mcp_connection(config.model_copy(update={'endpoint':peer.endpoint})); clients.append(c); return c
        app.state.task_host.mcp_service.connection_factory=connection
    with serving(tmp_path,configure=configure,chat_factory=model_factory) as (url,app,store):
        async with httpx.AsyncClient(base_url=url,timeout=15) as client:
            config=candidate(peer,connection={'endpoint':TAVILY_ENDPOINT,'auth_type':'bearer','preset_id':'tavily'},selected_tools=['tavily-search'])
            await saved(client,peer,config=config)
            cid=(await client.post('/api/v1/conversations',json={})).json()['conversation_id']; rid=str(uuid4())
            path=f'/api/v1/conversations/{cid}/requests/{rid}'
            assert (await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'prompt':'查 Qt 文档'})).status_code==202
            state=await wait_request(client,cid,rid); assert state['status']=='completed'
            assert len(models[0].requests)==2 and len(methods(peer,'tools/call'))==1
            assert state['tool_activity']['status']=='succeeded' and 'mcp_calls_json' not in state
            records=(await client.get(path+'/mcp-tools')).json()['items']; assert len(records)==1
            assert records[0]['sources']==[{'title':'Qt 文档','url':'https://doc.qt.io/qt-6/'}]
            assert '<html>' not in records[0]['result_excerpt'] and '<script>' not in records[0]['result_excerpt']
            detail=(await client.get('/api/v1/conversations/'+cid)).json()
            assert SECRET not in json.dumps([records,detail]) and not store.list()
            assert (await client.get(f'/api/v1/conversations/{uuid4()}/requests/{rid}/mcp-tools')).status_code==404
            peer.mode='hang'; peer.fail_on='tools/call'; second=str(uuid4())
            await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':second,'prompt':'停止中的查询'})
            await asyncio.wait_for(peer.streaming.wait(),3)
            active=(await client.get(f'/api/v1/conversations/{cid}/requests/{second}')).json()
            assert active['tool_activity']['status']=='running'
            stopped=await client.post(f'/api/v1/conversations/{cid}/requests/{second}/stop')
            assert stopped.json()['status']=='cancelled'
            assert stopped.json()['tool_activity']['status']=='cancelled'
            await asyncio.wait_for(peer.disconnected.wait(),3)
            assert all(c.owner.done() and c.transport.closed for c in clients)
            assert len((await client.get(path+'/mcp-tools')).json()['items'])==1
            assert_no_plaintext(store,SECRET)
    # Real restart over the same DB keeps calls but never replays them.
    count=len(methods(peer,'tools/call'))
    with serving(tmp_path,chat_factory=model_factory) as (url,app,store):
        async with httpx.AsyncClient(base_url=url) as client:
            records=(await client.get(path+'/mcp-tools')).json()['items']
            assert records[0]['sources'][0]['url']=='https://doc.qt.io/qt-6/'
            assert (await client.delete('/api/v1/conversations/'+cid)).status_code==200
            assert (await client.get(path+'/mcp-tools')).status_code==404
            assert not store._db.execute('SELECT * FROM conversation_requests').fetchall()
    assert len(methods(peer,'tools/call'))==count and all(m.closed for m in models)


@pytest.mark.asyncio
async def test_legacy_import_marker_and_disabled_no_io(tmp_path):
    path=tmp_path/'explicit-mcp.json'; path.write_text(stdio_config(tmp_path).model_dump_json(),encoding='utf-8')
    with serving(tmp_path,configure=lambda app:setattr(app.state.task_host,'legacy_mcp_config',str(path))) as (_,app,store):
        service=app.state.task_host.mcp_service
        await eventually(lambda:service.store.get(LEGACY_SERVER_ID) is not None)
        assert service.get(LEGACY_SERVER_ID)['selected_tools']==['project_info']
        service.delete(LEGACY_SERVER_ID,revision=1)
        assert store._db.execute('SELECT COUNT(*) FROM mcp_migrations').fetchone()[0]==1
    with serving(tmp_path,configure=lambda app:setattr(app.state.task_host,'legacy_mcp_config',str(path))) as (_,app,_):
        assert app.state.task_host.mcp_service.list_servers()=={'items':[]} and not app.state.mcp_clients
    disabled=tmp_path/'disabled'; disabled.mkdir()
    with serving(disabled,disabled=True,configure=lambda app:setattr(app.state.task_host,'legacy_mcp_config',str(path))) as (_,app,store):
        assert not app.state.mcp_clients and not store._db.execute('SELECT * FROM mcp_migrations').fetchall()


def test_v4_upgrade_records_default_backup_and_record_bounds(tmp_path):
    path=tmp_path/'v4.sqlite3'; store=SqliteEventStore(path)
    cid=store.create_conversation()['conversation_id']; rid=str(uuid4())
    store.begin_conversation_request(cid,rid,'chat','old prompt',''); store.finish_conversation_request(rid,'completed','old answer'); store.close()
    with sqlite3.connect(path) as db:
        for table in ('security_events','validation_runs','workspace_records','checkpoint_intents','checkpoints',
                      'graph_node_results','graph_node_states','graph_run_inputs','graph_events',
                      'model_calls','approvals','artifact_relations','artifacts',
                      'task_node_attempts','task_graph_runs'):
            db.execute(f'DROP TABLE "{table}"')
        db.execute('DROP TABLE rag_index_runs'); db.execute('DROP TABLE rag_files'); db.execute('ALTER TABLE conversation_requests DROP COLUMN mcp_calls_json'); db.execute('DROP TABLE mcp_migrations'); db.execute('PRAGMA user_version=4')
    store=SqliteEventStore(path)
    try:
        assert store._db.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA_VERSION
        assert store.conversation_mcp_tools(cid,rid)['items']==[]
        assert store.conversation_detail(cid)['messages'][-1]['content']=='old answer'
        backups=list(tmp_path.glob('*.v4-backup-*.sqlite3')); assert len(backups)==1
        with sqlite3.connect(backups[0]) as db: assert db.execute('PRAGMA user_version').fetchone()[0]==4
        record={'sequence':1,'server_id':str(uuid4()),'server_name':'服务','tool_name':'search','tool_call_id':'id','status':'succeeded',
                'arguments_excerpt':'😀'*1024,'result_excerpt':'😀'*2048,'sources':[{'title':'🌿'*256,'url':'https://example.test/'+('x'*2000)}]*10,'error_code':None,'duration_ms':1}
        encoded=bounded_records([dict(record,sequence=i+1) for i in range(8)])
        values=json.loads(encoded)
        assert len(values)==8 and len(encoded.encode())<=65536
        assert sum(len(r['sources']) for r in values)<=10
    finally: store.close()


def test_interrupted_call_recovery_is_durable_and_never_replayed(tmp_path):
    path=tmp_path/'interrupted.sqlite3'; store=SqliteEventStore(path)
    cid=store.create_conversation()['conversation_id']; rid=str(uuid4())
    store.begin_conversation_request(cid,rid,'chat','question','')
    store.record_mcp_call(cid,rid,{'sequence':1,'tool_call_id':'id','server_id':str(uuid4()),'server_name':'server',
        'tool_name':'query','status':'running','arguments_excerpt':'{}','result_excerpt':'','duration_ms':0,'error_code':None,'sources':[]})
    store.close()
    store=SqliteEventStore(path)
    try:
        state=store.conversation_request(cid,rid)
        assert state['status']=='interrupted' and state['tool_activity']['status']=='interrupted'
        raw=json.loads(store._db.execute('SELECT mcp_calls_json FROM conversation_requests WHERE request_id=?',(rid,)).fetchone()[0])
        assert raw[0]['status']=='interrupted' and raw[0]['error_code']=='service_interrupted'
        assert not store.list()
    finally: store.close()


@pytest.mark.parametrize('url',['javascript:alert(1)','file:///C:/secret','data:text/html,hi','https://user:key@example.test','https://example.test/ with space'])
def test_sources_reject_active_or_credential_urls(url):
    assert search_sources(json.dumps({'structuredContent':{'results':[{'title':'bad','url':url}]}}))==[]


def test_sources_text_formats_and_json_are_not_generic_url_scraping():
    output={'content':[{'type':'text','text':'Request ID: fixture\nDetailed Results:\n\nTitle: Qt title\nID: 1\nURL: https://doc.qt.io/\nContent: reference\n\nURL: https://example.test/extract\nRaw Content: extracted\nImages:\n[1] URL: https://example.test/image.png'}]}
    assert search_sources(json.dumps(output))==[
        {'title':'Qt title','url':'https://doc.qt.io/'},
        {'title':'https://example.test/extract','url':'https://example.test/extract'}]
    assert search_sources(json.dumps({'content':[{'type':'text','text':'Some model text https://doc.qt.io/'}]}))==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['invalid','hang'])
async def test_legacy_import_failure_or_pending_does_not_block_chat_startup(tmp_path,kind):
    config_path=tmp_path/'legacy.json'
    config_path.write_text('invalid' if kind=='invalid' else stdio_config(tmp_path,'init_hang').model_dump_json(),encoding='utf-8')
    with serving(tmp_path,configure=lambda app:setattr(app.state.task_host,'legacy_mcp_config',str(config_path)),
                 chat_factory=lambda:ChatService(LocalModel([reply('普通问答仍可用')]))) as (url,app,store):
        async with httpx.AsyncClient(base_url=url,timeout=5) as client:
            cid=(await client.post('/api/v1/conversations',json={})).json()['conversation_id']; rid=str(uuid4())
            await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'prompt':'hello'})
            assert (await wait_request(client,cid,rid))['status']=='completed'
            assert not store._db.execute('SELECT * FROM mcp_migrations').fetchall()
    for c in app.state.mcp_clients:
        if c.process is not None: stdio_closed(c)


@pytest.mark.asyncio
async def test_existing_legacy_id_is_preserved_without_parsing_or_reimport(peers,database):
    peer=await peers(); service=MCPService(database)
    old=await save_new(service,peer,server_id=LEGACY_SERVER_ID); count=len(peer.records)
    await service.import_legacy('missing-config-is-never-read')
    assert service.get(LEGACY_SERVER_ID)==old and len(peer.records)==count
    service.delete(LEGACY_SERVER_ID,revision=1)
    await service.import_legacy('missing-config-is-never-read')
    assert not service.list_servers()['items'] and len(peer.records)==count


@pytest.mark.asyncio
async def test_legacy_task_constructor_also_shares_eight_call_budget(tmp_path,cpp_repo,rg_exe,git_exe):
    from forgepilot.mcp_config import TOOL_NAME
    model=LocalModel([reply(calls=[call(TOOL_NAME,'{"topic":"architecture"}',id=str(i)) for i in range(8)]),
                     reply(calls=[call(TOOL_NAME,'{"topic":"architecture"}',id='ninth')]),reply('done')])
    work=TaskService(model,rg_executable=rg_exe,git_executable=git_exe,trace=MemoryTrace(),mcp_config=stdio_config(tmp_path))
    result=await work.analyze(str(cpp_repo),'legacy bounded query')
    assert result.state==TaskState.COMPLETED and work.mcp_manager.calls==8
    assert result.tool_results[-1]['error'].startswith('mcp_budget')
    assert sum(r['success'] for r in result.tool_results)==8
    await model.aclose(); work.close()


@pytest.mark.asyncio
async def test_disabled_host_overrides_explicit_legacy_task_factory(tmp_path,cpp_repo,rg_exe,git_exe):
    models=[]; services=[]
    def configure(app):
        def factory(trace):
            m=LocalModel([reply('analysis'),reply(json.dumps(proposal()))]); models.append(m)
            s=TaskService(m,rg_executable=rg_exe,git_executable=git_exe,trace=trace,mcp_config=stdio_config(tmp_path)); services.append(s)
            return s
        app.state.task_host.factory=factory
    with serving(tmp_path,disabled=True,configure=configure) as (url,app,store):
        async with httpx.AsyncClient(base_url=url) as client:
            result=await client.post('/api/v1/tasks',json={'repository_path':str(cpp_repo),'prompt':'disabled'})
            from test_api import until
            tid=result.json()['task_id']; await until(client,tid,{'waiting_approval'})
            assert services[0]._mcp is None and not services[0].mcp_config
            assert all(not t.name.startswith('mcp_') for t in models[0].requests[0][1])
            await client.post(f'/api/v1/tasks/{tid}/cancel',json={'reason':'reject','plan_version':1})


@pytest.mark.asyncio
async def test_work_host_persists_trace_calls_and_resumes_deleted_configuration(peers,tmp_path,cpp_repo,rg_exe,git_exe):
    peer=await peers(auth='bearer')
    for tool,name in zip(peer.tools,('tavily-search','tavily-extract')):
        tool['name']=name; tool.pop('outputSchema')
    peer.call_result={'content':[{'type':'text','text':json.dumps({'results':[
        {'raw_content':'完整正文'*8000, 'title':'工作来源','url':'https://example.test/work/1'}]})}]}
    def choose(messages,tools):
        selected=next(t for t in tools if t.name.startswith('mcp_'))
        return reply(calls=[call(selected.name,'{"query":"work lookup"}',id='work-'+str(len(messages)))])
    def configure(app):
        original=app.state.task_host.mcp_service.connection_factory
        app.state.task_host.mcp_service.connection_factory=lambda config: original(config.model_copy(update={'endpoint':peer.endpoint}))
        app.state.task_host.factory=lambda trace:TaskService(LocalModel([
            choose,reply('analysis'),reply(json.dumps(proposal())),choose,reply('根据实际查询完成 '+SECRET)]),
            rg_executable=rg_exe,git_executable=git_exe,trace=trace)
    with serving(tmp_path,configure=configure) as (url,app,store):
        async with httpx.AsyncClient(base_url=url,timeout=10) as client:
            record=await saved(client,peer,config=candidate(peer,connection={'endpoint':TAVILY_ENDPOINT,
                'auth_type':'bearer','preset_id':'tavily'},selected_tools=['tavily-search']))
            cid=(await client.post('/api/v1/conversations',json={'repository_path':str(cpp_repo)})).json()['conversation_id']; rid=str(uuid4())
            response=await client.post(f'/api/v1/conversations/{cid}/requests',json={'request_id':rid,'mode':'task','prompt':'inspect'})
            tid=response.json()['task_id']; from test_api import until
            pending=await until(client,tid,{'waiting_approval'})
            assert not pending['approved_files']
            endpoint=f'/api/v1/conversations/{cid}/requests/{rid}/mcp-tools'
            assert len((await client.get(endpoint)).json()['items'])==1
            assert all(c.owner.done() and c.transport.closed for c in app.state.mcp_clients)
            assert (await client.delete(PREFIX+'/servers/'+record['server_id']+'?revision=1')).status_code==200
            assert (await client.post(f'/api/v1/tasks/{tid}/approve',json={'plan_version':1})).status_code==202
            state=await wait_request(client,cid,rid); assert state['status']=='completed'
            records=(await client.get(endpoint)).json()['items']
            assert [r['status'] for r in records]==['succeeded','succeeded']
            assert all(r['sources']==[{'title':'工作来源','url':'https://example.test/work/1'}] for r in records)
            assert len(methods(peer,'tools/call'))==2
            assert store._db.execute('SELECT mcp_calls_json FROM conversation_requests WHERE request_id=?',(rid,)).fetchone()[0]=='[]'
            assert store._db.execute("SELECT COUNT(*) FROM task_events WHERE kind='mcp_tool_activity'").fetchone()[0]==4
            assert_no_plaintext(store,SECRET)
            assert (await client.delete('/api/v1/conversations/'+cid)).status_code==200
            assert not store.list() and not store._db.execute('SELECT * FROM task_events').fetchall()
