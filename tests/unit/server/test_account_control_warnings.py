"""额度预警的实时入队、短执行、去重、系统发送和重启边界。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest

from axile.common.config import settings
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server import error_notifications
from axile.server.account_control import monitor as monitor_module
from axile.server.account_control import providers, warnings
from axile.server.execution import execution_account_control as persistence
from axile.server.execution import notification_outbox
from tests.unit.server.test_account_control_monitor import database as monitor_database
from tests.unit.server.test_account_control_monitor import save_account


@asynccontextmanager
async def database(monkeypatch):
    async with monitor_database(monkeypatch) as (path, account, _):
        monkeypatch.setattr(notification_outbox, "_tasks", {})
        monkeypatch.setattr(notification_outbox, "_rerun", set())
        wakes, results, cards = [], [], []
        monkeypatch.setattr(notification_outbox, "wake_notifications", wakes.append)
        monkeypatch.setattr(error_notifications, "record_system_notification_result", results.append)
        monkeypatch.setattr(error_notifications, "push_feishu_card", lambda card, key: cards.append((card, key)))
        monkeypatch.setattr(monitor_module, "wake_pending_quota_warnings", warnings.wake_pending_quota_warnings)
        monkeypatch.setattr(settings, "system_execution_notification_mode", "default")
        monkeypatch.setattr(settings, "exe_err_feishu_key", "system-test-hook")
        account.account_control_override = AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "daily_warning_percent": 80,
                        "account": {
                            "per_day": {"limit": 5, "on_trigger": "block"},
                            "min_interval_ms": {"limit": 1, "on_trigger": "wait"},
                        },
                    }
                }
            }
        )
        save_account(path, account)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DELETE FROM account_control_counter_delta")
        yield path, account, wakes, results, cards


def executor(guard):
    return SimpleNamespace(get_account_control_guard=lambda: guard, export_account_control_records=guard.flush_records)


async def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            SimpleNamespace(**{**dict(row), "context": json.loads(row["context"])})
            for row in connection.execute("SELECT * FROM notification_event ORDER BY sequence")
        ]


def test_running_warning_is_sent_once_and_deduplicated_across_execution_and_monitor_restart(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, wakes, results, cards):
            current = await persistence.build_account_control_guard(account, "first")
            monitor = monitor_module.AccountControlMonitor()

            async def read_source(account_id):
                monitor._sources[account_id] = [(providers.snapshot(account_id), clock_now_ms(), False)]

            monkeypatch.setattr(monitor, "_read_source", read_source)
            for _ in range(3):
                current.begin_operation("place_order").record_outcome("submitted")
            await monitor.refresh()
            assert await rows(path) == []
            current.begin_operation("place_order").record_outcome("broker_sync_rejected")
            await monitor.refresh()
            assert wakes == [1]
            assert monitor.frames[1]["status"] == "running"
            queued = await rows(path)
            assert len(queued) == 1
            assert queued[0].context["quota"]["used"] == 4
            assert queued[0].context["error"] is None
            await notification_outbox._dispatch(1)
            assert (await rows(path))[0].status == "succeeded"
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
            assert len(await rows(path)) == len(cards) == 1
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute("SELECT count(*) FROM account_notification_state").fetchone()[0] == 0
            assert (await rows(path))[0].context["quota"]["used"] == 4

    asyncio.run(scenario())


def test_short_execution_enqueues_without_any_live_sampling_and_sender_failure_is_terminal(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, results, _):
            current = await persistence.build_account_control_guard(account, "short")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            assert len(await rows(path)) == 1
            monkeypatch.setattr(settings, "exe_err_feishu_key", "")
            await notification_outbox._dispatch(1)
            event = (await rows(path))[0]
            assert event.status == "failed"
            assert "未配置系统飞书" in event.error
            assert results[0].ok is False
            await notification_outbox._dispatch(1)
            assert len(results) == 1

    asyncio.run(scenario())


def test_custom_mode_reuses_system_function_and_only_system_credentials(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, cards):
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
            assert (await rows(path))[0].status == "succeeded"
            assert calls[0][0] == settings.system_execution_notification_code
            assert calls[0][1]["event_type"] == ACCOUNT_CONTROL_WARNING_EVENT
            assert calls[0][2] == {"system_feishu_key": "system-test-hook"}
            assert cards == []

    asyncio.run(scenario())


def test_saved_policy_change_does_not_rewrite_a_running_warning(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "frozen-policy")
            changed = account.model_copy(deep=True)
            override = changed.account_control_override.model_copy(deep=True)
            override.operations["place_order"].daily_warning_percent = 99
            changed.account_control_override = override
            save_account(path, changed)
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            quota = (await rows(path))[0].context["quota"]
            assert quota["warning_percent"] == 80
            assert quota["used"] == quota["threshold_count"] == 4

    asyncio.run(scenario())


def test_legacy_custom_function_failure_does_not_fall_back_or_modify_source(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, results, cards):
            current = await persistence.build_account_control_guard(account, "legacy-function")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            code = "def notify(context):\n    return context['error']['message']\n"
            monkeypatch.setattr(settings, "system_execution_notification_mode", "function")
            monkeypatch.setattr(settings, "system_execution_notification_code", code)
            await notification_outbox._dispatch(1)
            assert (await rows(path))[0].status == "failed"
            assert results[0].ok is False
            assert cards == []
            assert settings.system_execution_notification_code == code

    asyncio.run(scenario())


def test_queue_failure_does_not_roll_back_control_counters(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "queue-failed")
            for _ in range(4):
                current.begin_operation("place_order")

            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript(
                    "CREATE TRIGGER fail_warning BEFORE INSERT ON notification_event "
                    "BEGIN SELECT RAISE(ABORT,'queue unavailable'); END;"
                )
            await persistence.flush_account_control_records(executor(current))
            restored = await persistence.build_account_control_guard(account, "after-failure")
            metrics = restored.monitor_snapshot()["metrics"]
            assert next(row["value"] for row in metrics if row["operation"] == "place_order") == 4
            assert await rows(path) == []

    asyncio.run(scenario())


def test_monitor_warning_failure_keeps_late_outcomes_and_retries_warning(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, _):
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript(
                    "CREATE TRIGGER fail_warning BEFORE INSERT ON notification_event "
                    "BEGIN SELECT RAISE(ABORT,'queue unavailable'); END;"
                )
            current = await persistence.build_account_control_guard(account, "late-outcome")
            for _ in range(4):
                current.begin_operation("place_order").record_outcome("submitted")
            await persistence.flush_account_control_records(executor(current))
            current.update_event_outcome(0, outcome="broker_rejected", metadata={"error_id": 31})
            monitor = monitor_module.AccountControlMonitor()

            async def source(account_id):
                monitor._sources[account_id] = [(providers.snapshot(account_id), clock_now_ms(), False)]

            monkeypatch.setattr(monitor, "_read_source", source)
            await monitor.refresh()
            with closing(sqlite3.connect(path)) as connection, connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM account_control_event WHERE outcome='broker_rejected'"
                    ).fetchone()[0]
                    == 1
                )
                assert connection.execute("SELECT count(*) FROM notification_event").fetchone()[0] == 0
                connection.execute("DROP TRIGGER fail_warning")
            assert monitor._queued_warnings == {}
            await monitor.refresh()
            assert len(await rows(path)) == 1

    asyncio.run(scenario())


def test_unregistered_retired_source_cannot_enqueue_warning_for_a_reused_account(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, _):
            current = await persistence.build_account_control_guard(account, "retired")
            for _ in range(4):
                current.begin_operation("place_order")
            frame = current.monitor_snapshot()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DELETE FROM account_control_session")
            providers._guards.clear()
            monkeypatch.setattr(monitor_module, "sample", lambda _account_id: (frame, clock_now_ms(), True))
            await monitor_module.AccountControlMonitor().refresh()
            assert await rows(path) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_summary", [False, True])
def test_calling_warning_recovers_as_unknown_without_updating_account_notification(monkeypatch, fail_summary):
    async def scenario():
        async with database(monkeypatch) as (path, account, _, _, cards):
            current = await persistence.build_account_control_guard(account, "interrupted")
            for _ in range(4):
                current.begin_operation("place_order")
            await persistence.flush_account_control_records(executor(current))
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE notification_event SET status='calling'")
                if fail_summary:
                    connection.executescript(
                        "CREATE TRIGGER fail_system_summary BEFORE INSERT ON system_notification_state "
                        "BEGIN SELECT RAISE(ABORT,'summary unavailable'); END;"
                    )
            await notification_outbox.recover_notifications()
            event = (await rows(path))[0]
            assert event.status == "unknown"
            assert event.finished_at
            assert event.error == "服务重启，通知调用结果未知"
            with closing(sqlite3.connect(path)) as connection:
                summary = connection.execute(
                    "SELECT finished_at,event_type,execution_id,account_id,ok,error "
                    "FROM system_notification_state WHERE id=1"
                ).fetchone()
                if fail_summary:
                    assert summary is None
                else:
                    assert summary == (
                        event.finished_at,
                        ACCOUNT_CONTROL_WARNING_EVENT,
                        "interrupted",
                        account.id,
                        None,
                        event.error,
                    )
                assert connection.execute("SELECT count(*) FROM account_notification_state").fetchone()[0] == 0
            await notification_outbox._dispatch(1)
            assert cards == []

    asyncio.run(scenario())
