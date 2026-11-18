"""Configuration admission limits and static preset semantics over real HTTP."""
from copy import deepcopy
from uuid import uuid4

import pytest

from test_mcp_http import peers
from test_mcp_service import database, candidate, save_new, probe_only, SECRET, TOOLS
from forgepilot.errors import ForgePilotError
from forgepilot.mcp_config import create_mcp_connection, mcp_presets, TAVILY_TOOLS
from forgepilot.mcp_service import MCPService
import forgepilot.mcp_service as service_module


def tools_named(count, *, description="small"):
    return [{**deepcopy(TOOLS[0]), "name": "tool_" + str(index), "description": description} for index in range(count)]


@pytest.mark.asyncio
async def test_eight_saved_four_enabled_and_rejected_update_retains_old_config(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = [await save_new(service, peer, candidate(peer, enabled=i < 4)) for i in range(8)]
    before = service.store.all()
    with pytest.raises(ForgePilotError, match="mcp_limit.*servers"):
        await save_new(service, peer, candidate(peer, enabled=False))
    with pytest.raises(ForgePilotError, match="mcp_limit.*enabled"):
        await service.set_enabled(saved[4]["server_id"], revision=1, enabled=True)
    assert service.store.all() == before
    # Editing at the storage limit replaces an existing record rather than
    # counting it twice; disabling immediately frees enabled capacity.
    renamed = await service.save(saved[0]["server_id"], candidate(peer, name="rename at capacity"),
                                base_revision=1, catalog_hash=saved[0]["catalog_hash"])
    await service.set_enabled(saved[0]["server_id"], revision=renamed["revision"], enabled=False)
    assert (await service.set_enabled(saved[4]["server_id"], revision=1, enabled=True))["enabled"]
    assert len(service.store.all()) == 8
    probe_only(peer)


@pytest.mark.asyncio
async def test_twenty_enabled_tools_across_modes_and_servers(peers, database):
    peer = await peers()
    peer.tools = tools_named(20)
    names = [tool["name"] for tool in peer.tools]
    service = MCPService(database)
    await save_new(service, peer, candidate(peer, selected_tools=names[:11], modes=["chat"]))
    second = await save_new(service, peer, candidate(peer, selected_tools=names[:9], modes=["task"]))
    before = service.store.all()
    with pytest.raises(ForgePilotError, match="mcp_limit.*selected_tools"):
        await service.save(second["server_id"], candidate(peer, selected_tools=names[:10], modes=["task"]),
                           base_revision=1, catalog_hash=second["catalog_hash"])
    assert service.store.all() == before


@pytest.mark.asyncio
async def test_definition_budget_applies_per_mode_on_save_and_enable(peers, database):
    peer = await peers()
    peer.tools = tools_named(3, description="x" * 4096)
    names = [tool["name"] for tool in peer.tools]
    service = MCPService(database)
    await save_new(service, peer, candidate(peer, selected_tools=names, modes=["chat"]))
    second = await save_new(service, peer, candidate(peer, selected_tools=names, modes=["task"]))
    before = service.store.all()
    with pytest.raises(ForgePilotError, match="mcp_limit.*definitions/chat"):
        await service.save(second["server_id"], candidate(peer, selected_tools=names),
                           base_revision=1, catalog_hash=second["catalog_hash"])
    assert service.store.all() == before
    disabled = await save_new(service, peer, candidate(peer, enabled=False, selected_tools=names))
    before = service.store.get(disabled["server_id"])
    with pytest.raises(ForgePilotError, match="mcp_limit.*definitions/chat"):
        await service.set_enabled(disabled["server_id"], revision=1, enabled=True)
    assert service.store.get(disabled["server_id"]) == before


@pytest.mark.asyncio
async def test_empty_whitelist_can_be_stored_disabled_but_never_enabled(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer, candidate(peer, selected_tools=[], enabled=False))
    before = service.store.get(saved["server_id"])
    with pytest.raises(ForgePilotError, match="mcp_selection"):
        await service.set_enabled(saved["server_id"], revision=1, enabled=True)
    assert service.store.get(saved["server_id"]) == before
    with pytest.raises(ForgePilotError, match="mcp_selection"):
        await save_new(service, peer, candidate(peer, selected_tools=[]))


@pytest.mark.asyncio
async def test_detail_response_limit_failure_rolls_back_before_commit(peers, database, monkeypatch):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    before = service.store.get(saved["server_id"])
    peer.tools[0]["description"] = "expanded catalog " + "x" * 3000
    probe = await service.probe(candidate(peer))
    # Use a smaller test budget positioned between the real discovery envelope
    # and the larger saved-detail envelope (revision, selection, timestamps).
    monkeypatch.setattr(service_module, "MAX_RESPONSE_BYTES", len(service_module.canonical(probe).encode()) + 1)
    with pytest.raises(ForgePilotError, match="mcp_limit"):
        await service.save(saved["server_id"], candidate(peer, name="must roll back"),
                           base_revision=1, catalog_hash=probe["catalog_hash"])
    assert service.store.get(saved["server_id"]) == before and not database._db.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["catalog_limit", "tool_limit", "page_limit"])
async def test_oversize_catalogs_rejected_without_overwriting_saved_values(peers, database, mode):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    before = service.store.get(saved["server_id"])
    peer.mode = mode
    with pytest.raises(ForgePilotError, match="mcp_limit"):
        await service.save(saved["server_id"], candidate(peer), base_revision=1, catalog_hash=saved["catalog_hash"])
    assert service.store.get(saved["server_id"]) == before


@pytest.mark.asyncio
async def test_preset_selects_exact_read_names_and_rejects_missing_or_write_tool(peers, database):
    peer = await peers(auth="bearer")
    peer.tools = [{**deepcopy(TOOLS[0]), "name": name} for name in (*TAVILY_TOOLS, "tavily-crawl")]
    # Test-only routing to a loopback peer. The production preset remains fixed;
    # this test proves no facts about the real Tavily service.
    service = MCPService(database, connection_factory=lambda config:
        create_mcp_connection(config.model_copy(update={"endpoint": peer.endpoint})))
    preset = mcp_presets()[0]
    config = {"name": preset["name"], "transport": preset["transport"], "connection": preset["connection"],
              "selected_tools": list(TAVILY_TOOLS)}
    probe = await service.probe(config, secret_updates={"api_key": SECRET})
    assert probe["default_selected_tools"] == list(TAVILY_TOOLS)
    saved = await service.save(str(uuid4()), config, base_revision=0, catalog_hash=probe["catalog_hash"],
                               secret_updates={"api_key": SECRET})
    assert saved["selected_tools"] == list(TAVILY_TOOLS) and "tavily-crawl" not in saved["selected_tools"]
    before = service.store.get(saved["server_id"])
    peer.tools[0]["name"] = "tavily_search"  # A similar spelling cannot match.
    with pytest.raises(ForgePilotError, match="mcp_preset_mismatch"):
        await service.save(saved["server_id"], config, base_revision=1, catalog_hash=probe["catalog_hash"])
    peer.tools[0]["name"] = TAVILY_TOOLS[0]
    peer.tools[0]["annotations"] = {"readOnlyHint": False}
    with pytest.raises(ForgePilotError, match="mcp_preset_mismatch"):
        await service.probe(config, secret_updates={"api_key": SECRET})
    assert service.store.get(saved["server_id"]) == before
    probe_only(peer)
