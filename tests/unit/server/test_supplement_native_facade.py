"""补发编排只消费已提交的原生回执，外部副作用不进入事务。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.server.cron import SCHEDULER_TIMEZONE
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.execution import intents, notification_outbox, supplement_notifications, supplements

BASE = datetime(2026, 10, 9, 9, 30, tzinfo=SCHEDULER_TIMEZONE)


@pytest.mark.parametrize("failed", [False, True])
def test_cancel_waits_for_commit_before_memory_cleanup_and_notifications(monkeypatch, failed):
    calls = []

    async def cancel(name, request):
        assert name == "supplement_cancel_groups"
        assert request["account_id"] == 1
        assert request["reason"] == "account_stopped"
        calls.append("write")
        if failed:
            raise RuntimeError("commit failed")
        calls.append("committed")
        return True

    async def reap(account_id):
        assert account_id == 1
        assert calls[-1] == "committed"
        calls.append("reap")

    monkeypatch.setattr(supplements, "native_operation", cancel)
    monkeypatch.setattr(supplements, "reap_cancelled_queue", reap)
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _id: calls.append("wake"))
    if failed:
        with pytest.raises(RuntimeError, match="commit failed"):
            asyncio.run(supplements.cancel_groups_locked(1, "account_stopped"))
        assert calls == ["write"]
    else:
        assert asyncio.run(supplements.cancel_groups_locked(1, "account_stopped"))
        assert calls == ["write", "committed", "reap", "wake"]


def test_calendar_closed_cancels_before_submitting_any_intent(monkeypatch):
    submit = AsyncMock(side_effect=AssertionError("closed step must not submit"))
    cancel = AsyncMock()
    monkeypatch.setattr(intents, "submit_intent", submit)
    monkeypatch.setattr(supplements, "cancel_groups", cancel)
    monkeypatch.setattr(
        supplements,
        "native_operation",
        AsyncMock(
            return_value={
                "step": {"status": "pending"},
                "group": {"id": "g", "account_id": 1, "status": "active"},
                "channel": "ctp",
            }
        ),
    )
    monkeypatch.setattr(
        supplements, "calendar_context", lambda _channel: {"calendar_status_by_channel": {"ctp": "available_closed"}}
    )

    asyncio.run(supplements.fire_step("s"))

    submit.assert_not_awaited()
    cancel.assert_awaited_once_with(1, "schedule_interrupted", group_id="g")


def test_duplicate_group_receipt_does_not_schedule_or_fire(monkeypatch):
    account = SimpleNamespace(
        is_started=True,
        supplement=SupplementSettings(count=2, interval_minutes=1),
        portfolio_id=1,
        cron_expr="*/15 * * * *",
        trade_channel="ctp",
    )
    native = AsyncMock(return_value=None)
    fire = AsyncMock(side_effect=AssertionError("duplicate group must not fire"))
    monkeypatch.setattr(supplements, "get_account_snapshot", AsyncMock(return_value=account))
    monkeypatch.setattr(supplements, "native_operation", native)
    monkeypatch.setattr(supplements, "plan_supplements", lambda *_args: ([BASE], BASE))
    monkeypatch.setattr(supplements, "schedule_expiration", lambda _group: pytest.fail("duplicate group scheduled"))
    monkeypatch.setattr(supplements, "fire_step", fire)

    asyncio.run(supplements.create_group(1, BASE))

    native.assert_awaited_once()
    assert native.await_args.args[0] == "supplement_create_group"
    fire.assert_not_awaited()


def test_requests_without_supplements_do_not_open_a_write_stage(monkeypatch):
    native = AsyncMock(return_value={"steps": [], "channel": None})
    monkeypatch.setattr(supplement_notifications, "native_operation", native)

    assert asyncio.run(supplement_notifications.validate_execution_requests("execution"))

    native.assert_awaited_once_with("supplement_execution_requests", {"execution_id": "execution"})
