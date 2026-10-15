"""统一 SSE 的快照竞争、故障隔离、背压和连接清理。"""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI

from axile.server import event_stream
from axile.server.account_control.monitor import AccountControlMonitor
from axile.server.api.routes.events import router
from axile.server.execution.live import ExecutionLiveHub


def sources(monkeypatch, *, sampled=True):
    hub = ExecutionLiveHub()
    hub.bind_loop(asyncio.get_running_loop())
    monitor = AccountControlMonitor()
    monkeypatch.setattr(monitor, "start", lambda: None)
    monkeypatch.setattr(monitor, "refresh", AsyncMock())
    if sampled:
        monitor.frames = {1: {"account_id": 1, "sampled_at_ms": 1000, "stale": False}}
        monitor._publish([], [])
    monkeypatch.setattr(event_stream, "live_hub", hub)
    monkeypatch.setattr(event_stream, "monitor", monitor)
    from axile.server.connection_events import ConnectionEventHub

    monkeypatch.setattr(event_stream, "connection_hub", ConnectionEventHub())
    monkeypatch.setattr(event_stream, "connection_frames", AsyncMock(return_value=[]))
    return hub, monitor


async def next_event(events, timeout=1):
    event = await asyncio.wait_for(anext(events), timeout)
    assert event is not None
    return event


def publish(hub, account_id=1, event="execution_started", execution_id="run"):
    hub.publish(account_id=account_id, execution_id=execution_id, event_type=event)


def test_multiplexes_independent_snapshots_and_updates_without_per_connection_sampling(monkeypatch):
    async def scenario():
        hub, monitor = sources(monkeypatch)
        async with aclosing(event_stream.merge_events(("execution", "control"))) as events:
            initial = {event.name: event.data for event in [await next_event(events), await next_event(events)]}
            assert initial["execution.snapshot"]["data"] == []
            assert initial["control.snapshot"]["data"][0]["account_id"] == 1
            publish(hub)
            monitor.frames = {}
            monitor._publish([], [1])
            updates = {event.name: event.data for event in [await next_event(events), await next_event(events)]}
            assert updates["execution.event"]["execution_id"] == "run"
            assert updates["control.update"]["removed"] == [1]
            assert updates["control.update"]["stream_id"] == monitor.stream_id
            assert updates["control.update"]["version"] == monitor.version
            assert updates["control.update"]["freshness"] == []
            monitor.refresh.assert_not_called()
        assert not hub._subscribers
        assert not monitor.subscribers

    asyncio.run(scenario())


def test_slow_or_failed_initial_control_sample_does_not_block_execution_and_recovers_full(monkeypatch):
    async def scenario():
        hub, monitor = sources(monkeypatch, sampled=False)
        monkeypatch.setattr(event_stream, "_CONTROL_TIMEOUT_SECONDS", 0.05)
        async with aclosing(event_stream.merge_events(("execution", "control"))) as events:
            assert (await next_event(events)).name == "execution.snapshot"
            publish(hub)
            assert (await next_event(events)).name == "execution.event"
            assert (await next_event(events)).name == "control.error"
            monitor.frames = {2: {"account_id": 2, "sampled_at_ms": 2000, "stale": False}}
            monitor._publish(list(monitor.frames.values()), [])
            recovered = await next_event(events)
            assert recovered.name == "control.snapshot"
            assert recovered.data["data"][0]["account_id"] == 2

    asyncio.run(scenario())


def test_control_silence_keeps_execution_running_and_recovery_starts_with_snapshot(monkeypatch):
    async def scenario():
        hub, monitor = sources(monkeypatch)
        monkeypatch.setattr(event_stream, "_CONTROL_TIMEOUT_SECONDS", 0.05)
        async with aclosing(event_stream.merge_events(("execution", "control"))) as events:
            await next_event(events)
            await next_event(events)
            assert (await next_event(events)).name == "control.error"
            publish(hub)
            assert (await next_event(events)).name == "execution.event"
            monitor._publish([], [])
            recovered = await next_event(events)
            assert recovered.name == "control.snapshot"
            assert recovered.data["version"] == monitor.version

    asyncio.run(scenario())


def test_initial_snapshot_watermark_skips_pre_snapshot_delayed_thread_delivery(monkeypatch):
    async def scenario():
        hub, _ = sources(monkeypatch)
        publish(hub, event="target_computed")
        async with aclosing(event_stream.execution_events()) as events:
            snapshot = await next_event(events)
            assert snapshot.data["version"] == 1
            assert snapshot.data["data"][0]["phase"] == "planning"
            publish(hub, event="order_submitted")
            update = await next_event(events)
            assert update.name == "execution.event"
            assert update.data["version"] == 2
            assert update.data["phase"] == "executing"

    asyncio.run(scenario())


def test_update_between_snapshot_read_and_first_yield_is_not_lost(monkeypatch):
    async def scenario():
        hub, _ = sources(monkeypatch)
        original = hub.snapshot_envelope

        def snapshot_with_concurrent_publish():
            snapshot = original()
            publish(hub, event="target_computed")
            return snapshot

        monkeypatch.setattr(hub, "snapshot_envelope", snapshot_with_concurrent_publish)
        async with aclosing(event_stream.execution_events()) as events:
            assert (await next_event(events)).data["version"] == 0
            update = await next_event(events)
            assert update.data["version"] == 1
            assert update.data["phase"] == "planning"

    asyncio.run(scenario())


