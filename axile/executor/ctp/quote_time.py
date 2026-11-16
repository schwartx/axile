"""CTP 夜盘日期兼容解析；不使用接收时刻猜测交易日期。"""

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta

from axile.executor.china_futures_session import is_regular_night_session_transition
from axile.executor.ctp_product_sessions import CtpProductSession


def resolve_quote_trading_day(
    native_time: str,
    trading_day: str,
    sessions: Sequence[CtpProductSession],
    calendar_is_open: Callable[[date], bool | None],
) -> str:
    """将郑商所晚盘的自然日字段转换为交易日；无法确认时返回空值。"""
    if not native_time or not sessions or sessions[0].exchange_id != "CZCE":
        return trading_day
    parsed = datetime.fromisoformat(native_time)
    if parsed.strftime("%Y%m%d") != trading_day or parsed.time() < time(17):
        return trading_day
    if not any(session.time_begin <= parsed.time() < session.time_end for session in sessions):
        return trading_day
    # 沿用现有夜盘规则：只接受隔日或周五至周一，不跨长假推断。
    following = parsed.date() + timedelta(days=3 if parsed.weekday() == 4 else 1)
    if _previous_open_day(following, calendar_is_open) != parsed.date():
        return ""
    return following.strftime("%Y%m%d")


def resolve_quote_time(
    native_time: str,
    trading_day: str,
    sessions: Sequence[CtpProductSession],
    calendar_is_open: Callable[[date], bool | None],
) -> tuple[str, str, str]:
    """返回 ISO 时间、状态和原因；不确定的异常夜盘日期返回空时间。"""
    if not native_time:
        return "", "unknown", "invalid_native_time"
    parsed = datetime.fromisoformat(native_time)
    if parsed.strftime("%Y%m%d") != trading_day:
        return native_time, "native", ""
    clock = parsed.time()
    if time(3) <= clock < time(17):
        return native_time, "native", ""
    if not sessions:
        return "", "unknown", "missing_product_sessions"
    night = [s for s in sessions if s.time_begin >= time(17) or s.time_end < s.time_begin]
    matches = [
        s
        for s in night
        if (
            s.time_begin <= clock < s.time_end
            if s.time_end > s.time_begin
            else clock >= s.time_begin or clock < s.time_end
        )
    ]
    if not matches:
        return native_time, "native", ""
    if len(matches) != 1:
        return "", "unknown", "ambiguous_night_session"
    start = _previous_open_day(parsed.date(), calendar_is_open)
    if start is None:
        return "", "unknown", "calendar_unavailable"
    if not is_regular_night_session_transition(start, parsed.date()):
        return "", "unknown", "non_regular_night_transition"
    natural_day = start + timedelta(days=int(clock < matches[0].time_begin))
    normalized = parsed.replace(year=natural_day.year, month=natural_day.month, day=natural_day.day)
    if normalized == parsed:
        return native_time, "native", ""
    return normalized.isoformat(), "normalized", "action_day_is_trading_day"


def _previous_open_day(day: date, calendar_is_open: Callable[[date], bool | None]) -> date | None:
    try:
        if calendar_is_open(day) is not True:
            return None
        for offset in range(1, 15):
            candidate = day - timedelta(days=offset)
            state = calendar_is_open(candidate)
            if state is True:
                return candidate
            if state is not False:
                return None
    except Exception:  # noqa: BLE001 - 日历故障不允许推断日期
        return None
    return None
