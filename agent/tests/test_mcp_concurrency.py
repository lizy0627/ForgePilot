"""Optimistic writes while real MCP discovery is in flight (no DB lock held)."""
import asyncio
from uuid import uuid4

import pytest

from test_mcp_http import peers
from test_mcp_service import database, candidate, save_new
from forgepilot.errors import ForgePilotError
from forgepilot.mcp_config import create_mcp_connection
from forgepilot.mcp_service import MCPService


class DiscoveryGate:
    """Pause delivery after actual HTTP discovery, including its failures."""
    def __init__(self):
        self.arrivals = asyncio.Queue()
        self.release = asyncio.Event()
        self.clients = []

    def __call__(self, config):
        client = create_mcp_connection(config)
        self.clients.append(client)
        gate = self

        class Connection:
            @property
            def server_info(self):
                return client.server_info

            async def start(self):
                await client.start()

            async def list_tools(self):
                try:
                    return await client.list_tools()
                finally:
                    gate.arrivals.put_nowait(True)
                    await gate.release.wait()

            async def close(self):
                await client.close()

        return Connection()

    async def entered(self, count=1):
        async with asyncio.timeout(5):
            for _ in range(count):
                await self.arrivals.get()


@pytest.mark.asyncio
async def test_two_editors_same_revision_only_one_wins_without_network_transaction(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    gate = DiscoveryGate()
    delayed = MCPService(database, connection_factory=gate)
    tasks = [asyncio.create_task(delayed.save(saved["server_id"], candidate(peer, name=name),
        base_revision=1, catalog_hash=saved["catalog_hash"])) for name in ("editor-a", "editor-b")]
    try:
        await gate.entered(2)
        assert not database._db.in_transaction
        # RLock permits reentry in this thread: use another real thread to prove
        # discovery does not hold the store lock or block unrelated history writes.
        async with asyncio.timeout(2):
            history = await asyncio.to_thread(database.create_conversation, title="during MCP discovery")
        assert database.conversation_detail(history["conversation_id"])["title"] == "during MCP discovery"
    finally:
        gate.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, ForgePilotError)]
    assert len(successes) == len(conflicts) == 1 and conflicts[0].code == "mcp_config_conflict"
    assert successes[0]["revision"] == 2
    assert service.get(saved["server_id"]) == successes[0]
    assert all(client.owner.done() and client.transport.closed for client in gate.clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "save", "test", "enable"])
async def test_network_result_cannot_overwrite_concurrent_config_change(peers, database, operation):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer, candidate(peer, enabled=operation != "enable"))
    sid = saved["server_id"]
    gate = DiscoveryGate()
    delayed = MCPService(database, connection_factory=gate)
    if operation == "probe":
        pending = delayed.probe(candidate(peer), server_id=sid, base_revision=1)
    elif operation == "save":
        pending = delayed.save(sid, candidate(peer, name="stale update"), base_revision=1, catalog_hash=saved["catalog_hash"])
    elif operation == "test":
        pending = delayed.test(sid, revision=1)
    else:
        pending = delayed.set_enabled(sid, revision=1, enabled=True)
    task = asyncio.create_task(pending)
    try:
        await gate.entered()
        if operation == "enable":
            service.delete(sid, revision=1)
        else:
            await service.set_enabled(sid, revision=1, enabled=False)
        expected = service.store.get(sid)
    finally:
        gate.release.set()
    with pytest.raises(ForgePilotError, match="mcp_config_conflict"):
        await task
    assert service.store.get(sid) == expected


@pytest.mark.asyncio
async def test_late_failed_test_does_not_mark_new_revision_failed(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    gate = DiscoveryGate()
    delayed = MCPService(database, connection_factory=gate)
    peer.mode, peer.fail_on = "401", "tools/list"
    task = asyncio.create_task(delayed.test(sid, revision=1))
    try:
        await gate.entered()
        await service.set_enabled(sid, revision=1, enabled=False)
        expected = service.store.get(sid)
    finally:
        gate.release.set()
    with pytest.raises(ForgePilotError, match="mcp_config_conflict"):
        await task
    assert service.store.get(sid) == expected and expected["last_check_status"] == "success"


@pytest.mark.asyncio
async def test_delete_recreate_same_uuid_does_not_accept_old_inflight_revision(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    gate = DiscoveryGate()
    delayed = MCPService(database, connection_factory=gate)
    task = asyncio.create_task(delayed.test(sid, revision=1))
    try:
        await gate.entered()
        service.delete(sid, revision=1)
        recreated = await save_new(service, peer, candidate(peer, name="recreated"), server_id=sid)
        assert recreated["revision"] == 1 and recreated["created_at"] != saved["created_at"]
        expected = service.store.get(sid)
    finally:
        gate.release.set()
    with pytest.raises(ForgePilotError, match="mcp_config_conflict"):
        await task
    assert service.store.get(sid) == expected


@pytest.mark.asyncio
async def test_concurrent_enables_for_different_ids_recheck_global_capacity(peers, database):
    peer = await peers()
    service = MCPService(database)
    for _ in range(3):
        await save_new(service, peer)
    disabled = [await save_new(service, peer, candidate(peer, enabled=False)) for _ in range(2)]
    gate = DiscoveryGate()
    delayed = MCPService(database, connection_factory=gate)
    tasks = [asyncio.create_task(delayed.set_enabled(row["server_id"], revision=1, enabled=True)) for row in disabled]
    try:
        await gate.entered(2)
    finally:
        gate.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, ForgePilotError)]
    assert len(errors) == 1 and errors[0].code == "mcp_limit" and "enabled" in str(errors[0])
    assert sum(row["enabled"] for row in service.store.all()) == 4
    assert sorted(service.get(row["server_id"])["revision"] for row in disabled) == [1, 2]


@pytest.mark.asyncio
async def test_conflicts_are_checked_before_network_and_stable_uuid_does_not_duplicate(peers, database):
    peer = await peers()
    service = MCPService(database)
    saved = await save_new(service, peer)
    sid = saved["server_id"]
    count = len(peer.records)
    with pytest.raises(ForgePilotError, match="mcp_config_conflict"):
        await service.save(sid, candidate(peer), base_revision=0, catalog_hash=saved["catalog_hash"])
    with pytest.raises(ForgePilotError, match="unknown_mcp_server"):
        await service.test(str(uuid4()), revision=1)
    assert len(peer.records) == count and len(service.store.all()) == 1
    assert service.get(sid)["revision"] == 1