def test_execution_queue_overflow_resyncs_all_accounts_and_terminal_state(monkeypatch):
    async def scenario():
        from axile.server.execution import live

        monkeypatch.setattr(live, "_QUEUE_MAXSIZE", 2)
        hub, _ = sources(monkeypatch)
        async with aclosing(event_stream.execution_events()) as events:
            await next_event(events)
            publish(hub, account_id=1)
            publish(hub, account_id=2)
            publish(hub, account_id=1, event="execution_completed")
            resync = await next_event(events)
            assert resync.name == "execution.snapshot"
            assert resync.data["version"] == 3
            assert [frame["account_id"] for frame in resync.data["data"]] == [2]

    asyncio.run(scenario())


def test_threaded_publish_preserves_version_order(monkeypatch):
    async def scenario():
        hub, _ = sources(monkeypatch)
        async with hub.subscribe() as queue:
            await asyncio.gather(*(asyncio.to_thread(publish, hub, account_id) for account_id in range(50)))
            frames = [await asyncio.wait_for(queue.get(), 1) for _ in range(50)]
            assert [frame["version"] for frame in frames] == list(range(1, 51))
            assert len(hub.snapshot_envelope()["data"]) == 50

    asyncio.run(scenario())


def test_late_terminal_does_not_remove_next_execution_from_reconnect_snapshot(monkeypatch):
    async def scenario():
        hub, _ = sources(monkeypatch)
        publish(hub, execution_id="previous")
        publish(hub, execution_id="next")
        publish(hub, execution_id="previous", event="execution_completed")
        snapshot = hub.snapshot_envelope()
        assert snapshot["data"][0]["execution_id"] == "next"
        assert snapshot["data"][0]["status"] == "running"

    asyncio.run(scenario())


def test_one_domain_exception_is_reported_without_closing_other_domain(monkeypatch):
    async def fail():
        raise RuntimeError("fixture sampler unavailable")
        yield  # noqa: B018

    async def scenario():
        hub, _ = sources(monkeypatch)
        monkeypatch.setattr(event_stream, "control_events", fail)
        async with aclosing(event_stream.merge_events(("execution", "control"))) as events:
            received = [await next_event(events), await next_event(events)]
            assert {event.name for event in received} == {"execution.snapshot", "control.error"}
            error = next(event for event in received if event.name == "control.error")
            assert error.data["message"] == "fixture sampler unavailable"
            publish(hub)
            assert (await next_event(events)).name == "execution.event"
        assert not hub._subscribers

    asyncio.run(scenario())


def test_heartbeat_and_empty_snapshots(monkeypatch):
    async def scenario():
        _, monitor = sources(monkeypatch)
        monitor.frames = {}
        async with aclosing(event_stream.merge_events(("execution", "control"), heartbeat_seconds=0.01)) as events:
            snapshots = [await next_event(events), await next_event(events)]
            assert all(event.data["data"] == [] for event in snapshots)
            assert await asyncio.wait_for(anext(events), 1) is None
            assert event_stream._encode(None) == ": ping\n\n"

    asyncio.run(scenario())


def test_closing_slow_connection_cleans_up_blocked_forwarders_and_all_subscriptions(monkeypatch):
    async def scenario():
        hub, monitor = sources(monkeypatch)
        monkeypatch.setattr(event_stream, "_QUEUE_SIZE", 1)
        async with aclosing(event_stream.merge_events(("execution", "control"))) as events:
            await next_event(events)
            await next_event(events)
            for index in range(100):
                publish(hub, index)
                monitor._publish([], [])
                await asyncio.sleep(0)
        assert not hub._subscribers
        assert not monitor.subscribers
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

    asyncio.run(scenario())


def test_response_headers_encoding_and_disconnect_cleanup(monkeypatch):
    async def scenario():
        hub, monitor = sources(monkeypatch)
        disconnected = False

        async def is_disconnected():
            return disconnected

        response = event_stream.stream_response(SimpleNamespace(is_disconnected=is_disconnected))
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        async with aclosing(response.body_iterator) as frames:
            first = await asyncio.wait_for(anext(frames), 1)
            assert first.startswith("event: execution.snapshot\n")
            assert json.loads(first.split("data: ")[1])["data"] == []
            disconnected = True
            assert await anext(frames, None) is None
        assert not hub._subscribers
        assert not monitor.subscribers

    asyncio.run(scenario())


def test_only_unified_stream_is_registered():
    from axile.server.api.main import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    paths = app.openapi()["paths"]
    assert "/api/v1/events/stream" in paths
    assert "/api/v1/account/executions/stream" not in paths
    assert "/api/v1/account/control/stream" not in paths
    assert [route.path for route in router.routes] == ["/events/stream"]


def test_default_stream_includes_connection_snapshot_without_periodic_reads(monkeypatch):
    async def scenario():
        sources(monkeypatch)
        async with aclosing(event_stream.merge_events()) as events:
            initial = [await next_event(events) for _ in range(3)]
            assert {event.name for event in initial} == {
                "execution.snapshot",
                "control.snapshot",
                "connection.snapshot",
            }
            assert event_stream.connection_frames.await_count == 1
            event_stream.connection_hub.changed(7)
            update = await next_event(events)
            assert update.name == "connection.update"
            assert update.data["removed"] == [7]
            assert event_stream.connection_frames.await_count == 2
        assert not event_stream.connection_hub._subscribers

    asyncio.run(scenario())
