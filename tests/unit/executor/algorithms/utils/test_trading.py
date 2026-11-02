"""交易辅助函数测试。"""

from __future__ import annotations

import pytest

from axile.executor.algorithms.utils.trading import cancel_pending_orders_via_query
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


def _pending_order(order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=3200.0,
        status="SUBMITTED",
    )


class _Executor:
    def __init__(self, results: dict[str, bool | Exception]) -> None:
        self.results = results
        self.cancelled: list[str] = []

    def get_pending_orders(self) -> list[UnifiedOrder]:
        return [_pending_order(order_id) for order_id in self.results]

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        result = self.results[order_id]
        if isinstance(result, Exception):
            raise result
        return result


def test_cancel_pending_orders_propagates_ctp_session_recovery_without_continuing() -> None:
    executor = _Executor(
        {
            "order-recovery": CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2),
            "order-not-attempted": True,
        }
    )

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        cancel_pending_orders_via_query(executor)  # type: ignore[arg-type]

    assert executor.cancelled == ["order-recovery"]


def test_cancel_pending_orders_keeps_common_failure_aggregation() -> None:
    executor = _Executor({"order-failed": RuntimeError("cancel failed"), "order-cancelled": True})

    result = cancel_pending_orders_via_query(executor)  # type: ignore[arg-type]
    assert set(result.unconfirmed) == {("rb2610", "order-failed"), ("rb2610", "order-cancelled")}
    assert executor.cancelled == ["order-failed", "order-cancelled"]
