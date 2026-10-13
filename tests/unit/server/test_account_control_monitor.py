"""流控监控的数据库交接、异常边界和广播验证。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.executor.account_control.guard import resolve_control_window
from axile.executor.account_control.models import AccountControlBucketType, AccountControlScopeType
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


def executor(guard):
    return SimpleNamespace(get_account_control_guard=lambda: guard, export_account_control_records=guard.flush_records)


def test_handoff_rebinding_and_late_results_never_double_count(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, monitor):
            first = await persistence.build_account_control_guard(account, "same-execution")
            first.policy.operations["place_order"].account.min_interval_ms = None
            for _ in range(3):
                first.begin_operation("place_order").record_outcome("submitted")
            await monitor.refresh()
            assert used(monitor.frames[1]) == 123
            await persistence.flush_account_control_records(executor(first))
            await monitor.refresh()
            assert used(monitor.frames[1]) == 123
            assert monitor.frames[1]["status"] == "idle"
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
