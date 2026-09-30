"""Stage-two service against real HTTP/stdio, SQLite and current-user DPAPI."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import logging
import sqlite3
import sys
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fixtures.mcp_http_peer import SECRET, TOOLS
from test_mcp_http import peers
from test_mcp import PEER
from forgepilot.errors import ForgePilotError
from forgepilot.mcp_config import checked_server_settings, mcp_presets, TAVILY_ENDPOINT, TAVILY_TOOLS, create_mcp_connection
from forgepilot.mcp_service import MCPService
from forgepilot.security import Redactor
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.windows_secrets import unprotect_secrets

REPLACEMENT = "fixture-mcp-replacement-credential"
MODEL_SECRET = "fixture-model-credential-for-redaction"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["port", "host", "path", "slash", "auth", "header", "scheme"])
async def test_scope_change_requires_explicit_key_before_any_network(peers, database, change):
    a, b = await peers(auth="api_key"), await peers(auth="api_key")
    service = MCPService(database)
    saved = await save_new(service, a)
    before = service.store.get(saved["server_id"])
    config = candidate(a)
    connection = config["connection"]
    connection.update({
        "port": {"endpoint": b.endpoint},
        "host": {"endpoint": a.endpoint.replace("127.0.0.1", "localhost")},
        "path": {"endpoint": a.endpoint + "-other"},
        "slash": {"endpoint": a.endpoint + "/"},
        "auth": {"auth_type": "bearer", "header_name": None},
        "header": {"header_name": "X-New-Key"},
        "scheme": {"endpoint": a.endpoint.replace("http:", "https:")},
    }[change])
    prior = len(a.records)
    for updates in ({}, {"api_key": None}, {"api_key": ""}, {"api_key": "\n"}):
        for operation in ("probe", "save"):
            with pytest.raises(ForgePilotError, match="mcp_secret_required"):
                if operation == "probe":
                    await service.probe(config, server_id=saved["server_id"], base_revision=1, secret_updates=updates)
                else:
                    await service.save(saved["server_id"], config, base_revision=1,
                        catalog_hash=saved["catalog_hash"], secret_updates=updates)
    assert b.records == [] and len(a.records) == prior
    assert service.store.get(saved["server_id"]) == before
    if change == "port":
        b.expected_secret = REPLACEMENT
        updated = await service.save(saved["server_id"], config, base_revision=1,
            catalog_hash=saved["catalog_hash"], secret_updates={"api_key": REPLACEMENT})
        assert updated["revision"] == 2
        assert all(r["headers"].get("x-api-key") == REPLACEMENT for r in b.records)


def test_scope_normalization_preserves_exact_path():
    from forgepilot.mcp_config import credential_scope
    def scope(url, header="X-Key"):
        return credential_scope(checked_server_settings({"name": "scope", "transport": "streamable_http",
            "connection": {"endpoint": url, "auth_type": "api_key", "header_name": header}}))
    assert scope("HTTPS://EXAMPLE.TEST") == scope("https://example.test:443/", "x-key")
    assert scope("https://example.test/mcp") != scope("https://example.test/mcp/")
    assert scope("https://example.test/%6dcp") != scope("https://example.test/mcp")


@pytest.mark.parametrize("public", [["missing"], ["DEBUG", "debug"], ["DEBUG"] * 17])
def test_public_env_subset_and_windows_uniqueness(public):
    with pytest.raises(ForgePilotError, match="mcp_config"):
        checked_server_settings({"name": "env", "transport": "stdio", "connection": {
            "command": sys.executable, "env_names": ["DEBUG"], "public_env_names": public}})


@pytest.mark.asyncio
async def test_public_env_real_discovery_persistence_snapshot_call_and_removal(database, tmp_path):
    from forgepilot.mcp_manager import MCPManager
    from forgepilot.tools.registry import ToolRegistry
    from forgepilot.tools.mcp_tools import tool_alias
    from forgepilot.mcp_service import tool_fingerprint
    service = MCPService(database)
    config = {"name": "ordinary flags", "transport": "stdio", "selected_tools": ["search"], "connection": {
        "command": sys.executable, "cwd": str(tmp_path), "args": ["-I", "-u", str(PEER), "generic_public", str(tmp_path / "wire.jsonl")],
        "env_names": ["DEBUG", "PYTHONUNBUFFERED", "MCP_TEST_TOKEN"], "public_env_names": ["debug", "PYTHONUNBUFFERED"]}}
    updates = {"env:DEBUG": "true", "env:PYTHONUNBUFFERED": "1", "env:MCP_TEST_TOKEN": SECRET}
    probe = await service.probe(config, secret_updates=updates)
    sid = "11111111-1111-4111-8111-111111111111"
    saved = await service.save(sid, config, base_revision=0, catalog_hash=probe["catalog_hash"], secret_updates=updates)
    assert all(__import__('re').fullmatch(r"sha256:[0-9a-f]{64}", t["fingerprint"]) for t in saved["tools"])
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == updates
    assert SECRET not in json.dumps(service.get(sid)) and 'secret_updates' not in service.get(sid)
    fresh = MCPService(database)  # Reload persisted JSON, never reuse the draft.
    events = []
    manager = MCPManager(fresh.snapshots("chat"), on_tool_event=events.append)
    registry = ToolRegistry(redactor=manager.redactor); manager.register(registry)
    alias = tool_alias(sid, "search")
    try:
        result = await manager.execute(registry, alias, {"query": "true 1"}, "call-true-1")
        assert result.success and SECRET not in result.output and "[REDACTED]" in result.output
        output = json.loads(result.output)
        assert output["server"] == sid and output["structuredContent"]["url"] == "https://example.test/true/1"
        assert json.loads(output["content"][0]["text"])["DEBUG"] == "true"
        assert manager.redactor.text(alias) == alias and events[-1]["tool_call_id"] == "call-true-1"
        assert events[-1]["status"] == "succeeded"
    finally:
        await manager.close()
    config["connection"]["env_names"] = ["MCP_TEST_TOKEN"]
    config["connection"]["public_env_names"] = []
    await service.save(sid, config, base_revision=1, catalog_hash=probe["catalog_hash"])
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {"env:MCP_TEST_TOKEN": SECRET}
    assert_no_plaintext(database, SECRET)


def test_catalog_structure_short_secrets_and_sensitive_contracts(database):
    service = MCPService(database)
    tool = deepcopy(TOOLS[0])
    from forgepilot.mcp_service import tool_fingerprint
    entry = service._catalog([tool], Redactor(("1", "true")))[0]
    assert entry["fingerprint"] == tool_fingerprint(tool) and entry["compatible"] is True
    for field in ("name", "inputSchema", "outputSchema", "execution", "annotations"):
        bad = deepcopy(tool)
        if field == "name": bad[field] = "tool-" + SECRET
        else: bad[field] = {**bad.get(field, {}), "description": SECRET}
        entry = service._catalog([bad], Redactor((SECRET,)))[0]
        assert entry["compatible"] is False and entry["input_schema"] is None and entry["output_schema"] is None
        assert SECRET not in json.dumps(entry)
        assert entry["fingerprint"] == tool_fingerprint(bad)


@pytest.mark.asyncio
async def test_legacy_env_default_classification_and_deliberate_reclassification(database, tmp_path):
    service = MCPService(database)
    config = {"name": "env", "transport": "stdio", "selected_tools": ["search"], "connection": {
        "command": sys.executable, "args": ["-I", "-u", str(PEER), "generic", str(tmp_path / "wire.jsonl")],
        "env_names": ["DEBUG", "OTHER"]}}
    updates = {"env:DEBUG": "true", "env:OTHER": SECRET}
    probe = await service.probe(config, secret_updates=updates)
    sid = str(uuid4())
    await service.save(sid, config, base_revision=0, catalog_hash=probe["catalog_hash"], secret_updates=updates)
    row = service.store.get(sid)
    legacy = json.loads(row["connection_json"]); legacy.pop("public_env_names")
    database._db.execute("UPDATE mcp_servers SET connection_json=? WHERE server_id=?", (json.dumps(legacy), sid))
    service = MCPService(database)
    assert service.get(sid)["connection"]["public_env_names"] == []
    assert service.snapshots("chat")[0].request_redactor(Redactor()).text("true") == "[REDACTED]"
    config["connection"]["public_env_names"] = ["DEBUG"]
    row = service.store.get(sid)
    _, _, _, redactor = service._prepare(config, row, {})
    assert redactor.text("true " + SECRET) == "true [REDACTED]"
    _, _, _, redactor = service._prepare(config, row, {"env:OTHER": "true"})
    assert redactor.text("true " + SECRET) == "[REDACTED] [REDACTED]"
    saved = await service.save(sid, config, base_revision=1, catalog_hash=probe["catalog_hash"])
    assert saved["connection"]["public_env_names"] == ["DEBUG"]
    assert service.store.get(sid)["secrets_dpapi"] == row["secrets_dpapi"]
    # A public connection argument reflecting a credential still fails before discovery.
    config["connection"]["args"].append(SECRET)
    with pytest.raises(ForgePilotError, match="mcp_config"):
        await service.probe(config, server_id=sid, base_revision=2)


@pytest.fixture
def database(tmp_path):
    store = SqliteEventStore(tmp_path / "mcp.sqlite3", redactor=Redactor((MODEL_SECRET,)))
    yield store
    store.close()


def candidate(peer, **changes):
    return {"name": "测试查询", "transport": "streamable_http", "connection": {"endpoint": peer.endpoint,
        "auth_type": peer.auth, "header_name": "X-API-Key" if peer.auth == "api_key" else None},
        "selected_tools": ["search"], **changes}


async def save_new(service, peer, config=None, *, secret_updates=None, server_id=None):
    config = config or candidate(peer)
    updates = secret_updates if secret_updates is not None else ({"api_key": peer.expected_secret} if peer.auth != "none" else {})
    probe = await service.probe(config, secret_updates=updates)
    saved = await service.save(server_id or str(uuid4()), config, base_revision=0,
                               catalog_hash=probe["catalog_hash"], secret_updates=updates)
    return saved


def probe_only(peer):
    methods = [row["body"].get("method") for row in peer.records if row["http"] == "POST"]
    assert "tools/call" not in methods
    assert set(methods) <= {"initialize", "notifications/initialized", "tools/list", "notifications/cancelled"}


def assert_no_plaintext(database, *secrets):
    from pathlib import Path
    for suffix in ("", "-wal", "-shm"):
        path = Path(database.path + suffix)
        if path.exists():
            contents = path.read_bytes()
            for secret in secrets:
                assert secret.encode() not in contents and secret.encode("utf-16-le") not in contents


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["none", "bearer", "api_key"])
async def test_service_lifecycle_reopen_and_probe_only(peers, database, auth):
    peer = await peers(auth=auth)
    service = MCPService(database)
    cid = database.create_conversation(title="原有历史")["conversation_id"]
    config = candidate(peer)
    probe = await service.probe(config, secret_updates={"api_key": SECRET} if auth != "none" else {})
    assert service.list_servers() == {"items": []}
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    assert saved["revision"] == 1 and saved["last_check_status"] == "success"
    assert saved["has_secret"] == (auth != "none") and saved["catalog_hash"] == probe["catalog_hash"]
    summary = service.list_servers()["items"][0]
    assert "tools" not in summary and "connection" not in summary and summary["selected_tool_count"] == 1
    assert len(service.snapshots("chat")) == 1
    tested = await service.test(sid, revision=1)
    assert tested["revision"] == 1 and tested["updated_at"] == saved["updated_at"]
    same = await service.save(sid, config, base_revision=1, catalog_hash=saved["catalog_hash"])
    assert same["revision"] == 1
    disabled = await service.set_enabled(sid, revision=1, enabled=False)
    assert disabled["revision"] == 2 and not service.snapshots("chat")
    enabled = await service.set_enabled(sid, revision=2, enabled=True)
    assert enabled["revision"] == 3
    path = database.path
    database.close()
    reopened = SqliteEventStore(path)
    try:
        service = MCPService(reopened)
        assert service.get(sid) == enabled
        assert (await service.test(sid, revision=3))["revision"] == 3
        assert service.delete(sid, revision=3) == {"server_id": sid, "deleted": True}
        assert service.list_servers() == {"items": []}
        assert reopened.conversation_detail(cid)["conversation_id"] == cid
    finally:
        reopened.close()
    probe_only(peer)


@pytest.mark.asyncio
async def test_secret_omission_replace_clear_and_auth_removal(peers, database):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid, config = saved["server_id"], candidate(peer, name="改名但保留密钥")
    before = service.store.get(sid)
    renamed = await service.save(sid, config, base_revision=1, catalog_hash=saved["catalog_hash"])
    assert renamed["revision"] == 2
    assert service.store.get(sid)["secrets_dpapi"] == before["secrets_dpapi"]
    assert unprotect_secrets(before["secrets_dpapi"]) == {"api_key": SECRET}
    peer.expected_secret = REPLACEMENT
    replaced = await service.save(sid, config, base_revision=2, catalog_hash=saved["catalog_hash"],
                                  secret_updates={"api_key": REPLACEMENT})
    assert replaced["revision"] == 3
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {"api_key": REPLACEMENT}
    stable = service.store.get(sid)
    for value in (None, ""):
        with pytest.raises(ForgePilotError, match="mcp_secret_required|mcp_config"):
            await service.save(sid, config, base_revision=3, catalog_hash=saved["catalog_hash"], secret_updates={"api_key": value})
        assert service.store.get(sid) == stable
    peer.auth = "none"
    cleared = await service.save(sid, candidate(peer), base_revision=3, catalog_hash=saved["catalog_hash"],
                                 secret_updates={"api_key": None})
    assert cleared["revision"] == 4 and not cleared["has_secret"]
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {}
    start = len(peer.records)
    await service.test(sid, revision=4)
    assert all("authorization" not in row["headers"] for row in peer.records[start:])
    probe_only(peer)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates,code", [(None, "mcp_secret_required"), ({}, "mcp_secret_required"),
    ({"api_key": None}, "mcp_secret_required"), ({"api_key": ""}, "mcp_config"),
    ({"api_key": 123}, "mcp_config"), ({"api_key": SECRET, "unknown": SECRET}, "mcp_config")])
async def test_new_server_requires_valid_credential_patch_before_network(peers, database, updates, code):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    with pytest.raises(ForgePilotError, match=code) as error:
        await service.probe(candidate(peer), secret_updates=updates)
    assert SECRET not in str(error.value)
    assert peer.records == [] and service.list_servers() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [-1, True, "1", 1.0, None, 2**63])
async def test_revision_is_strict_bounded_integer_before_network(database, revision):
    service = MCPService(database)
    with pytest.raises(ForgePilotError, match="mcp_config"):
        await service.probe({"invalid": "unused"}, base_revision=revision)
    assert service.list_servers() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("401", "mcp_auth"), ("403", "mcp_auth"), ("429", "mcp_capacity"),
    ("html", "mcp_not_mcp"), ("malformed", "mcp_protocol")])
async def test_failed_save_and_probe_leave_old_record_and_key_untouched(peers, database, mode, code):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    old = service.store.get(sid)
    peer.mode, peer.expected_secret = mode, REPLACEMENT
    config = candidate(peer, name="失败候选不能保存")
    with pytest.raises(ForgePilotError, match=code) as error:
        await service.save(sid, config, base_revision=1, catalog_hash=saved["catalog_hash"], secret_updates={"api_key": REPLACEMENT})
    assert SECRET not in str(error.value) and REPLACEMENT not in str(error.value)
    assert service.store.get(sid) == old
    with pytest.raises(ForgePilotError, match=code):
        await service.probe(config, secret_updates={"api_key": REPLACEMENT})
    assert len(service.store.all()) == 1 and service.store.get(sid) == old
    assert_no_plaintext(database, SECRET, REPLACEMENT, MODEL_SECRET)
    peer.mode, peer.expected_secret = "normal", SECRET
    assert (await service.test(sid, revision=1))["last_check_status"] == "success"


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_configuration_and_ciphertext(peers, database, monkeypatch):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    before = service.store.get(sid)
    peer.expected_secret = REPLACEMENT
    actual = database._transaction
    @contextmanager
    def fail_before_commit():
        with actual():
            yield
            raise sqlite3.OperationalError("injected COMMIT failure")
    monkeypatch.setattr(database, "_transaction", fail_before_commit)
    with pytest.raises(ForgePilotError, match="mcp_storage"):
        await service.save(sid, candidate(peer, name="不能提交"), base_revision=1, catalog_hash=saved["catalog_hash"],
                           secret_updates={"api_key": REPLACEMENT})
    assert service.store.get(sid) == before and not database._db.in_transaction
    assert unprotect_secrets(before["secrets_dpapi"]) == {"api_key": SECRET}
    monkeypatch.setattr(database, "_transaction", actual)
    peer.expected_secret = SECRET
    assert (await service.test(sid, revision=1))["last_check_status"] == "success"


@pytest.mark.asyncio
async def test_dpapi_failure_does_not_erase_config_and_complete_replacement_repairs_it(peers, database):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    database._db.execute("UPDATE mcp_servers SET secrets_dpapi=? WHERE server_id=?", (b"corrupt-fixture", sid))
    assert service.get(sid)["has_secret"]
    with pytest.raises(ForgePilotError, match="mcp_secret_unavailable"):
        await service.test(sid, revision=1)
    assert service.get(sid)["last_error_code"] == "mcp_secret_unavailable"
    snapshot = service.snapshots("chat")[0]
    assert snapshot.secret_error == "mcp_secret_unavailable"
    with pytest.raises(ForgePilotError, match="mcp_secret_unavailable"):
        snapshot.connection_config()
    stable = service.store.get(sid)
    with pytest.raises(ForgePilotError, match="mcp_secret_unavailable"):
        await service.save(sid, candidate(peer, name="保留不可读密钥"), base_revision=1, catalog_hash=saved["catalog_hash"])
    assert service.store.get(sid) == stable
    repaired = await service.save(sid, candidate(peer), base_revision=1, catalog_hash=saved["catalog_hash"],
                                  secret_updates={"api_key": SECRET})
    assert repaired["revision"] == 2 and repaired["last_error_code"] is None
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {"api_key": SECRET}


@pytest.mark.asyncio
async def test_stdio_environment_credentials_are_removed_and_default_cwd_is_visible(database, tmp_path):
    service = MCPService(database)
    config = {"name": "本地查询", "transport": "stdio", "selected_tools": ["search"], "connection": {
        "command": sys.executable, "args": ["-I", "-u", str(PEER), "generic", str(tmp_path / "wire.jsonl")],
        "env_names": ["MCP_TEST_TOKEN", "EXTRA"]}}
    updates = {"env:MCP_TEST_TOKEN": SECRET, "env:EXTRA": REPLACEMENT}
    probe = await service.probe(config, secret_updates=updates)
    assert probe["connection"]["cwd"] == str(__import__("pathlib").Path(sys.executable).parent)
    saved = await service.save(str(uuid4()), config, base_revision=0, catalog_hash=probe["catalog_hash"], secret_updates=updates)
    sid = saved["server_id"]
    config["connection"]["env_names"] = ["MCP_TEST_TOKEN"]
    updated = await service.save(sid, config, base_revision=1, catalog_hash=probe["catalog_hash"])
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {"env:MCP_TEST_TOKEN": SECRET}
    assert SECRET not in json.dumps(updated) and REPLACEMENT not in json.dumps(updated)
    config["connection"]["env_names"] = []
    await service.save(sid, config, base_revision=2, catalog_hash=probe["catalog_hash"])
    assert unprotect_secrets(service.store.get(sid)["secrets_dpapi"]) == {}
    log = (tmp_path / "wire.jsonl").read_text(encoding="utf-8")
    assert "tools/call" not in log and SECRET not in log and REPLACEMENT not in log


@pytest.mark.asyncio
async def test_snapshots_remain_immutable_after_replace_disable_delete(peers, database):
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer, candidate(peer, modes=["task"]))
    sid = saved["server_id"]
    assert not service.snapshots("chat")
    snapshot = service.snapshots("task")[0]
    assert SECRET not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 2
    with pytest.raises(ValidationError):
        snapshot.config.name = "mutation"
    snapshot.catalog[0]["name"] = "mutation"
    assert snapshot.catalog[0]["name"] != "mutation"
    peer.expected_secret = REPLACEMENT
    updated = await service.save(sid, candidate(peer, modes=["task"]), base_revision=1, catalog_hash=saved["catalog_hash"],
                                 secret_updates={"api_key": REPLACEMENT})
    assert service.snapshots("task")[0].connection_config().secret == REPLACEMENT
    disabled = await service.set_enabled(sid, revision=updated["revision"], enabled=False)
    service.delete(sid, revision=disabled["revision"])
    assert not service.snapshots("task")
    assert snapshot.connection_config().secret == SECRET and snapshot.revision == 1


@pytest.mark.asyncio
async def test_catalog_hash_selected_fingerprints_and_failure_status(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    peer.tools.reverse()
    assert (await service.probe(candidate(peer)))["catalog_hash"] == saved["catalog_hash"]
    with pytest.raises(ForgePilotError, match="mcp_catalog_changed"):
        await service.save(sid, candidate(peer), base_revision=1, catalog_hash="sha256:" + "0" * 64)
    disabled = await service.set_enabled(sid, revision=1, enabled=False)
    added = deepcopy(TOOLS[0]); added["name"] = "new-unselected"
    peer.tools.append(added)
    enabled = await service.set_enabled(sid, revision=disabled["revision"], enabled=True)
    assert enabled["selected_tools"] == ["search"] and len(enabled["tools"]) == 2
    selected = next(tool for tool in peer.tools if tool["name"] == "search")
    selected["description"] = "changed selected definition"
    before = service.store.get(sid)
    with pytest.raises(ForgePilotError, match="mcp_catalog_changed"):
        await service.test(sid, revision=enabled["revision"])
    failed = service.get(sid)
    assert failed["revision"] == enabled["revision"] and failed["last_error_code"] == "mcp_catalog_changed"
    assert service.store.get(sid)["catalog_json"] == before["catalog_json"]


@pytest.mark.asyncio
async def test_untrusted_catalog_is_redacted_and_incompatible_tools_cannot_be_selected(peers, database, caplog):
    caplog.set_level(logging.DEBUG)
    peer = await peers(auth="bearer")
    good = deepcopy(TOOLS[0]); good["description"] = "public " + SECRET + " " + MODEL_SECRET
    definitions = [good]
    for name, change in (("writes", {"annotations": {"readOnlyHint": False}}),
            ("bad", {"inputSchema": {"type": "object", "$ref": "https://invalid/schema"}}),
            ("required", {"execution": {"taskSupport": "required"}}),
            ("malformed", {"execution": {"taskSupport": {"unexpected": True}}}),
            ("reflected", {"inputSchema": {"type": "object", "description": SECRET}})):
        definitions.append({**deepcopy(TOOLS[0]), "name": name, **change})
    peer.tools = definitions
    service = MCPService(database)
    probe = await service.probe(candidate(peer), secret_updates={"api_key": SECRET})
    assert sum(tool["compatible"] for tool in probe["tools"]) == 1
    text = json.dumps(probe)
    assert SECRET not in text and MODEL_SECRET not in text
    for name in ("writes", "bad", "required", "malformed", "reflected", "missing"):
        with pytest.raises(ForgePilotError, match="mcp_selection"):
            await service.save(str(uuid4()), candidate(peer, selected_tools=[name]), base_revision=0,
                catalog_hash=probe["catalog_hash"], secret_updates={"api_key": SECRET})
    saved = await save_new(service, peer)
    await service.test(saved["server_id"], revision=1)
    peer.mode = "rpc_error"  # No tool call: this does not affect probe semantics.
    await service.test(saved["server_id"], revision=1)
    database._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    assert_no_plaintext(database, SECRET, MODEL_SECRET)
    assert SECRET not in caplog.text and MODEL_SECRET not in caplog.text
    probe_only(peer)


def test_tavily_preset_is_fixed_and_not_claimed_live_verified():
    preset = mcp_presets()[0]
    assert preset["connection"]["endpoint"] == TAVILY_ENDPOINT
    assert preset["connection"]["auth_type"] == "bearer" and preset["default_tools"] == list(TAVILY_TOOLS)
    assert preset["real_service_verified"] is False
    preset["connection"]["endpoint"] = "https://changed.invalid"
    assert mcp_presets()[0]["connection"]["endpoint"] == TAVILY_ENDPOINT


@pytest.mark.asyncio
@pytest.mark.parametrize('names,read_only,accepted', [
    (('tavily-search', 'tavily-extract'), True, True),
    (('tavily_search', 'tavily_extract'), True, True),
    (('tavily_search', 'tavily-extract'), True, False),
    (('tavily_search_extra', 'tavily_extract'), True, False),
    (('tavily_search', 'tavily_extract'), False, False),
])
async def test_tavily_exact_remote_and_legacy_names_with_real_discovery(peers, database, names, read_only, accepted):
    peer = await peers(auth='bearer')
    for tool, name in zip(peer.tools, names):
        tool['name'] = name
        tool['annotations'] = {'readOnlyHint': read_only}
    extra = deepcopy(peer.tools[0])
    extra['name'] = 'tavily_crawl'
    peer.tools.append(extra)
    preset = mcp_presets()[0]
    config = {'name': 'Tavily', 'transport': preset['transport'], 'connection': preset['connection']}
    service = MCPService(database, connection_factory=lambda settings:
                         create_mcp_connection(settings.model_copy(update={'endpoint': peer.endpoint})))
    if not accepted:
        with pytest.raises(ForgePilotError, match='mcp_preset_mismatch'):
            await service.probe(config, secret_updates={'api_key': SECRET})
        assert service.list_servers() == {'items': []}
    else:
        found = await service.probe(config, secret_updates={'api_key': SECRET})
        assert found['default_selected_tools'] == list(names)
        config['selected_tools'] = found['default_selected_tools']
        saved = await service.save(str(uuid4()), config, base_revision=0,
                                   catalog_hash=found['catalog_hash'], secret_updates={'api_key': SECRET})
        assert saved['selected_tools'] == list(names)
        assert 'tavily_crawl' not in service.snapshots('chat')[0].config.selected_tools
        assert (await service.test(saved['server_id'], revision=1))['last_check_status'] == 'success'
    probe_only(peer)
    assert_no_plaintext(database, SECRET)


@pytest.mark.parametrize("change", [{"transport": "http"}, {"effect": "write"}, {"enabled": 1},
    {"name": " "}, {"modes": []}, {"modes": ["chat", "chat"]}, {"modes": ["admin"]},
    {"selected_tools": ["search", "search"]}, {"selected_tools": [str(i) for i in range(21)]},
    {"revision": 1}, {"unknown": "field"}, {"connection": {"endpoint": "https://example.com/mcp", "command": "x.exe"}},
    {"connection": {"endpoint": "https://example.com/mcp", "secret": SECRET}},
    {"connection": {"endpoint": "https://example.com/mcp", "preset_id": "tavily", "auth_type": "bearer"}}])
def test_fixed_config_rejects_mixed_unknown_invalid_and_write_fields(change):
    config = {"name": "fixed", "transport": "streamable_http", "connection": {"endpoint": "https://example.com/mcp"}}
    with pytest.raises(ForgePilotError, match="mcp_config") as error:
        checked_server_settings({**config, **change})
    assert SECRET not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "save", "test"])
@pytest.mark.parametrize("fail_on", ["initialize", "tools/list"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_service_timeout_and_cancel_reclaim_connections_without_candidate_writes(
        peers, database, caplog, operation, fail_on, cancel):
    caplog.set_level(logging.DEBUG)
    peer = await peers(auth="bearer")
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    before = service.store.get(sid)
    clients = []
    def factory(config):
        client = create_mcp_connection(config)
        clients.append(client)
        return client
    bounded = MCPService(database, connection_factory=factory, probe_timeout=1.0)
    peer.mode, peer.fail_on = "hang", fail_on
    if operation == "test":
        action = bounded.test(sid, revision=1)
    else:
        peer.expected_secret = REPLACEMENT
        if operation == "probe":
            action = bounded.probe(candidate(peer, name="not saved"), secret_updates={"api_key": REPLACEMENT})
        else:
            action = bounded.save(sid, candidate(peer, name="not saved"), base_revision=1,
                catalog_hash=saved["catalog_hash"], secret_updates={"api_key": REPLACEMENT})
    start = monotonic()
    pending = asyncio.create_task(action)
    async with asyncio.timeout(5):
        await peer.streaming.wait()
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(ForgePilotError, match="mcp_timeout"):
                await pending
        await peer.disconnected.wait()
    assert monotonic() - start < 4.0
    assert all(client.owner.done() and client.closer.done() and client.transport.closed for client in clients)
    after = service.store.get(sid)
    if operation == "test" and not cancel:
        assert after["last_error_code"] == "mcp_timeout" and after["last_check_status"] == "failed"
        for key in before.keys() - {"last_checked_at", "last_check_status", "last_error_code"}:
            assert after[key] == before[key]
    else:
        assert after == before
    assert len(service.store.all()) == 1 and not database._db.in_transaction
    assert_no_plaintext(database, SECRET, REPLACEMENT, MODEL_SECRET)
    assert SECRET not in caplog.text and REPLACEMENT not in caplog.text and MODEL_SECRET not in caplog.text
    probe_only(peer)
