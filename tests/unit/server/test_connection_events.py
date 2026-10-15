"""连接订阅只消费变更；覆盖快照竞争、背压、重连和过期通知。"""

import asyncio
from contextlib import aclosing
from unittest.mock import AsyncMock

from axile.server import connection_runtime, event_stream
from axile.server.connection_events import ConnectionEventHub


def setup_hub(monkeypatch):
    hub = ConnectionEventHub()
    monkeypatch.setattr(event_stream, "connection_hub", hub)
    monkeypatch.setattr(connection_runtime, "session_deadlines", lambda: {})
    return hub


def test_snapshot_race_and_coalesced_updates_keep_all_accounts(monkeypatch):
    async def scenario():
        hub = setup_hub(monkeypatch)
        values = {1: {"account_id": 1, "connection": {"status": "ready"}}}

        async def frames(ids=None):
            result = [frame for key, frame in values.items() if ids is None or key in ids]
            if ids is None:
                values[2] = {"account_id": 2, "connection": {"status": "connecting"}}
                hub.changed(2)
            return result

        monkeypatch.setattr(event_stream, "connection_frames", frames)
        async with aclosing(event_stream.connection_events()) as events:
            initial = await anext(events)
            assert initial.name == "connection.snapshot"
            assert initial.data["version"] == 0
            assert [frame["account_id"] for frame in initial.data["data"]] == [1]
            added = await asyncio.wait_for(anext(events), 1)
            assert added.data["data"] == [values[2]]
            hub.changed(1)
            hub.changed(2)
            update = await asyncio.wait_for(anext(events), 1)
            assert update.data["version"] == 3
            assert {frame["account_id"] for frame in update.data["data"]} == {1, 2}
        assert not hub._subscribers
        assert not hub._deadlines

    asyncio.run(scenario())


def test_removal_and_reconnect_use_authoritative_full_snapshot(monkeypatch):
    async def scenario():
        hub = setup_hub(monkeypatch)
        read = AsyncMock(return_value=[{"account_id": 1, "connection": {"status": "ready"}}])
        monkeypatch.setattr(event_stream, "connection_frames", read)
        async with aclosing(event_stream.connection_events()) as events:
            await anext(events)
            read.return_value = []
            hub.changed(1)
            removed = await asyncio.wait_for(anext(events), 1)
            assert removed.data["removed"] == [1]
            assert removed.data["data"] == []
        read.return_value = [{"account_id": 2, "connection": {"status": "uninitialized"}}]
        async with aclosing(event_stream.connection_events()) as events:
            snapshot = await anext(events)
            assert snapshot.name == "connection.snapshot"
            assert snapshot.data["data"] == read.return_value
        assert read.call_count == 3

    asyncio.run(scenario())


def test_sampling_only_rearms_one_shot_expiry_without_reading_or_publishing(monkeypatch):
    async def scenario():
        hub = setup_hub(monkeypatch)
        async with hub.subscribe() as queue:
            hub.sampled(1, 60)
            await asyncio.sleep(0)
            first = hub._deadlines[1]
            hub.sampled(1, 120)
            await asyncio.sleep(0)
            assert first.cancelled()
            assert hub.version == 0
            assert queue.empty()
            assert len(hub._deadlines) == 1
            hub._expired(1)
            version, ids = await asyncio.wait_for(queue.get(), 1)
            assert version == 1
            assert ids == {1}
        assert not hub._deadlines

    asyncio.run(scenario())
