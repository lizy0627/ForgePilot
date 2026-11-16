"""A confirmed HTTP authentication fault survives owned cleanup past a deadline."""
import asyncio
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from forgepilot.errors import ForgePilotError
from forgepilot.mcp_config import create_mcp_connection
from forgepilot.mcp_http import MCPHttpClient
from forgepilot.mcp_service import MCPService
from forgepilot.sqlite_store import SqliteEventStore
from test_mcp_http import peers
from test_mcp_service import SECRET, assert_no_plaintext, candidate


@dataclass
class ProbeCase:
    service: MCPService
    store: SqliteEventStore
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    clients: list[MCPHttpClient] = field(default_factory=list)
    pending: asyncio.Task | None = None


@pytest_asyncio.fixture
async def probe_case(tmp_path):
    store = SqliteEventStore(tmp_path / "probe.sqlite3")
    case = ProbeCase(MCPService(store, probe_timeout=0.5), store)

    def connect(config):
        client = create_mcp_connection(config)
        assert isinstance(client, MCPHttpClient)
        original_close = client.transport.aclose

        async def close():
            case.closing.set()
            await case.release.wait()
            await original_close()

        client.transport.aclose = close
        case.clients.append(client)
        return client

    case.service.connection_factory = connect
    try:
        yield case
    finally:
        case.release.set()
        if case.pending is not None:
            if not case.pending.done():
                case.pending.cancel()
            await asyncio.gather(case.pending, return_exceptions=True)
        for client in case.clients:
            await client.close()
        store.close()


async def begin_probe(case, peer):
    case.pending = asyncio.create_task(case.service.probe(
        candidate(peer), secret_updates={"api_key": SECRET}))
    async with asyncio.timeout(5):
        await case.closing.wait()
    assert not case.pending.done() and not case.clients[0].transport.closed


async def wait_deadline(case):
    # Observe the real unchanged deadline cancelling the probe, rather than
    # extending it or sleeping until an assumed network response time.
    async with asyncio.timeout(5):
        while not case.pending.cancelling():
            await asyncio.sleep(0.005)
    assert case.service.probe_timeout == 0.5 and not case.pending.done()


def assert_closed(case, peer):
    assert len(case.clients) == 1
    client = case.clients[0]
    assert client.owner.done() and client.transport.closed and client.http.is_closed
    assert sum(row["body"].get("method") == "initialize" for row in peer.records) == 1
    assert case.service.list_servers()["items"] == []
    assert_no_plaintext(case.store, SECRET)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["401", "403"])
async def test_confirmed_auth_survives_probe_deadline_during_cleanup(peers, probe_case, status):
    peer = await peers(mode=status, auth="bearer")
    await begin_probe(probe_case, peer)
    assert probe_case.clients[0].failure.code == "mcp_auth"
    await wait_deadline(probe_case)
    probe_case.release.set()
    with pytest.raises(ForgePilotError, match="mcp_auth") as failure:
        await probe_case.pending
    assert len(failure.value.message) <= 512 and SECRET not in str(failure.value)
    assert_closed(probe_case, peer)


@pytest.mark.asyncio
async def test_unanswered_probe_still_times_out_after_cleanup(peers, probe_case):
    peer = await peers(mode="hang", auth="bearer")
    await begin_probe(probe_case, peer)
    await wait_deadline(probe_case)
    probe_case.release.set()
    with pytest.raises(ForgePilotError, match="mcp_timeout"):
        await probe_case.pending
    assert_closed(probe_case, peer)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["401", "hang"])
async def test_repeated_caller_cancel_wins_and_joins_owned_cleanup(peers, probe_case, mode):
    peer = await peers(mode=mode, auth="bearer")
    await begin_probe(probe_case, peer)
    probe_case.pending.cancel()
    await asyncio.sleep(0)
    probe_case.pending.cancel()
    assert not probe_case.pending.done()
    probe_case.release.set()
    with pytest.raises(asyncio.CancelledError):
        await probe_case.pending
    assert_closed(probe_case, peer)
