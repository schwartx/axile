"""按渠道声明评估轻量交易日历。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from axile.channels import get_channel
from axile.channels.gm_sessions import GM_REGULAR_WINDOWS, GM_SPECIAL_WINDOWS, GMSessionSettings
from axile.channels.schedule_clock import ClockWindow, is_within_schedule_windows
from axile.common.trade_channel import TradeChannel
from axile.executor.china_futures_session import is_regular_night_session_transition
from axile.executor.trading_calendar import ShinnyTradingCalendar, TradingCalendar


def schedule_windows(channel: TradeChannel | str, sessions: GMSessionSettings | None = None) -> tuple[ClockWindow, ...]:
    """按账户选择组装自动交易窗，各阶段保留独立补发边界。"""
    if str(channel) != "gm":
        if sessions is not None:
            sessions.validate_channel(str(channel))
        return tuple((w.start, w.end) for w in get_channel(str(channel)).descriptor.schedule.windows)
    choice = sessions or GMSessionSettings()
    return (
        *GM_REGULAR_WINDOWS,
        *((start, end) for name, (start, end, _) in GM_SPECIAL_WINDOWS.items() if getattr(choice, name).enabled),
    )


class CalendarDecisionStatus(StrEnum):
    """渠道在指定自然日上的交易日历状态。"""

    AVAILABLE_OPEN = "available_open"
    AVAILABLE_CLOSED = "available_closed"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class CalendarUnavailableReason(StrEnum):
    """轻量交易日历无法作出判断的原因。"""

    UNCOVERED = "uncovered"
    READ_FAILED = "read_failed"


type CalendarSkipReason = Literal[
    "CALENDAR.CLOSED", "CALENDAR.NO_NIGHT_SESSION", "CALENDAR.SESSION_CLOSED", "CALENDAR.UNAVAILABLE"
]


class CalendarDayDecision(BaseModel):
    """一次渠道交易日历判断结果。"""

    channel: str
    day: date
    status: CalendarDecisionStatus
    unavailable_reason: CalendarUnavailableReason | None = None
    calendar_id: str | None = None
    label: str | None = None
    effective_is_open: bool | None = None
    reason_code: CalendarSkipReason | None = None


_DEFAULT_CALENDAR = ShinnyTradingCalendar()


def evaluate_channel_calendar_day(
    channel: TradeChannel | str,
    day: date,
    *,
    calendar: TradingCalendar = _DEFAULT_CALENDAR,
) -> CalendarDayDecision:
    """判断一个渠道在单个自然日上的日历可用性。"""
    channel_name = str(channel)
    declaration = get_channel(channel_name).descriptor.calendar
    if declaration is None:
        return CalendarDayDecision(channel=channel_name, day=day, status=CalendarDecisionStatus.NOT_REQUIRED)
    try:
        is_open = calendar.is_open(declaration.calendar_id, day)
    except Exception:  # noqa: BLE001 - 由调用方按 fail-open 契约决定是否执行
        return CalendarDayDecision(
            channel=channel_name,
            day=day,
            calendar_id=declaration.calendar_id,
            label=declaration.label,
            status=CalendarDecisionStatus.UNAVAILABLE,
            unavailable_reason=CalendarUnavailableReason.READ_FAILED,
        )
    if is_open is None:
        return CalendarDayDecision(
            channel=channel_name,
            day=day,
            calendar_id=declaration.calendar_id,
            label=declaration.label,
            status=CalendarDecisionStatus.UNAVAILABLE,
            unavailable_reason=CalendarUnavailableReason.UNCOVERED,
        )
    return CalendarDayDecision(
        channel=channel_name,
        day=day,
        calendar_id=declaration.calendar_id,
        label=declaration.label,
        status=(CalendarDecisionStatus.AVAILABLE_OPEN if is_open else CalendarDecisionStatus.AVAILABLE_CLOSED),
        effective_is_open=is_open,
    )


def _china_futures_night_start(current: datetime) -> date | None:
    """返回期货夜盘所属的起始自然日，日盘时段返回 ``None``。"""
    local_time = current.timetz().replace(tzinfo=None)
    if local_time >= time(21):
        return current.date()
    if local_time <= time(2, 30):
        return current.date() - timedelta(days=1)
    return None


def evaluate_channel_calendar_moment(
    channel: TradeChannel | str,
    current: datetime,
    *,
    windows: tuple[ClockWindow, ...] | None = None,
    calendar: TradingCalendar = _DEFAULT_CALENDAR,
) -> CalendarDayDecision:
    """按渠道时段把一次触发映射到交易日并判断是否执行。"""
    channel_name = str(channel)
    descriptor = get_channel(channel_name).descriptor
    clock_windows = (
        windows if windows is not None else tuple((window.start, window.end) for window in descriptor.schedule.windows)
    )
    if clock_windows and not is_within_schedule_windows(current, clock_windows):
        # 只把「今日开市」改写成市场缝；日历不可用 / 休市日保持原判定，避免盖掉 fail-open。
        decision = evaluate_channel_calendar_day(channel, current.date(), calendar=calendar)
        if decision.status is CalendarDecisionStatus.AVAILABLE_OPEN:
            return decision.model_copy(
                update={
                    "status": CalendarDecisionStatus.AVAILABLE_CLOSED,
                    "effective_is_open": False,
                    "reason_code": "CALENDAR.SESSION_CLOSED",
                }
            )
        return decision
    session_start = (
        _china_futures_night_start(current)
        if descriptor.schedule.kind == "cn_futures" and descriptor.schedule.night is not None
        else None
    )
    if session_start is None:
        decision = evaluate_channel_calendar_day(channel, current.date(), calendar=calendar)
        if decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED:
            return decision.model_copy(update={"reason_code": "CALENDAR.CLOSED"})
        return decision

    nominal_day = session_start + timedelta(days=1)
    start_decision = evaluate_channel_calendar_day(channel, session_start, calendar=calendar)
    if start_decision.status is CalendarDecisionStatus.NOT_REQUIRED:
        return start_decision.model_copy(update={"day": nominal_day})
    if start_decision.status is CalendarDecisionStatus.UNAVAILABLE:
        return start_decision.model_copy(update={"day": nominal_day})
    if start_decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED:
        return start_decision.model_copy(update={"reason_code": "CALENDAR.NO_NIGHT_SESSION"})

    for offset in range(1, 15):
        candidate_day = session_start + timedelta(days=offset)
        candidate = evaluate_channel_calendar_day(channel, candidate_day, calendar=calendar)
        if candidate.status is CalendarDecisionStatus.UNAVAILABLE:
            return candidate
        if candidate.status is not CalendarDecisionStatus.AVAILABLE_OPEN:
            continue
        if is_regular_night_session_transition(session_start, candidate_day):
            return candidate
        return candidate.model_copy(
            update={
                "status": CalendarDecisionStatus.AVAILABLE_CLOSED,
                "effective_is_open": False,
                "reason_code": "CALENDAR.NO_NIGHT_SESSION",
            }
        )

    return start_decision.model_copy(
        update={
            "day": nominal_day,
            "status": CalendarDecisionStatus.AVAILABLE_CLOSED,
            "effective_is_open": False,
            "reason_code": "CALENDAR.NO_NIGHT_SESSION",
        }
    )


__all__ = [
    "CalendarDayDecision",
    "CalendarDecisionStatus",
    "CalendarSkipReason",
    "CalendarUnavailableReason",
    "evaluate_channel_calendar_day",
    "evaluate_channel_calendar_moment",
]
