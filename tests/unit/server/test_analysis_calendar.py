"""渠道 SDK 评估结束后一次提交日历；数据库日期边界由原生准备阶段提供。"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from axile.server import analysis_calendar
from axile.server.analysis_calendar import ensure_calendar_facts
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus


def decision(day, status=CalendarDecisionStatus.AVAILABLE_OPEN):
    return CalendarDayDecision(channel="ctp", day=day, status=status, calendar_id="SHFE", label="期货交易日历")


@pytest.mark.parametrize("args", [(None, "2026-01-01", "2026-01-02"), ("ctp", None, None)])
def test_missing_channel_or_history_does_not_evaluate_or_write(monkeypatch, args):
    def evaluate(*args):
        pytest.fail("没有观测或渠道时不应读取交易日历")

    write = AsyncMock()
    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr(analysis_calendar, "native_operation", write)
    assert asyncio.run(ensure_calendar_facts(*args)) == 0
    write.assert_not_awaited()


def test_full_range_is_evaluated_before_one_native_write(monkeypatch):
    evaluated = []
    write = AsyncMock(return_value=5)

    def evaluate(channel, day):
        write.assert_not_awaited()
        evaluated.append((channel, day))
        return decision(
            day, CalendarDecisionStatus.AVAILABLE_CLOSED if day.day == 3 else CalendarDecisionStatus.AVAILABLE_OPEN
        )

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr(analysis_calendar, "native_operation", write)
    assert asyncio.run(ensure_calendar_facts("ctp", "2026-01-01", "2026-01-05")) == 5
    assert evaluated == [("ctp", date(2026, 1, day)) for day in range(1, 6)]
    name, request = write.await_args.args
    assert name == "analysis_calendar_save"
    assert request["channel"] == "ctp"
    assert [row["day"] for row in request["days"]] == [f"2026-01-0{day}" for day in range(1, 6)]
    assert request["days"][2] == {
        "day": "2026-01-03",
        "status": "available_closed",
        "calendar_id": "SHFE",
        "label": "期货交易日历",
    }
    write.assert_awaited_once()


@pytest.mark.parametrize("unregistered", [False, True])
def test_terminal_channel_decisions_fill_all_days_after_one_evaluation(monkeypatch, unregistered):
    calls = []
    write = AsyncMock(return_value=3)

    def evaluate(channel, day):
        calls.append(day)
        if unregistered:
            raise KeyError(channel)
        return decision(day, CalendarDecisionStatus.NOT_REQUIRED)

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr(analysis_calendar, "native_operation", write)
    assert asyncio.run(ensure_calendar_facts("ctp", "2026-01-01", "2026-01-03")) == 3
    assert calls == [date(2026, 1, 1)]
    expected = "unregistered" if unregistered else "not_required"
    days = write.await_args.args[1]["days"]
    assert {row["status"] for row in days} == {expected}
    assert all(row["calendar_id"] is None and row["label"] is None for row in days)


def test_sdk_failure_leaves_the_previous_calendar_untouched(monkeypatch):
    write = AsyncMock()

    def evaluate(channel, day):
        if day.day == 2:
            raise RuntimeError("SDK unavailable")
        return decision(day)

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr(analysis_calendar, "native_operation", write)
    with pytest.raises(RuntimeError, match="SDK unavailable"):
        asyncio.run(ensure_calendar_facts("ctp", "2026-01-01", "2026-01-03"))
    write.assert_not_awaited()
