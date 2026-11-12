"""分别判定本次交易完成情况与渠道持仓核对结果。"""

from math import isclose, isfinite
from typing import TypedDict

from axile.domain.execution import ExecutionReasonFamily
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import ExecutionOutcome, ExecutionStatus, outcome_from_status
from axile.executor.models.unified_order import OrderDirection, TradeRecord, UnifiedOrder

TARGET_NOT_REACHED = "COMMON.TARGET.NOT_REACHED"


class OutcomeSummary(TypedDict):
    """算法结果的明确状态、错误与可序列化持仓字段；outcome 恒等于 f(status)。"""

    status: ExecutionStatus
    error: str | None
    final_volume: float | None
    start_volume: float | None
    expected_volume: float | None
    position_confirmed: bool | None
    outcome: ExecutionOutcome
    outcome_reason: str | None
    reason_code: str | None
    reason_family: ExecutionReasonFamily | None


def confirmed_position(start: float, orders: list[UnifiedOrder]) -> float | None:
    """用有效起点及去重后的终态累计成交推导仓位，证据不足时返回空值。"""
    if not isfinite(start) or not orders:
        return None
    unique: dict[str, UnifiedOrder] = {}
    for order in orders:
        if (
            not order.order_id
            or not order.is_completed()
            or order.status == OrderStatus.REJECTED
            or not isfinite(order.volume)
            or not isfinite(order.filled_volume)
            or not 0 <= order.filled_volume <= order.volume
            or order.direction not in {OrderDirection.BUY, OrderDirection.SELL}
            or order.status == OrderStatus.FILLED
            and order.filled_volume != order.volume
        ):
            return None
        previous = unique.get(order.order_id)
        if previous is not None and (previous.symbol, previous.direction, previous.volume) != (
            order.symbol,
            order.direction,
            order.volume,
        ):
            return None
        if previous is None or previous.filled_volume < order.filled_volume:
            unique[order.order_id] = order
    if len({order.symbol for order in unique.values()}) != 1:
        return None
    expected = start + sum(
        order.filled_volume if order.direction == OrderDirection.BUY else -order.filled_volume
        for order in unique.values()
    )
    return expected if isfinite(expected) else None


def summarize_outcome(
    start: float,
    final: float,
    target: float,
    orders: list[UnifiedOrder],
    trades: list[TradeRecord],
    explicit_error: str | None = None,
    explicit_blocked_error: str | None = None,
    *,
    error_reason_code: str | None = None,
    error_reason_family: ExecutionReasonFamily | None = None,
    blocked_reason_code: str | None = None,
    blocked_reason_family: ExecutionReasonFamily | None = None,
    position_error: str | None = None,
) -> OutcomeSummary:
    """以订单、成交、持仓和明确错误聚合结果，绝不扫描调试数据。"""
    terminal = all(OrderStatus.is_completed(order.status) for order in orders)
    # 进展 = 真实成交、持仓位移或尚未确认终态的挂单；已终态零成交的死单（拒单/零成交撤单）不算进展，
    # 落 FAILED——禁止把「提交过订单」稀释成 PARTIAL 告警。
    progress = (
        bool(trades)
        or any(order.filled_volume > 0 for order in orders)
        or (isfinite(final) and final != start)
        or (bool(orders) and not terminal)
    )
    rejected = any(order.status == OrderStatus.REJECTED for order in orders)
    expected = confirmed_position(start, orders)
    execution_final = expected if expected is not None else final
    position_confirmed = (
        isfinite(final) and isclose(final, expected, rel_tol=0, abs_tol=1e-9) if expected is not None else None
    )
    completed = isfinite(execution_final) and isclose(execution_final, target, rel_tol=0, abs_tol=1e-9)
    error = explicit_error or (position_error if expected is None else None)
    reason_code = error_reason_code if error else None
    reason_family = error_reason_family if error else None
    if rejected and error is None:
        error = "报单被拒绝"
    if not terminal and error is None:
        error = "订单最终状态尚未确认（含撤单结果未知）"
    if expected is None and (not isfinite(final) or position_error) and error is None:
        error = position_error or "最终持仓尚未确认"
    if completed and error is None:
        status = ExecutionStatus.NOOP if start == target and not orders and not trades else ExecutionStatus.SUCCEEDED
    elif error is None and not progress and not orders and explicit_blocked_error:
        # BLOCKED 只描述「从未报单」的受阻；死单（拒单/零成交撤单）走失败分支。
        status, error = ExecutionStatus.BLOCKED, explicit_blocked_error
        reason_code, reason_family = blocked_reason_code, blocked_reason_family
    else:
        status = ExecutionStatus.PARTIAL if progress else ExecutionStatus.FAILED
        if error is None:
            error = explicit_blocked_error or f"目标未完成: 当前持仓={execution_final}，目标持仓={target}"
            if explicit_blocked_error:
                reason_code, reason_family = blocked_reason_code, blocked_reason_family
            else:
                reason_code, reason_family = TARGET_NOT_REACHED, ExecutionReasonFamily.EXECUTION_STRATEGY
    return {
        "status": status,
        "error": error,
        "final_volume": final if isfinite(final) and position_confirmed is not False else None,
        "start_volume": start if isfinite(start) else None,
        "expected_volume": expected,
        "position_confirmed": position_confirmed,
        "outcome": outcome_from_status(status),
        "outcome_reason": error,
        "reason_code": reason_code,
        "reason_family": reason_family,
    }
