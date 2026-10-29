"""Real QML -> production HTTP API -> SQLite/DPAPI -> HTTP and stdio peers.

Fault middleware injects errors and delays/damages responses; committed-save
cases always run production discovery/storage first. An uncommitted failure
exercises verification of an unchanged or missing row. Tavily fixture routing changes the transport destination
after production preset validation, so no public service or real key is used.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from fastapi import Request


def run(root, output_root):
    sys.path.insert(0, str(root / "agent/tests"))
    from fixtures.mcp_http_peer import HttpPeer, TOOLS
    from test_mcp_api import serving
    from forgepilot.mcp_config import create_mcp_connection, MCPHttpConfig, TAVILY_ENDPOINT

    output = output_root.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    print("OFFLINE MCP UI evidence: " + str(output), flush=True)

    async def exercise():
        peer = await HttpPeer(auth="bearer", sse=True).start()
        tavily = await HttpPeer(auth="bearer").start()
        tavily.tools = deepcopy(TOOLS)
        for tool, name in zip(tavily.tools, ("tavily-search", "tavily-extract")):
            tool["name"] = name
        tavily.tools.append({**deepcopy(TOOLS[0]), "name": "tavily-crawl"})
        peer.tools.extend([
            {"name": "write_item", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}},
            {"name": "bad_schema", "inputSchema": {"$ref": "https://invalid.example/schema"}},
        ])
        # These are generated test-only values, never a configured user credential.
        secret, replacement = "fixture-ui-" + uuid4().hex, "fixture-ui-" + uuid4().hex
        peer.expected_secret = tavily.expected_secret = secret
        state = {"drop_save": False, "delay_save": False, "fail_detail": False, "delay_probe": False, "delay_list": False,
                 "delay_detail": False, "uncommitted_save": False, "probe_error": None, "save_error": None}
        evidence = {"dropped_saved_responses": 0, "delayed_saves": 0, "failed_details": 0, "delayed_probes": 0, "delayed_lists": 0,
                    "delayed_details": 0, "uncommitted_saves": 0}
        put_ids = []
        detail_ids = []
        clients = []

        class FaultDelivery:
            def __init__(self, app): self.app = app
            async def __call__(self, scope, receive, send):
                path = scope.get("path", "")
                if scope.get("method") == "PUT" and path.startswith("/api/v1/mcp/servers/"):
                    put_ids.append(path.rsplit("/", 1)[-1])
                    if state["uncommitted_save"]:
                        state["uncommitted_save"] = False; evidence["uncommitted_saves"] += 1
                        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                        await send({"type": "http.response.body", "body": b"{"})
                        return
                if scope.get("method") == "GET" and path.startswith("/api/v1/mcp/servers/"):
                    detail_ids.append(path.rsplit("/", 1)[-1])
                failure = state["probe_error"] if path == "/api/v1/mcp/probe" else None
                if scope.get("method") == "PUT" and path.startswith("/api/v1/mcp/servers/"):
                    failure = state["save_error"]
                if scope.get("method") == "GET" and path.startswith("/api/v1/mcp/servers/") and state["fail_detail"]:
                    evidence["failed_details"] += 1
                    failure = "mcp_unavailable"
                if failure:
                    payload = json.dumps({"error": {"code": failure, "message": "private fixture " + secret,
                        "details": None, "task_id": None}}).encode()
                    await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": payload})
                    return
                drop = scope.get("method") == "PUT" and path.startswith("/api/v1/mcp/servers/") and state["drop_save"]
                delay_save = scope.get("method") == "PUT" and path.startswith("/api/v1/mcp/servers/") and state["delay_save"]
                delay = path == "/api/v1/mcp/probe" and state["delay_probe"]
                delay_list = path == "/api/v1/mcp/servers" and scope.get("method") == "GET" and state["delay_list"]
                delay_detail = path.startswith("/api/v1/mcp/servers/") and scope.get("method") == "GET" and state["delay_detail"]
                if drop: state["drop_save"] = False
                if delay_save: state["delay_save"] = False
                if delay: state["delay_probe"] = False
                if delay_list: state["delay_list"] = False
                if delay_detail: state["delay_detail"] = False
                messages = []
                async def capture(message): messages.append(message)
                await self.app(scope, receive, capture if drop or delay or delay_list or delay_save or delay_detail else send)
                if drop:
                    assert messages[0]["status"] in (200, 201), "Fault must occur after successful real save"
                    evidence["dropped_saved_responses"] += 1
                    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", b"1")]})
                    await send({"type": "http.response.body", "body": b"{"})
                elif delay or delay_list or delay_save or delay_detail:
                    evidence["delayed_details" if delay_detail else "delayed_saves" if delay_save else "delayed_probes" if delay else "delayed_lists"] += 1
                    await asyncio.sleep(1.5 if delay_save or delay_detail else .7)
                    for message in messages: await send(message)

        def configure(app):
            app.add_middleware(FaultDelivery)
            def connection(config):
                if isinstance(config, MCPHttpConfig) and config.endpoint == TAVILY_ENDPOINT:
                    config = config.model_copy(update={"endpoint": tavily.endpoint})
                client = create_mcp_connection(config)
                clients.append(client)
                return client
            app.state.task_host.mcp_service.connection_factory = connection

            @app.post("/fixture/control")
            async def control(request: Request):
                data = await request.json()
                for name in state:
                    if name in data: state[name] = data[name]
                for name in ("mode", "auth", "expected_secret", "fail_on"):
                    if name in data: setattr(peer, name, data[name])
                if data.get("change_catalog"):
                    peer.tools[0]["description"] += " changed fixture definition"
                return {"ok": True, "put_ids": list(put_ids), "detail_ids": list(detail_ids), "peer_requests": len(peer.records)}

        try:
            with tempfile.TemporaryDirectory(prefix="ForgePilot MCP UI ") as temp:
                folder = Path(temp)
                first, second = folder / "first", folder / "second"
                first.mkdir(); second.mkdir()
                script = folder / "带空格 MCP 示例.py"
                stdio_evidence = folder / "stdio-evidence.jsonl"
                # Verify exact argv and explicit environment in a real child,
                # recording only booleans, never credential values.
                script.write_text(
                    "import os,sys,json,runpy\n"
                    f"assert sys.argv[1:] == ['参数 含空格', '独立参数']\n"
                    f"import hashlib\nassert 'MCP_FIXTURE' not in os.environ or hashlib.sha256(os.environ['MCP_FIXTURE'].encode()).hexdigest() == {hashlib.sha256(secret.encode()).hexdigest()!r}\n"
                    "assert 'FORGEPILOT_API_KEY' not in os.environ and 'OPENAI_API_KEY' not in os.environ\n"
                    "assert 'DEBUG' not in os.environ or os.environ['DEBUG'] == 'true'\n"
                    "assert 'PYTHONUNBUFFERED' not in os.environ or os.environ['PYTHONUNBUFFERED'] == '1'\n"
                    f"with open({str(stdio_evidence)!r},'a',encoding='utf-8') as stream: stream.write(json.dumps(dict(env_present='MCP_FIXTURE' in os.environ))+'\\n')\n"
                    f"runpy.run_path({str(root / 'agent/examples/mcp_project_notes.py')!r},run_name='__main__')\n",
                    encoding="utf-8")
                env = os.environ.copy()
                env.update(FORGEPILOT_INTEGRATION_QML=str(root / "client/Main.qml"),
                    FORGEPILOT_INTEGRATION_OUTPUT=str(output), FORGEPILOT_MCP_ENDPOINT=peer.endpoint,
                    FORGEPILOT_MCP_SECRET=secret, FORGEPILOT_MCP_REPLACEMENT=replacement,
                    FORGEPILOT_MCP_PYTHON=sys.executable, FORGEPILOT_MCP_SCRIPT=str(script))

                def qt(url, alternate, phase):
                    env.update(FORGEPILOT_INTEGRATION_URL=url, FORGEPILOT_MCP_ALTERNATE=alternate, FORGEPILOT_MCP_PHASE=phase)
                    report = output / (phase + ".txt")
                    result = subprocess.run([str(root / "client/build/Release/ForgePilotMcpTests.exe"), "-v1", "-o", str(report) + ",txt"],
                        env=env, capture_output=True, timeout=300)
                    detail = (report.read_bytes() if report.exists() else b"") + result.stdout + result.stderr
                    assert all(s.encode() not in detail for s in (secret, replacement)), "Credential leaked to Qt report"
                    (output / (phase + ".log")).write_bytes(detail)
                    print(detail.decode("utf-8", errors="replace"), flush=True)
                    if result.returncode: raise RuntimeError("Qt MCP " + phase + " failed")

                with serving(first, configure=configure) as (url, app, store), serving(second) as (alternate, _, other_store):
                    qt(url, alternate, "diagnostics")
                    qt(url, alternate, "recovery")
                    assert not store._db.execute("SELECT * FROM mcp_servers").fetchall()
                    recovery_evidence = dict(evidence)
                    qt(url, alternate, "manage")
                    assert len(store._db.execute("SELECT * FROM mcp_servers").fetchall()) == 1
                    assert not other_store._db.execute("SELECT * FROM mcp_servers").fetchall()
                    assert not store.list() and store.conversation_page()["total"] == 0
                    for file in first.glob("api.sqlite3*"):
                        if file.suffix != ".lock":
                            assert all(s.encode() not in file.read_bytes() for s in (secret, replacement))
                # Same database + fresh Qt process: stored selection, credentials,
                # timestamp and startup-level disabled state are all exercised.
                before_disabled = len(clients)
                with serving(first, disabled=True, configure=configure) as (url, _, store):
                    qt(url, url, "restart-disabled")
                    assert not store._db.execute("SELECT * FROM mcp_servers").fetchall()
                assert len(clients) == before_disabled
                assert all(c.owner.done() and c.transport.closed for c in clients if hasattr(c, "owner"))
                assert all(c.process.returncode is not None and c.reader.done() and c.stderr_reader.done()
                    for c in clients if hasattr(c, "process") and c.process is not None)
                await asyncio.wait_for(peer.disconnected.wait(), 10)
                stdio_rows = [json.loads(line) for line in stdio_evidence.read_text(encoding="utf-8").splitlines()]
                assert {row["env_present"] for row in stdio_rows} == {True, False}
                for p in (peer, tavily):
                    assert p.records and all(r["body"].get("method") != "tools/call" for r in p.records)
                headers = [r["headers"] for r in peer.records]
                # HttpPeer normalizes whole header lines to lower case.
                assert any(h.get("authorization") == "bearer " + secret for h in headers)
                assert any(h.get("authorization") == "bearer " + replacement for h in headers)
                assert any("authorization" not in h and "x-api-key" not in h for h in headers)
                assert any(h.get("x-api-key") == secret for h in headers)
                assert {key: evidence[key] - recovery_evidence[key] for key in evidence} == {
                    "dropped_saved_responses": 2, "delayed_saves": 1, "failed_details": 2, "delayed_probes": 1, "delayed_lists": 1,
                    "delayed_details": 0, "uncommitted_saves": 0}
                evidence["recovery"] = recovery_evidence
                evidence.update(http_requests=len(peer.records), preset_requests=len(tavily.records),
                    no_business_calls=True, credentials_redacted=True, migration_version=5,
                    real_service_verified=False, restart_and_disabled=True, stdio_runs=len(stdio_rows),
                    stdio_env_cleared=True, hanging_http_disconnected=True)
                (output / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
                print("REAL QML + FastAPI + HTTP/stdio + SQLite/DPAPI: management, secrets, faults, cancellation, restart passed", flush=True)
        finally:
            await peer.close(); await tavily.close()
    asyncio.run(exercise())
    return 0
