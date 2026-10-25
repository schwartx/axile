"""自动排程预览与触发测试。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind
from axile.server.api.routes import account_schedule, account_support
from axile.server.cron import SCHEDULER_TIMEZONE, combine_cron_triggers, parse_cron_expr
from axile.server.execution import intents as execution_intents
from axile.server.execution import scheduler as execution_scheduler
from axile.server.trading_calendar import (
    CalendarDayDecision,
    CalendarDecisionStatus,
    CalendarUnavailableReason,
)
from tests.unit.server._runtime_db_support import runtime_database, runtime_rows


class _NativeContext:
    def __init__(self, account: SimpleNamespace | None = None) -> None:
        self.account = vars(account) if account is not None else None
        self.added: list[SimpleNamespace] = []
        self.audit_error: Exception | None = None

    async def call(self, name: str, request: dict[str, object]) -> object:
        if name == "runtime_sync_scheduler_account":
            return self.account
        assert name == "runtime_sync_record_schedule_skip"
        if self.audit_error is not None:
            raise self.audit_error
        self.added.append(SimpleNamespace(**request))
        return {"id": len(self.added)}


def _account(*, started: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        name="测试账户",
        is_started=started,
        cron_expr="30 9 * * *",
        trade_channel=TradeChannel.CTP,
        supplement=None,
    )


def _decision(status: CalendarDecisionStatus) -> CalendarDayDecision:
    return CalendarDayDecision(
        channel="ctp",
        day=date(2026, 8, 29),
        status=status,
        calendar_id="china",
        label="中国交易日历",
        unavailable_reason=(
            CalendarUnavailableReason.UNCOVERED if status is CalendarDecisionStatus.UNAVAILABLE else None
        ),
        reason_code=("CALENDAR.CLOSED" if status is CalendarDecisionStatus.AVAILABLE_CLOSED else None),
    )


def test_scheduled_rebalance_delegates_on_open_day(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_OPEN),
    )
    submitted: list[tuple[object, ...]] = []

    async def fake_submit(account_id: object, kind: object, trigger_source: object, **kwargs: object) -> object:
        submitted.append((account_id, kind, trigger_source, kwargs.get("on_conflict")))
        return SimpleNamespace(outcome="created", execution_id="exec-1", account_id=account_id)

    monkeypatch.setattr("axile.server.execution.intents.submit_intent", fake_submit)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    assert submitted == [(7, ExecutionKind.REBALANCE, "scheduler", "skip")]


def test_scheduled_rebalance_skips_closed_day_and_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED),
    )
    submit_intent = MagicMock()
    monkeypatch.setattr(execution_intents, "submit_intent", submit_intent)

    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))

    submit_intent.assert_not_called()
    assert len(session.added) == 1
    assert getattr(session.added[0], "reason_code") == "CALENDAR.CLOSED"


def test_scheduled_rebalance_skips_session_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED).model_copy(
            update={"reason_code": "CALENDAR.SESSION_CLOSED"}
        ),
    )
    submit_intent = MagicMock()
    monkeypatch.setattr(execution_intents, "submit_intent", submit_intent)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    submit_intent.assert_not_called()
    assert getattr(session.added[0], "reason_code") == "CALENDAR.SESSION_CLOSED"


def test_scheduled_rebalance_is_fail_closed_when_calendar_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.UNAVAILABLE),
    )
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    assert session.added[0].reason_code == "CALENDAR.UNAVAILABLE"


def test_scheduled_rebalance_records_calendar_evaluator_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)

    def fail(*_args: object) -> None:
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(execution_scheduler, "evaluate_channel_calendar_moment", fail)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))

    assert session.added[0].reason_code == "CALENDAR.UNAVAILABLE"
    assert session.added[0].calendar_id == ""


def test_closed_day_without_reason_uses_closed_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED).model_copy(update={"reason_code": None}),
    )
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))

    assert session.added[0].reason_code == "CALENDAR.CLOSED"


def test_closed_day_stays_skipped_when_audit_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    session.audit_error = RuntimeError("database unavailable")
    monkeypatch.setattr(execution_scheduler, "native_operation", session.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED),
    )
    submit_intent = MagicMock()
    monkeypatch.setattr(execution_intents, "submit_intent", submit_intent)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    submit_intent.assert_not_called()


def test_closed_day_stays_skipped_when_audit_session_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _NativeContext(_account())
    calls = 0

    async def native_operation(name, request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database unavailable")
        return await session.call(name, request)

    monkeypatch.setattr(execution_scheduler, "native_operation", native_operation)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED),
    )

    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    assert calls == 2


def test_scheduled_rebalance_ignores_stale_job_for_stopped_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_scheduler, "native_operation", _NativeContext(_account(started=False)).call)
    submit_intent = MagicMock()
    monkeypatch.setattr(execution_intents, "submit_intent", submit_intent)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    submit_intent.assert_not_called()


def test_busy_schedule_records_skip_after_intent_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _NativeContext(_account())
    monkeypatch.setattr(execution_scheduler, "native_operation", store.call)
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_OPEN),
    )

    async def submit(*_args, **_kwargs):
        return SimpleNamespace(outcome="skipped_busy")

    monkeypatch.setattr("axile.server.execution.intents.submit_intent", submit)
    asyncio.run(execution_scheduler.execute_scheduled_rebalance(7))
    assert [skip.reason_code for skip in store.added] == ["BUSY"]


def test_native_schedule_reads_settings_and_commits_calendar_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        execution_scheduler,
        "evaluate_channel_calendar_moment",
        lambda *_args: _decision(CalendarDecisionStatus.AVAILABLE_CLOSED),
    )

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            await execution_scheduler.execute_scheduled_rebalance(1, "2026-08-29T09:30:00+08:00")
            rows = runtime_rows(
                path, "SELECT account_id,channel,triggered_at,calendar_day,reason_code FROM schedule_skip"
            )
            assert [tuple(row) for row in rows] == [
                (1, "ctp", "2026-08-29T09:30:00+08:00", "2026-08-29", "CALENDAR.CLOSED")
            ]

    asyncio.run(scenario())


def _patch_reconcile(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    async def fake_create(*_args: object, **_kwargs: object) -> None:
        events.append("create")

    def fake_delete(*_args: object) -> None:
        events.append("delete")

    monkeypatch.setattr(account_support, "create_job", fake_create)
    monkeypatch.setattr(account_support, "delete_job", fake_delete)
    return events


def test_reconcile_recreates_when_compact_str_collides_but_hours_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account.cron_expr = "0 11 * * * | 0 16 * * *"
    events = _patch_reconcile(monkeypatch)
    existing = SimpleNamespace(trigger=combine_cron_triggers(parse_cron_expr("0 10 * * * | 0 15 * * *")))
    sched = SimpleNamespace(get_job=lambda _job_id: existing)

    asyncio.run(account_support._apply_account_job(sched, account, 11))

    assert events == ["delete", "create"]


def test_schedule_preview_returns_raw_cron_points_with_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluated = datetime(2026, 8, 26, 9, 0, tzinfo=SCHEDULER_TIMEZONE)

    monkeypatch.setattr(account_schedule, "clock_now", lambda *, tz: evaluated)
    payload = account_schedule.SchedulePreviewRequest(
        trade_channel=TradeChannel.CTP,
        cron_expr="30 9 * * *",
        limit=2,
    )
    response = asyncio.run(account_schedule.schedule_preview(payload))
    assert [item.scheduled_at.isoformat() for item in response.items] == [
        "2026-08-26T09:30:00+08:00",
        "2026-08-27T09:30:00+08:00",
    ]
    assert response.next_cursor == response.items[-1].scheduled_at
    assert response.has_more is True

    continued = asyncio.run(
        account_schedule.schedule_preview(payload.model_copy(update={"after": response.next_cursor}))
    )
    assert continued.items[0].scheduled_at.isoformat() == "2026-08-28T09:30:00+08:00"
