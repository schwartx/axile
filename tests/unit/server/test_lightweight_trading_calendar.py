"""渠道轻量交易日历决策测试。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from axile.common.trade_channel import TradeChannel
from axile.server.trading_calendar import (
    CalendarDecisionStatus,
    CalendarUnavailableReason,
    evaluate_channel_calendar_day,
    evaluate_channel_calendar_moment,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Calendar:
    def __init__(self, days: dict[date, bool | None]) -> None:
        self.days = days

    def is_open(self, calendar_id: str, day: date) -> bool | None:
        assert calendar_id == "china"
        return self.days.get(day)


class _BrokenCalendar:
    def is_open(self, _calendar_id: str, _day: date) -> bool | None:
        raise RuntimeError("calendar failed")


def test_calendar_uncovered_and_read_failure_are_distinct() -> None:
    uncovered = evaluate_channel_calendar_day(TradeChannel.CTP, date(2027, 1, 1), calendar=_Calendar({}))
    failed = evaluate_channel_calendar_day(TradeChannel.CTP, date(2026, 8, 27), calendar=_BrokenCalendar())

    assert uncovered.status is CalendarDecisionStatus.UNAVAILABLE
    assert uncovered.unavailable_reason is CalendarUnavailableReason.UNCOVERED
    assert failed.status is CalendarDecisionStatus.UNAVAILABLE
    assert failed.unavailable_reason is CalendarUnavailableReason.READ_FAILED


def test_friday_night_maps_to_monday_trading_day() -> None:
    calendar = _Calendar(
        {
            date(2026, 8, 21): True,
            date(2026, 8, 22): False,
            date(2026, 8, 23): False,
            date(2026, 8, 24): True,
        }
    )

    decision = evaluate_channel_calendar_moment(
        TradeChannel.CTP,
        datetime(2026, 8, 21, 21, 30, tzinfo=_SHANGHAI),
        calendar=calendar,
    )

    assert decision.status is CalendarDecisionStatus.AVAILABLE_OPEN
    assert decision.day == date(2026, 8, 24)


def test_holiday_eve_has_no_corresponding_night_session() -> None:
    days = {date(2026, 9, 30): True, date(2026, 10, 8): True}
    days.update({date(2026, 10, day): False for day in range(1, 8)})

    decision = evaluate_channel_calendar_moment(
        TradeChannel.CTP,
        datetime(2026, 9, 30, 21, 30, tzinfo=_SHANGHAI),
        calendar=_Calendar(days),
    )

    assert decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED
    assert decision.reason_code == "CALENDAR.NO_NIGHT_SESSION"


def test_weekday_session_gap_is_closed_before_day_calendar() -> None:
    """工作日 20:34 是市场缝，不能因为「今日开市」就放行。"""
    decision = evaluate_channel_calendar_moment(
        TradeChannel.CTP,
        datetime(2026, 8, 26, 20, 34, tzinfo=_SHANGHAI),
        calendar=_Calendar({date(2026, 8, 26): True}),
    )
    assert decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED
    assert decision.reason_code == "CALENDAR.SESSION_CLOSED"


def test_night_open_is_not_session_gap() -> None:
    decision = evaluate_channel_calendar_moment(
        TradeChannel.CTP,
        datetime(2026, 8, 26, 21, 15, tzinfo=_SHANGHAI),
        calendar=_Calendar({date(2026, 8, 26): True, date(2026, 8, 27): True}),
    )
    assert decision.status is CalendarDecisionStatus.AVAILABLE_OPEN
    assert decision.reason_code is None


@pytest.mark.parametrize(
    ("channel", "moment"),
    [
        (TradeChannel.CTP, datetime(2026, 9, 18, 11, 30, tzinfo=_SHANGHAI)),
        (TradeChannel.CTP, datetime(2026, 9, 18, 12, 0, tzinfo=_SHANGHAI)),
        (TradeChannel.GM, datetime(2026, 9, 18, 11, 30, tzinfo=_SHANGHAI)),
        (TradeChannel.GM, datetime(2026, 9, 18, 12, 0, tzinfo=_SHANGHAI)),
    ],
)
def test_lunch_is_session_closed_on_an_open_day(channel: TradeChannel, moment: datetime) -> None:
    decision = evaluate_channel_calendar_moment(
        channel,
        moment,
        calendar=_Calendar({date(2026, 9, 18): True}),
    )
    assert decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED
    assert decision.reason_code == "CALENDAR.SESSION_CLOSED"
