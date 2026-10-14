"""交易日历约束的盘前目录预热，不将午休或迟到任务当成新场次。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from axile.channels import get_channel
from axile.channels.schedule_clock import is_overnight_window, parse_hhmm
from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def preheat_window(current: datetime | None = None) -> tuple[str, float] | None:
    """返回有效盘前场次与截止时间，恰好距开盘三分钟仍允许。"""
    now = current or clock_now(tz=_SHANGHAI)
    now = now.astimezone(_SHANGHAI) if now.tzinfo else now.replace(tzinfo=_SHANGHAI)
    windows = get_channel(TradeChannel.CTP).descriptor.schedule.windows
    day = [parse_hhmm(w.start) for w in windows if not is_overnight_window(w.start, w.end)]
    night = [parse_hhmm(w.start) for w in windows if is_overnight_window(w.start, w.end)]
    for name, prepared, opens in (("day", "08:30", day), ("night", "20:30", night)):
        if not opens:
            continue
        opening = datetime.combine(now.date(), min(opens), tzinfo=_SHANGHAI)
        cutoff = opening - timedelta(minutes=3)
        begin = datetime.combine(now.date(), parse_hhmm(prepared), tzinfo=_SHANGHAI)
        if not begin <= now <= cutoff:
            continue
        decision = evaluate_channel_calendar_moment(TradeChannel.CTP, opening)
        if decision.status is not CalendarDecisionStatus.AVAILABLE_OPEN:
            logger.info("跳过目录预热 | session={} calendar={}", name, decision.status)
            return None
        return f"{opening.date().isoformat()}:{name}", cutoff.timestamp()
    return None


def arm_catalog_preheat() -> None:
    """来源尚未登录时只登记补偿，不阻塞 API 启动。"""
    from axile.server.execution.worker_backend.manager import get_worker_backend_manager

    window = preheat_window()
    if window is not None:
        get_worker_backend_manager().instrument_catalog.arm_refresh(*window)
