"""在数据库事务之外评估渠道 SDK 日历，一次提交完整日期区间。"""

from datetime import date, timedelta

from axile.server.native_store import native_operation
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_day


async def ensure_calendar_facts(trade_channel: str | None, first_day: str | None, last_day: str | None) -> int:
    """重评估账户观测区间；共享渠道的其他日期和业务源版本保持不变。"""
    if trade_channel is None or first_day is None or last_day is None:
        return 0
    current, last = date.fromisoformat(first_day), date.fromisoformat(last_day)
    rows = []
    terminal_status: str | None = None
    while current <= last:
        calendar_id = label = None
        if terminal_status is not None:
            status = terminal_status
        else:
            try:
                decision = evaluate_channel_calendar_day(trade_channel, current)
            except KeyError:
                status = terminal_status = "unregistered"
            else:
                status = decision.status
                calendar_id, label = decision.calendar_id, decision.label
                if status is CalendarDecisionStatus.NOT_REQUIRED:
                    status = terminal_status = CalendarDecisionStatus.NOT_REQUIRED
                    calendar_id = label = None
        rows.append({"day": current.isoformat(), "status": str(status), "calendar_id": calendar_id, "label": label})
        current += timedelta(days=1)
    if not rows:
        return 0
    return await native_operation("analysis_calendar_save", {"channel": str(trade_channel), "days": rows})
