"""股票（GM）阶段边界，供调度与 SDK 调用共用。"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from axile.executor.order_cancellation import CancelNotAllowedError

SHANGHAI = ZoneInfo("Asia/Shanghai")
GM_REGULAR_WINDOWS = (("09:30", "11:30"), ("13:00", "14:57"))
GM_SPECIAL_WINDOWS = {
    "opening_cancelable": ("09:15", "09:20", "早盘集合竞价 · 可撤单"),
    "opening_locked": ("09:20", "09:25", "早盘集合竞价 · 不可撤单"),
    "closing_auction": ("14:57", "15:00", "收盘集合竞价"),
    "after_hours": ("15:05", "15:30", "盘后定价交易"),
}
AUCTION_PHASES = frozenset(GM_SPECIAL_WINDOWS) - {"after_hours"}


def gm_phase(now: datetime) -> str:
    """按北京时间返回左闭右开的交易阶段。"""
    local = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    clock = local.strftime("%H:%M")
    for phase, (start, end, _) in GM_SPECIAL_WINDOWS.items():
        if start <= clock < end:
            return phase
    if "09:25" <= clock < "09:30":
        return "opening_wait"
    for index, (start, end) in enumerate(GM_REGULAR_WINDOWS):
        if start <= clock < end:
            return f"continuous_{index}"
    return "closed"


def check_gm_operation(
    now: datetime,
    *,
    cancel: bool = False,
    limit: bool = True,
    context: dict[str, Any] | None = None,
) -> None:
    """在发起及实际调用 SDK 时检查权限；查询与回报不经过此门控。"""
    phase = gm_phase(now)
    if cancel:
        if phase == "opening_cancelable" or phase.startswith("continuous_"):
            return
        raise CancelNotAllowedError("当前股票交易阶段不允许撤单，保留原委托并继续核实终态")
    if context and phase != context.get("phase"):
        raise ValueError("本次股票调仓所属交易阶段已结束，跳过下单")
    if context and context.get("trading_day") != now.astimezone(SHANGHAI).date().isoformat():
        raise ValueError("本次股票调仓所属交易日已结束，跳过下单")
    auction = context is not None and context.get("phase") in AUCTION_PHASES
    if auction and not limit:
        raise ValueError("本次集合竞价执行仅允许限价单")
    if phase.startswith("continuous_"):
        return
    if phase in AUCTION_PHASES and limit and auction:
        if context and context.get("sessions", {}).get(phase, {}).get("enabled"):
            return
    raise ValueError("当前股票交易阶段不允许本次下单")


def gm_session_context(sessions: dict[str, Any], now: datetime) -> dict[str, Any]:
    """冻结一次自动调仓的阶段和设置。"""
    local = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    phase = gm_phase(local)
    context: dict[str, Any] = {"phase": phase, "trading_day": local.date().isoformat(), "sessions": sessions}
    check_gm_operation(local, context=context)
    return context
