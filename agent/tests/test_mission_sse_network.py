"""Real HTTP coverage of the production v2 route and durable subscriber."""
import asyncio
from contextlib import contextmanager
import json
import socket
from threading import Thread
import time
import httpx
import pytest
import uvicorn

from forgepilot.api import create_app
from forgepilot.mission_events import publish_mission_event
from forgepilot.sqlite_store import SqliteEventStore
from test_artifact_protocol import TASK_ID
from test_mission_events import make_mission, finish_mission
from test_api_network import next_block


@contextmanager
def serving_events(tmp_path, *, page_size=5, max_subscribers=8):
    store = SqliteEventStore(tmp_path / "mission-network.sqlite3")
    make_mission(store)
    app = create_app(lambda trace: None, store=store, heartbeat_seconds=.05,
                     mission_stream_max_subscribers=max_subscribers)
    streams = app.state.mission_events
    streams.page_size, streams.poll_seconds = page_size, .005
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(.01)
    try:
        yield url, store, streams
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        store.close()
        assert not thread.is_alive()


def event_frames(text):
    result = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        fields = dict(line.split(": ", 1) for line in lines if ": " in line and not line.startswith(":"))
        if "data" in fields:
            event = json.loads(fields["data"])
            assert event["event_id"] == fields["id"]
            assert event["event_type"] == fields["event"]
            result.append(event)
    return result


async def no_subscribers(streams):
    async with asyncio.timeout(3):
        while streams.subscriber_count(TASK_ID):
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_network_disconnect_concurrent_publish_and_exclusive_reconnect(tmp_path):
    with serving_events(tmp_path) as (url, store, streams):
        path = f"/api/v2/missions/{TASK_ID}/events"
        async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
            received = []
            async with client.stream("GET", path) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = response.aiter_lines()
                assert await next_block(lines) == "retry: 1000\n\n"
                while len(received) < 2:
                    received.extend(event_frames(await next_block(lines)))
            await no_subscribers(streams)
            cursor = received[-1]["event_id"]

            def append_many(writer):
                for index in range(45):
                    publish_mission_event(store, TASK_ID, "tool.completed", {"writer": writer, "index": index})
            await asyncio.gather(*(asyncio.to_thread(append_many, writer) for writer in range(3)))
            finish_mission(store)
            # Independent subscribers see exactly the same committed suffix.
            replies = await asyncio.gather(*(client.get(path, headers={"Last-Event-ID": cursor}) for _ in range(3)))
            tails = [event_frames(response.text) for response in replies]
            assert tails[0] == tails[1] == tails[2]
            received.extend(tails[0])
            assert [event["event_id"] for event in received] == [f"{TASK_ID}:{index}" for index in range(1, 139)]
            assert [event["task_revision"] for event in received] == list(range(1, 139))
            assert received[-1]["event_type"] == "task.completed"
            acknowledged = await client.get(path, headers={"Last-Event-ID": received[-1]["event_id"]})
            assert acknowledged.status_code == 204
            assert (await client.get(path, headers={"Last-Event-ID": "wrong:1"})).status_code == 400
            assert (await client.get(path, headers={"Last-Event-ID": TASK_ID + ":999"})).status_code == 409
            assert (await client.get(path, headers=[("Last-Event-ID", cursor), ("Last-Event-ID", cursor)])).status_code == 400
            await no_subscribers(streams)


@pytest.mark.asyncio
async def test_network_slow_consumer_is_bounded_and_disconnect_releases_capacity(tmp_path):
    with serving_events(tmp_path, page_size=3, max_subscribers=1) as (url, store, streams):
        path = f"/api/v2/missions/{TASK_ID}/events"
        async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
            async with client.stream("GET", path) as first:
                assert first.status_code == 200
                assert streams.subscriber_count(TASK_ID) == 1
                for index in range(50):
                    publish_mission_event(store, TASK_ID, "model.completed", {"index": index})
                denied = await client.get(path)
                assert denied.status_code == 429
                assert streams.page_size == 3
                # There is no growing in-memory event queue behind the slow socket.
                assert not hasattr(streams, "_queue") and not hasattr(streams, "_events")
            await no_subscribers(streams)
            finish_mission(store)
            replay = await client.get(path)
            assert len(event_frames(replay.text)) == 53
            await no_subscribers(streams)
