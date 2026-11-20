"""查询最终持仓失败时保留订单证据，不把未知仓位当成零。"""

from collections.abc import Callable
from math import isfinite

from axile.common.serialization import to_payload
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.algorithms.utils.outcome import confirmed_position
from axile.executor.models.unified_account_assets import UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.termination import ExecutionTerminated


def _checkpoint_order(order: UnifiedOrder) -> dict[str, object]:
    return to_payload(
        order,
        mode="json",
        include={"order_id", "symbol", "direction", "order_type", "volume", "price", "status", "filled_volume"},
    )


def save_order_checkpoint(executor: ExecutorProtocol, order: UnifiedOrder, start: float | None) -> bool:
    """只写变化的委托。首笔委托同时记录阶段起始仓位。"""
    details: dict[str, object] = {"orders": [_checkpoint_order(order)]}
    if start is not None:
        details["start_volume"] = start
    return executor.emit_audit_event(
        event_type=ExecutionEventType.POSITION_CHECKPOINT,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
        reason_code="COMMON.POSITION.UNCONFIRMED",
        symbol=executor.symbol,
        order_id=order.order_id,
        details=details,
    )


def save_position_checkpoint(
    executor: ExecutorProtocol, start: float | None, orders: list[UnifiedOrder] | None
) -> bool:
    """在可能阻塞的查询或撤单前，保存重启后核对仓位所需的订单证据。"""
    if (
        start is not None
        and isfinite(start)
        and orders
        and getattr(executor, "has_audit_sink", False) is True
        and executor.audit_context.get("execution_id")
    ):
        expected = confirmed_position(start, orders) if any(order.filled_volume for order in orders) else None
        return executor.emit_audit_event(
            event_type=ExecutionEventType.POSITION_CHECKPOINT,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
            reason_code="COMMON.POSITION.UNCONFIRMED",
            symbol=executor.symbol,
            details={
                "start_volume": start,
                "expected_volume": expected,
                "orders": [_checkpoint_order(order) for order in orders],
            },
        )
    return True


def read_final_position(
    executor: ExecutorProtocol,
    quantity: Callable[[UnifiedAccountAssets], float],
    *,
    start: float | None = None,
    orders: list[UnifiedOrder] | None = None,
) -> tuple[UnifiedAccountAssets, float, str | None]:
    """返回资产、持仓与明确查询错误；原始异常由日志保留。"""
    if not save_position_checkpoint(executor, start, orders):
        return UnifiedAccountAssets.unavailable(), float("nan"), "待核对仓位保存失败"
    try:
        assets = executor.get_account_assets()
        if is_degraded_snapshot_source(assets.source):
            return assets, float("nan"), "最终持仓尚未确认"
        return assets, quantity(assets), None
    except (ExecutionTerminated, MemoryError):
        raise
    except Exception as exc:  # noqa: BLE001 - 渠道 SDK 异常不一定继承 RuntimeError
        if getattr(exc, "requires_session_recovery", False):
            raise
        executor.logger.exception("最终持仓查询失败")
        return UnifiedAccountAssets.unavailable(), float("nan"), execution_error_message(exc, "最终持仓查询")
