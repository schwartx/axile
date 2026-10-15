"""流控监控的数据库交接、异常边界和广播验证。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.executor.account_control.guard import resolve_control_window
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlContextKind,
    AccountControlScopeType,
    AccountControlSource,
)
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import monitor as monitor_module
from axile.server.account_control import providers
from axile.server.db.models import Account, AccountControlCounterDelta, AccountControlEvent
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.execution import execution_account_control as persistence
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(monitor_module, "SessionLocal", factory)
    monkeypatch.setattr(persistence, "SessionLocal", factory)
    monkeypatch.setattr(monitor_module, "sample", lambda _: None)
    monkeypatch.setattr(providers, "_guards", {})
    monkeypatch.setattr(providers, "_final", {})
    monkeypatch.setattr(providers, "_updates", {})
    account = build_account(id=1, portfolio_id=None, account_control_preset="default", account_control_override=None)
    async with factory() as db:
        db.add(account)
        window = resolve_control_window("Asia/Shanghai")
        db.add(
            AccountControlCounterDelta(
                account_id=1,
                execution_id="history",
                control_date=window.control_date,
                bucket_type=AccountControlBucketType.DAY,
                bucket_start=window.day_bucket_start,
                scope_type=AccountControlScopeType.ACCOUNT,
                operation="place_order",
                delta_count=120,
                delta_uid="baseline",
            )
        )
        await db.commit()
    try:
        yield factory, account, monitor_module.AccountControlMonitor()
    finally:
        await engine.dispose()


def used(frame):
    return next(m["value"] for m in frame["metrics"] if m["operation"] == "place_order")


def test_reconnecting_to_expired_cache_does_not_make_monitor_values_fresh(monkeypatch):
    now = [10000]
    monkeypatch.setattr(monitor_module, "clock_now_ms", lambda: now[0])
    monitor = monitor_module.AccountControlMonitor()
    monitor.frames = {1: {"account_id": 1, "sampled_at_ms": 10000, "stale": False}}
    monitor._publish([], [])
    assert monitor.snapshot()["data"][0]["stale"] is False
    now[0] = 15001
    assert monitor.snapshot()["data"][0]["stale"] is True
    assert monitor.frames[1]["stale"] is False
    monitor._publish([], [])
    assert monitor.snapshot()["data"][0]["stale"] is False


def test_snapshot_cannot_observe_half_completed_sampling_round(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, _, monitor):
            await monitor.refresh()
            before = monitor.snapshot()
            async with factory() as db:
                db.add(build_account(id=2, portfolio_id=None))
                await db.commit()
            entered = asyncio.Event()
            release = asyncio.Event()
            original = monitor._account_frame

            async def delayed_frame(db, account, sessions):
                if account.id == 2:
                    entered.set()
                    await release.wait()
                frame = await original(db, account, sessions)
                frame["waiting_total"] = 99
                return frame

            monkeypatch.setattr(monitor, "_account_frame", delayed_frame)
            task = asyncio.create_task(monitor.refresh())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert monitor.snapshot() == before
            finally:
                release.set()
                await task
            after = monitor.snapshot()
            assert after["version"] == before["version"] + 1
            assert len(after["data"]) == 2
            assert all(frame["waiting_total"] == 99 for frame in after["data"])

    asyncio.run(scenario())


def executor(guard):
    return SimpleNamespace(get_account_control_guard=lambda: guard, export_account_control_records=guard.flush_records)


@pytest.mark.parametrize("kind", [AccountControlContextKind.ACCOUNT_OPERATION, AccountControlContextKind.UNKNOWN])
def test_catalog_session_does_not_hide_running_execution_or_report_duplicate_execution(monkeypatch, kind):
    async def scenario():
        async with database(monkeypatch) as (_, account, monitor):
            active = await persistence.build_account_control_guard(account, "active-execution")
            active.begin_operation("place_order").record_outcome("submitted")
            catalog = await persistence.build_account_control_guard(
                account,
                "catalog-" + "a" * 32,
                register_monitor=False,
                context_kind=kind,
                source=AccountControlSource.INSTRUMENT_CATALOG,
            )

            async def source(account_id):
                monitor._sources[account_id] = [(providers.snapshot(account_id), clock_now_ms(), False)]

            monkeypatch.setattr(monitor, "_read_source", source)

            def assert_running():
                frame = monitor.frames[1]
                assert frame["status"] == "running" and frame["instance_id"] == active.instance_id
                assert frame["execution_id"] == "active-execution"
                assert used(frame) == 121
                assert not frame["stale"] and not frame["incomplete"]
                assert frame["actual_fingerprint"] is not None

            await monitor.refresh()
            assert_running()
            catalog.begin_operation("query_instruments").record_outcome("fetched")
            await persistence.flush_account_control_records(executor(catalog))
            await monitor.refresh()
            assert_running()
            await persistence.flush_account_control_records(executor(active))
            await monitor.refresh()
            assert monitor.frames[1]["status"] == "idle"
            assert used(monitor.frames[1]) == 121

    asyncio.run(scenario())


def test_handoff_rebinding_and_late_results_never_double_count(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, monitor):
            first = await persistence.build_account_control_guard(account, "same-execution")
            first.policy.operations["place_order"].account.min_interval_ms = None
            for _ in range(3):
                first.begin_operation("place_order").record_outcome("submitted")
            # 采样有 200ms 预算，CI 的首个线程任务可能尚未调度完成。
            # 等待对应实例的首帧；计数仍单独断言，不能用目标计数作为等待条件。
            async with asyncio.timeout(3):
                while monitor.frames.get(1, {}).get("instance_id") != first.instance_id:
                    await monitor.refresh()
                    await asyncio.sleep(0)
            assert used(monitor.frames[1]) == 123
            await persistence.flush_account_control_records(executor(first))
            await monitor.refresh()
            assert used(monitor.frames[1]) == 123
            assert monitor.frames[1]["status"] == "idle"
            assert len(monitor.frames[1]["records"]) == 3
            assert all(event["outcome"] == "submitted" for event in monitor.frames[1]["records"])
            restarted = monitor_module.AccountControlMonitor()
            await restarted.refresh()
            assert restarted.frames[1]["records"] == monitor.frames[1]["records"]
            second = await persistence.build_account_control_guard(account, "same-execution")
            await monitor.refresh()
            assert used(monitor.frames[1]) == 123
            second.begin_operation("place_order").record_outcome("submitted")
            await persistence.flush_account_control_records(executor(second))
            first.update_event_outcome(0, outcome="broker_rejected", metadata={"error_id": 31})
            await monitor.refresh()
            assert used(monitor.frames[1]) == 124
            async with factory() as db:
                events = (await db.execute(select(AccountControlEvent))).scalars().all()
                assert len(events) == 4
                assert any(e.outcome == "broker_rejected" for e in events)
                sessions = (await db.execute(select(AccountControlSession))).scalars().all()
                assert all(s.status == "closed" for s in sessions)
            assert len(monitor.frames[1]["records"]) == 4
            assert any(e["outcome"] == "broker_rejected" for e in monitor.frames[1]["records"])

    asyncio.run(scenario())


def test_idle_interval_is_recomputed_without_reloading_history(monkeypatch):
    from axile.executor.algorithms.utils import clock as clock_module

    now = [1800000010.0]
    monkeypatch.setattr(clock_module, "_default_clock", SimpleNamespace(time=lambda: now[0], sleep=lambda _: None))

    async def scenario():
        async with database(monkeypatch) as (_, account, monitor):
            guard = await persistence.build_account_control_guard(account, "interval")
            guard.begin_operation("place_order").record_outcome("submitted")
            await persistence.flush_account_control_records(executor(guard))
            await monitor.refresh()

            def interval():
                return next(r for r in monitor.frames[1]["rules"] if r["id"] == "account:place_order:min_interval_ms")

            assert interval()["estimated_wait_ms"] == 300
            cached = monitor._history[1]
            now[0] += 0.6
            await monitor.refresh()
            assert monitor._history[1] is cached
            assert interval()["value"] == 600
            assert interval()["estimated_wait_ms"] == 0
            assert len(monitor.frames[1]["records"]) == 1

    asyncio.run(scenario())


def test_crash_preserves_last_known_value_and_restart_marks_confirmed_history_incomplete(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (_, account, monitor):
            guard = await persistence.build_account_control_guard(account, "crash")
            guard.begin_operation("place_order").record_outcome("submitted")
            await monitor.refresh()
            assert used(monitor.frames[1]) == 121
            providers._guards.clear()
            await monitor_module.recover_sessions()
            await monitor.refresh()
            assert used(monitor.frames[1]) == 121
            assert monitor.frames[1]["incomplete"]
            assert monitor.frames[1]["stale"]
            restarted = monitor_module.AccountControlMonitor()
            await restarted.refresh()
            assert used(restarted.frames[1]) == 120
            assert restarted.frames[1]["incomplete"]
            next_guard = await persistence.build_account_control_guard(account, "after-crash")
            next_guard.begin_operation("place_order").record_outcome("submitted")
            await restarted.refresh()
            assert restarted.frames[1]["incomplete"]

    asyncio.run(scenario())


def test_saved_policy_does_not_replace_active_policy(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, monitor):
            await persistence.build_account_control_guard(account, "old-policy")
            await monitor.refresh()
            old = monitor.frames[1]["actual_fingerprint"]
            async with factory() as db:
                saved = await db.get(Account, 1)
                saved.account_control_override = {
                    "operations": {"place_order": {"account": {"per_day": {"limit": 700}}}}
                }
                db.add(saved)
                await db.commit()
            await monitor.refresh()
            assert monitor.frames[1]["actual_fingerprint"] == old
            assert monitor.frames[1]["pending_policy"]
            assert monitor.frames[1]["saved_fingerprint"] != old

    asyncio.run(scenario())


def test_slow_subscriber_receives_full_resync_instead_of_losing_account_updates():
    async def scenario():
        monitor = monitor_module.AccountControlMonitor()
        monitor.frames = {1: {"account_id": 1, "sampled_at_ms": 1000, "stale": False}}
        async with monitor.subscribe() as queue:
            for _ in range(17):
                monitor._publish([], [])
            frame = await queue.get()
            assert frame["kind"] == "snapshot"
            assert frame["data"][0]["account_id"] == 1
            assert frame["version"] == monitor.version

    asyncio.run(scenario())


def test_failed_flush_preserves_last_value_and_marks_persistent_gap(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (_, account, monitor):
            guard = await persistence.build_account_control_guard(account, "flush-failure")
            guard.begin_operation("place_order").record_outcome("submitted")
            await monitor.refresh()
            original = persistence.AccountControlStore.flush_execution_records

            async def fail(_self, **_kwargs):
                raise RuntimeError("offline write failure")

            monkeypatch.setattr(persistence.AccountControlStore, "flush_execution_records", fail)
            import pytest

            with pytest.raises(RuntimeError, match="offline write failure"):
                await persistence.flush_account_control_records(executor(guard))
            monkeypatch.setattr(persistence.AccountControlStore, "flush_execution_records", original)
            await monitor.refresh()
            assert used(monitor.frames[1]) == 121
            assert monitor.frames[1]["incomplete"]
            assert monitor.frames[1]["actual_policy"] is None
            restarted = monitor_module.AccountControlMonitor()
            await restarted.refresh()
            assert used(restarted.frames[1]) == 121  # 同进程保留的最终快照仍可读取。
            assert restarted.frames[1]["incomplete"]

    asyncio.run(scenario())


def test_retired_worker_snapshot_cannot_replace_new_guard(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (_, account, monitor):
            first = await persistence.build_account_control_guard(account, "old")
            old_frame = first.monitor_snapshot()
            first.begin_operation("place_order").record_outcome("submitted")
            await persistence.flush_account_control_records(executor(first))
            await monitor.refresh()
            second = await persistence.build_account_control_guard(account, "new")
            providers._guards.clear()  # 模拟新 worker 尚未提供第一帧。
            providers._final.clear()
            monkeypatch.setattr(monitor_module, "sample", lambda _: (old_frame, old_frame["sampled_at_ms"], False))
            await monitor.refresh()
            assert used(monitor.frames[1]) == 121
            assert monitor.frames[1]["status"] == "unknown"
            current = second.monitor_snapshot()
            monkeypatch.setattr(monitor_module, "sample", lambda _: (current, current["sampled_at_ms"], False))
            await monitor.refresh()
            assert used(monitor.frames[1]) == 121
            assert monitor.frames[1]["instance_id"] == second.instance_id

    asyncio.run(scenario())
