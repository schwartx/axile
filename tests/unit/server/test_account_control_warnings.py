"""额度预警的实时入队、短执行、去重、系统发送和重启边界。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, delete, select

from axile.common.config import settings
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server import error_notifications
from axile.server.account_control import monitor as monitor_module
from axile.server.account_control import providers, warnings
from axile.server.db.models.account import Account
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.account_notification import AccountNotificationState
from axile.server.db.models.supplement import NotificationEvent
from axile.server.execution import execution_account_control as persistence
from axile.server.execution import notification_outbox, notification_state
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (monitor_module, persistence, notification_outbox):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(providers, "_guards", {})
    monkeypatch.setattr(providers, "_final", {})
    monkeypatch.setattr(providers, "_updates", {})
    monkeypatch.setattr(notification_outbox, "_tasks", {})
    monkeypatch.setattr(notification_outbox, "_rerun", set())
    wakes, results, cards = [], [], []
    monkeypatch.setattr(notification_outbox, "wake_notifications", wakes.append)
    monkeypatch.setattr(error_notifications, "record_system_notification_result", results.append)
    monkeypatch.setattr(notification_state, "record_system_notification_result", results.append)
    monkeypatch.setattr(error_notifications, "push_feishu_card", lambda card, key: cards.append((card, key)))
    monkeypatch.setattr(settings, "system_execution_notification_mode", "default")
    monkeypatch.setattr(settings, "exe_err_feishu_key", "system-test-hook")
    account = build_account(
        id=1,
        portfolio_id=None,
        account_control_preset="default",
        account_control_override=AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "daily_warning_percent": 80,
                        "account": {
                            "per_day": {"limit": 5, "on_trigger": "block"},
                            "min_interval_ms": {"limit": 1, "on_trigger": "wait"},
                        },
                    }
                },
            }
        ),
    )
    async with factory() as session:
        session.add(account)
        await session.commit()
    try:
        yield factory, account, wakes, results, cards
    finally:
        await engine.dispose()


def executor(guard):
    return SimpleNamespace(get_account_control_guard=lambda: guard, export_account_control_records=guard.flush_records)


async def rows(factory):
    async with factory() as session:
        return list((await session.scalars(select(NotificationEvent))).all())


def test_running_warning_is_sent_once_and_deduplicated_across_execution_and_monitor_restart(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, wakes, results, cards):
            current = await persistence.build_account_control_guard(account, "first")
            monitor = monitor_module.AccountControlMonitor()

            async def read_source(account_id):
                monitor._sources[account_id] = [(providers.snapshot(account_id), clock_now_ms(), False)]

            monkeypatch.setattr(monitor, "_read_source", read_source)
            for _ in range(3):
                current.begin_operation("place_order").record_outcome("submitted")
            await monitor.refresh()
            assert await rows(factory) == []
            current.begin_operation("place_order").record_outcome("broker_sync_rejected")
            await monitor.refresh()
            assert wakes == [1]
            assert monitor.frames[1]["status"] == "running"
            queued = await rows(factory)
            assert len(queued) == 1
            assert queued[0].context["quota"]["used"] == 4
            assert queued[0].context["error"] is None
            await notification_outbox._dispatch(1)
            assert (await rows(factory))[0].status == "succeeded"
            assert len(cards) == len(results) == 1
            assert cards[0][1] == "system-test-hook"
            assert "4 / 5" in cards[0][0]["elements"][0]["content"]
            assert results[0].event_type == ACCOUNT_CONTROL_WARNING_EVENT
            await persistence.flush_account_control_records(executor(current))
            second = await persistence.build_account_control_guard(account, "second")
            second.begin_operation("place_order").record_outcome("submitted")
            await persistence.flush_account_control_records(executor(second))
            await monitor.refresh()
            restarted = monitor_module.AccountControlMonitor()
            await restarted.refresh()
            await notification_outbox._dispatch(1)
            assert len(await rows(factory)) == len(cards) == 1
            async with factory() as session:
                assert await session.get(AccountNotificationState, 1) is None
            assert (await rows(factory))[0].context["quota"]["used"] == 4

    asyncio.run(scenario())


def test_short_execution_enqueues_without_any_live_sampling_and_sender_failure_is_terminal(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, results, _):
            current = await persistence.build_account_control_guard(account, "short")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            assert len(await rows(factory)) == 1
            monkeypatch.setattr(settings, "exe_err_feishu_key", "")
            await notification_outbox._dispatch(1)
            event = (await rows(factory))[0]
            assert event.status == "failed"
            assert "未配置系统飞书" in event.error
            assert results[0].ok is False
            await notification_outbox._dispatch(1)
            assert len(results) == 1

    asyncio.run(scenario())


def test_custom_mode_reuses_system_function_and_only_system_credentials(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, _, cards):
            current = await persistence.build_account_control_guard(account, "custom")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            calls = []

            def run(code, context, **credentials):
                calls.append((code, context, credentials))
                return NotificationFunctionResult(True)

            monkeypatch.setattr(settings, "system_execution_notification_mode", "function")
            monkeypatch.setattr(settings, "system_execution_notification_code", "def notify(context): pass")
            monkeypatch.setattr(error_notifications, "run_notification_function", run)
            await notification_outbox._dispatch(1)
            assert (await rows(factory))[0].status == "succeeded"
            assert calls[0][0] == settings.system_execution_notification_code
            assert calls[0][1]["event_type"] == ACCOUNT_CONTROL_WARNING_EVENT
            assert calls[0][2] == {"system_feishu_key": "system-test-hook"}
            assert cards == []

    asyncio.run(scenario())


def test_saved_policy_change_does_not_rewrite_a_running_warning(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "frozen-policy")
            async with factory() as session:
                saved = await session.get(Account, 1)
                override = saved.account_control_override.model_copy(deep=True)
                override.operations["place_order"].daily_warning_percent = 99
                saved.account_control_override = override
                session.add(saved)
                await session.commit()
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            quota = (await rows(factory))[0].context["quota"]
            assert quota["warning_percent"] == 80
            assert quota["used"] == quota["threshold_count"] == 4

    asyncio.run(scenario())


def test_legacy_custom_function_failure_does_not_fall_back_or_modify_source(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, results, cards):
            current = await persistence.build_account_control_guard(account, "legacy-function")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            code = "def notify(context):\n    return context['error']['message']\n"
            monkeypatch.setattr(settings, "system_execution_notification_mode", "function")
            monkeypatch.setattr(settings, "system_execution_notification_code", code)
            await notification_outbox._dispatch(1)
            assert (await rows(factory))[0].status == "failed"
            assert results[0].ok is False
            assert cards == []
            assert settings.system_execution_notification_code == code

    asyncio.run(scenario())


def test_queue_failure_does_not_roll_back_control_counters(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "queue-failed")
            for _ in range(4):
                current.begin_operation("place_order")

            async def unavailable(*_args):
                raise OSError("queue unavailable")

            monkeypatch.setattr(warnings, "enqueue_quota_warnings", unavailable)
            await persistence.flush_account_control_records(executor(current))
            restored = await persistence.build_account_control_guard(account, "after-failure")
            metrics = restored.monitor_snapshot()["metrics"]
            assert next(row["value"] for row in metrics if row["operation"] == "place_order") == 4
            assert await rows(factory) == []

    asyncio.run(scenario())


def test_unregistered_retired_source_cannot_enqueue_warning_for_a_reused_account(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "retired")
            for _ in range(4):
                current.begin_operation("place_order")
            frame = current.monitor_snapshot()
            async with factory() as session:
                await session.execute(delete(AccountControlSession))
                await session.commit()
            providers._guards.clear()
            monkeypatch.setattr(monitor_module, "sample", lambda _account_id: (frame, clock_now_ms(), True))
            await monitor_module.AccountControlMonitor().refresh()
            assert await rows(factory) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_summary", [False, True])
def test_calling_warning_recovers_as_unknown_without_updating_account_notification(monkeypatch, fail_summary):
    async def scenario():
        async with database(monkeypatch) as (factory, account, _, results, cards):
            current = await persistence.build_account_control_guard(account, "interrupted")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            async with factory() as session:
                event = await session.get(NotificationEvent, (await rows(factory))[0].sequence)
                event.status = "calling"
                session.add(event)
                await session.commit()
            if fail_summary:

                def fail(_result):
                    raise OSError("summary unavailable")

                monkeypatch.setattr(notification_state, "record_system_notification_result", fail)
            await notification_outbox.recover_notifications()
            assert (await rows(factory))[0].status == "unknown"
            if not fail_summary:
                assert results[0].ok is None
                assert results[0].event_type == ACCOUNT_CONTROL_WARNING_EVENT
            async with factory() as session:
                assert await session.get(AccountNotificationState, 1) is None
            await notification_outbox._dispatch(1)
            assert cards == []

    asyncio.run(scenario())
