"""渠道级市场钟：全市场可报单窗与盘中节奏落点.

账户定时只描述节奏；本模块回答「这个渠道此刻有没有任何品种能下单」，
以及把频率对齐到左闭右开窗内的可报单分钟。

空窗表示连续交易，永远开盘。隔夜窗 ``start > end``，例如 ``21:00–02:30``。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

type ClockWindow = tuple[str, str]

CN_STOCK_WINDOWS: tuple[ClockWindow, ...] = (
    ("09:15", "09:25"),
    ("09:30", "11:30"),
    ("13:00", "15:00"),
)
CN_FUTURES_WINDOWS: tuple[ClockWindow, ...] = (
    ("09:00", "11:30"),
    ("13:00", "15:15"),
    ("21:00", "02:30"),
)

# 期货下午钟到 15:15（国债），默认 15 分/60 分 bar 仍按 15:00 收盘对齐。
_FUTURES_BAR_AFTERNOON_END = "15:00"


def parse_hhmm(value: str) -> time:
    """把 ``HH:MM`` 解析成不含时区的 ``time``."""
    hour_text, minute_text = value.split(":", 1)
    return time(int(hour_text), int(minute_text))


def _as_minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _from_minutes(total: int) -> str:
    wrapped = total % (24 * 60)
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


def window_contains(start: time, end: time, clock: time) -> bool:
    """左闭右开；隔夜窗覆盖 ``start`` 之后或 ``end`` 之前."""
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def is_overnight_window(start: str, end: str) -> bool:
    """开始时刻不早于结束时刻时视为隔夜窗."""
    return parse_hhmm(start) >= parse_hhmm(end)


def window_span_minutes(start: time, end: time) -> int:
    """窗口长度（分钟），隔夜跨过 24:00."""
    begin = _as_minutes(start)
    finish = _as_minutes(end)
    if start < end:
        return finish - begin
    return 24 * 60 - begin + finish


def is_within_schedule_windows(now: datetime, windows: Sequence[ClockWindow]) -> bool:
    """判断 ``now`` 是否落在任一可报单窗内.

    空 ``windows`` 视为连续交易，永远为真。无时区的 ``datetime`` 按上海时间理解。
    """
    if not windows:
        return True
    local = now.astimezone(_SHANGHAI) if now.tzinfo is not None else now.replace(tzinfo=_SHANGHAI)
    clock = local.time()
    return any(window_contains(parse_hhmm(start), parse_hhmm(end), clock) for start, end in windows)


def day_windows(windows: Sequence[ClockWindow]) -> tuple[ClockWindow, ...]:
    """去掉隔夜窗，供日盘节奏使用."""
    return tuple(window for window in windows if not is_overnight_window(*window))


def overnight_windows(windows: Sequence[ClockWindow]) -> tuple[ClockWindow, ...]:
    """只保留隔夜窗，供夜盘节奏使用."""
    return tuple(window for window in windows if is_overnight_window(*window))


def bar_windows(kind: str, windows: Sequence[ClockWindow]) -> tuple[ClockWindow, ...]:
    """节奏对齐用的窗.

    期货下午钟到 15:15，bar 仍对齐 15:00，避免 15:14 这种假收盘点。
    """
    if kind != "cn_futures":
        return tuple(windows)
    adjusted: list[ClockWindow] = []
    for start, end in windows:
        if start == "13:00" and end == "15:15":
            adjusted.append((start, _FUTURES_BAR_AFTERNOON_END))
        else:
            adjusted.append((start, end))
    return tuple(adjusted)


def last_tradable_hhmm(start: str, end: str) -> str:
    """窗口内最后一分钟（右开端点的前一分钟）."""
    begin = parse_hhmm(start)
    span = window_span_minutes(begin, parse_hhmm(end))
    return _from_minutes(_as_minutes(begin) + span - 1)


def close_lead_hhmm(start: str, end: str, lead_minutes: int = 5) -> str:
    """收盘前 ``lead_minutes`` 分钟，且仍落在窗内."""
    if lead_minutes <= 0:
        raise ValueError("lead_minutes 必须为正")
    begin = parse_hhmm(start)
    span = window_span_minutes(begin, parse_hhmm(end))
    return _from_minutes(_as_minutes(begin) + max(0, span - lead_minutes))


def hhmm_in_windows(label: str, windows: Sequence[ClockWindow]) -> bool:
    """判断一个 ``HH:MM`` 是否落在可报单窗内."""
    if not windows:
        return True
    clock = parse_hhmm(label)
    return any(window_contains(parse_hhmm(start), parse_hhmm(end), clock) for start, end in windows)


def rhythm_hhmm(
    windows: Sequence[ClockWindow],
    freq_minutes: int,
    offsets: Sequence[int] = (0,),
    *,
    clock_windows: Sequence[ClockWindow] | None = None,
) -> list[str]:
    """按频率把窗口切成可报单时刻.

    每个窗口开盘先打一枪，再每 ``freq_minutes`` 一格。格点必须市场钟仍开。
    频率打在 bar 窗右端点时：若市场钟在该时刻仍开则保留端点（期货 15:00），
    否则钳到前一分钟（11:29 / 14:59）。补发越出市场钟则丢弃。
    """
    if freq_minutes <= 0:
        raise ValueError("freq_minutes 必须为正")
    clock = tuple(clock_windows) if clock_windows is not None else tuple(windows)
    seen: list[str] = []
    known: set[str] = set()
    for start_text, end_text in windows:
        begin = parse_hhmm(start_text)
        span = window_span_minutes(begin, parse_hhmm(end_text))
        bases: list[int] = []
        step = 0
        while step < span:
            bases.append(step)
            step += freq_minutes
        if step == span:
            boundary = _from_minutes(_as_minutes(begin) + span)
            bases.append(span if hhmm_in_windows(boundary, clock) else span - 1)
        for base in bases:
            for offset in offsets:
                total = base + offset
                if total < 0:
                    continue
                label = _from_minutes(_as_minutes(begin) + total)
                if not hhmm_in_windows(label, clock) or label in known:
                    continue
                known.add(label)
                seen.append(label)
    return seen


__all__ = [
    "CN_FUTURES_WINDOWS",
    "CN_STOCK_WINDOWS",
    "ClockWindow",
    "bar_windows",
    "close_lead_hhmm",
    "day_windows",
    "hhmm_in_windows",
    "is_overnight_window",
    "is_within_schedule_windows",
    "last_tradable_hhmm",
    "overnight_windows",
    "parse_hhmm",
    "rhythm_hhmm",
    "window_contains",
    "window_span_minutes",
]
