"""流控监控的数据库交接、异常边界和广播验证。"""

import asyncio
import json
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import resolve_control_window
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlContextKind,
    AccountControlSource,
)
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import monitor as monitor_module
from axile.server.account_control import providers
from axile.server.execution import execution_account_control as persistence
from tests.unit.server._execution_test_support import account_settings_values, build_account


def save_account(path, account):
    """测试 oracle 直接保存账户配置，生产路径不调用 SQLite。"""
    groups = account_settings_values(account)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT OR REPLACE INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                account.id,
                account.name,
                account.market,
                str(account.trade_channel),
                account.brokerage,
                account.created_at,
                account.updated_at,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO account_settings(account_id,connection,execution,schedule,notification,backtest) "
            "VALUES (?,?,?,?,?,?)",
            (account.id, *(json.dumps(value) for value in groups.values())),
        )


@asynccontextmanager
async def database(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "monitor.sqlite"
        schema = (Path(__file__).parents[2] / "fixtures/rust_store/v0.22.0.sql").read_text()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript("\n".join(re.findall(r"CREATE TABLE .*?;", schema, re.DOTALL)))
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
        monkeypatch.setattr(monitor_module, "sample", lambda _: None)
        monkeypatch.setattr(monitor_module, "wake_pending_quota_warnings", _ignore_warnings)
        for name in ("_guards", "_final", "_updates"):
            monkeypatch.setattr(providers, name, {})
        account = build_account(
            id=1, portfolio_id=None, account_control_preset="default", account_control_override=None
        )
        save_account(path, account)
        window = resolve_control_window("Asia/Shanghai")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO account_control_counter_delta(account_id,execution_id,control_date,bucket_type,"
                "bucket_start,scope_type,operation,delta_count,delta_uid) VALUES (1,'history',?,'DAY',?,'ACCOUNT',"
                "'place_order',120,'baseline')",
                (window.control_date, window.day_bucket_start),
            )
        yield path, account, monitor_module.AccountControlMonitor()


async def _ignore_warnings(_account_ids):
    pass


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
        async with database(monkeypatch) as (path, _, monitor):
            await monitor.refresh()
            before = monitor.snapshot()
            save_account(path, build_account(id=2, portfolio_id=None))
            entered = asyncio.Event()
            release = asyncio.Event()
            original = monitor._account_frame

            async def delayed_frame(account, sessions, persisted):
                if account["id"] == 2:
                    entered.set()
                    await release.wait()
                frame = await original(account, sessions, persisted)
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
        async with database(monkeypatch) as (path, account, monitor):
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
            with closing(sqlite3.connect(path)) as connection, connection:
                events = connection.execute("SELECT outcome FROM account_control_event").fetchall()
                assert len(events) == 4
                assert ("broker_rejected",) in events
                assert (
                    connection.execute(
                        "SELECT count(*) FROM account_control_session WHERE status!='closed'"
                    ).fetchone()[0]
                    == 0
                )
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
            assert restarted.frames[1]["history_incomplete"]
            assert not restarted.frames[1]["snapshot_stale"]
            assert restarted.frames[1]["counter_integrity"] == "unknown"
            assert not restarted.frames[1]["risk_operations_blocked"]
            assert restarted.frames[1]["counter_recovery"] == "historical_gap_retained"
            assert restarted.frames[1]["incomplete_instances_total"] == 1
            next_guard = await persistence.build_account_control_guard(account, "after-crash")
            next_guard.begin_operation("place_order").record_outcome("submitted")
            next_guard.begin_operation("query_order").record_outcome("fetched")
            next_guard.begin_operation("cancel_order").record_outcome("submitted")
            await restarted.refresh()
            assert restarted.frames[1]["incomplete"]

    asyncio.run(scenario())


def test_saved_policy_does_not_replace_active_policy(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, monitor):
            await persistence.build_account_control_guard(account, "old-policy")
            await monitor.refresh()
            old = monitor.frames[1]["actual_fingerprint"]
            changed = account.model_copy(deep=True)
            from axile.executor.account_control.models import AccountControlOverride

            changed.account_control_override = AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"per_day": {"limit": 700}}}}}
            )
            save_account(path, changed)
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


@pytest.mark.parametrize("status", ["active", "incomplete"])
@pytest.mark.parametrize("outcome", ["allowed", "submitted"])
def test_orphan_session_warns_without_blocking_and_known_quota_still_applies(monkeypatch, status, outcome):
    """中断缺口保留提示；重启可报单，已知计数达到限额后仍阻断。"""
    from axile.executor.account_control.models import AccountControlOverride

    async def scenario():
        async with database(monkeypatch) as (path, account, _):
            account.account_control_override = AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"per_day": {"limit": 1, "on_trigger": "block"}}}}}
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DELETE FROM account_control_counter_delta")
            first = await persistence.build_account_control_guard(account, "lost")
            first.begin_operation("place_order").record_outcome(outcome)  # 模拟发送前／受理后未刷盘被杀。
            providers._guards.clear()
            if status == "incomplete":
                await monitor_module.recover_sessions()
            second = await persistence.build_account_control_guard(account, "restart")
            native_calls = []
            second.begin_operation("place_order").record_outcome("submitted")
            native_calls.append("new_order")
            assert native_calls == ["new_order"]
            assert second.monitor_incomplete
            assert not second.monitor_snapshot()["risk_operations_blocked"]
            with pytest.raises(AccountControlBlockedError):
                second.begin_operation("place_order")
            for operation in ("query_order", "cancel_order", "cancel_order_ctp"):
                second.begin_operation(operation)
            for operation in ("option_exercise", "option_abandon", "option_self_close"):
                second.begin_operation(operation)
            deltas, events = second.flush_records()
            assert not any(event.outcome == "counter_incomplete" for event in events)
            assert any(delta.operation == "place_order" for delta in deltas)
            with closing(sqlite3.connect(path)) as connection, connection:
                assert (
                    connection.execute(
                        "SELECT coalesce(sum(delta_count),0) FROM account_control_counter_delta"
                    ).fetchone()[0]
                    == 0
                )

    asyncio.run(scenario())


def test_prior_day_gap_does_not_permanently_block_and_known_active_guard_is_not_an_orphan(monkeypatch):
    from datetime import timedelta

    from axile.executor.algorithms.utils.clock import clock_now

    async def scenario():
        async with database(monkeypatch) as (path, account, _):
            yesterday = int((clock_now() - timedelta(days=1)).timestamp() * 1000)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "INSERT INTO account_control_session(instance_id,account_id,execution_id,context_kind,source,"
                    "timezone,started_at_ms,finished_at_ms,status) "
                    "VALUES ('yesterday',1,'old','execution','algorithm','Asia/Shanghai',?,?,'incomplete')",
                    (yesterday - 1000, yesterday),
                )
            first = await persistence.build_account_control_guard(account, "today")
            assert not first.incomplete_control_dates
            second = await persistence.build_account_control_guard(account, "known-active")
            assert not second.incomplete_control_dates
            second.begin_operation("place_order")

    asyncio.run(scenario())


def test_commit_response_loss_and_duplicate_flush_do_not_restore_daily_quota(monkeypatch):
    from axile.executor.account_control.models import AccountControlOverride
    from axile.server.account_control.store import AccountControlStore

    async def scenario():
        async with database(monkeypatch) as (path, account, _):
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DELETE FROM account_control_counter_delta")
            account.account_control_override = AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"per_day": {"limit": 1, "on_trigger": "block"}}}}}
            )
            first = await persistence.build_account_control_guard(account, "committed")
            first.begin_operation("place_order").record_outcome("submitted")
            deltas, events = first.flush_records()
            await persistence.flush_account_control_records(executor(first))
            # 模拟提交成功但响应丢失后的同一记录重试；delta/event 唯一标识防止重复累计。
            await AccountControlStore().flush_execution_records(counter_deltas=deltas, events=events)
            providers._guards.clear()
            second = await persistence.build_account_control_guard(account, "retry")
            assert not second.incomplete_control_dates
            with pytest.raises(AccountControlBlockedError) as caught:
                second.begin_operation("place_order")
            assert caught.value.reason_code != "CONTROL.COUNTER_INCOMPLETE"
            assert (
                second.baseline.get_count(
                    bucket_type=AccountControlBucketType.DAY,
                    bucket_start=resolve_control_window("Asia/Shanghai").day_bucket_start,
                    operation="place_order",
                )
                == 1
            )

    asyncio.run(scenario())
