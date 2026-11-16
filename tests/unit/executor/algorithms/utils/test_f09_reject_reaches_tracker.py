"""F09：拒单终态进入 OrderTracker 后 pending 收敛（不导入 openctp）。"""

from __future__ import annotations

from typing import Any

from axile.common.serialization import copy_data
from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


class _Exec:
    channel_type = TradeChannel("gm")
    symbol = "rb2610"

    def __init__(self) -> None:
        self.logger = type(
            "L", (), {n: lambda *a, **k: None for n in ("debug", "info", "warning", "error", "exception")}
        )()
        self.audit_context: dict[str, Any] = {}

    def place_order(self, *a, **k):
        raise NotImplementedError

    def get_pending_orders(self):
        return []

    def query_trades(self, order_id: str):
        return []

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self):
        return None

    def handle_termination_checkpoint(self) -> None:
        return None

    def set_audit_context(self, context):
        self.audit_context = dict(context)

    def next_audit_seq(self) -> int:
        return 1

    def register_order_audit_metadata(self, *a, **k):
        return None

    def get_order_audit_metadata(self, order_id: str):
        return {}

    def emit_audit_event(self, **kwargs) -> bool:
        return True

    def cancel_order(self, order_id: str) -> bool:
        return True

    def reconcile_terminal_order(self, symbol: str, order_id: str):
        # 薄对账：查不到返回 None，不伪造成撤
        _ = symbol, order_id
        return None


def test_F09_async_insert_rejection_reaches_tracker() -> None:
    executor = _Exec()
    tracker = OrderTracker(executor=executor)
    pending = UnifiedOrder(
        order_id="o1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=3200,
        status=OrderStatus.PENDING,
        filled_volume=0,
        avg_price=0,
    )
    tracker.add_order(pending)
    assert tracker.get_pending_count() == 1

    rejected = copy_data(
        pending, update={"status": OrderStatus.REJECTED, "extra": {"error_id": 31, "error_msg": "synthetic reject"}}
    )
    tracker.on_order_update(rejected)
    assert tracker.get_pending_count() == 0
    assert "o1" in tracker.completed_orders
    assert tracker.completed_orders["o1"].status == OrderStatus.REJECTED


def test_F09_reconcile_missing_does_not_invent_cancel() -> None:
    executor = _Exec()
    tracker = OrderTracker(executor=executor)
    pending = UnifiedOrder(
        order_id="o1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=3200,
        status=OrderStatus.PENDING,
        filled_volume=0,
        avg_price=0,
    )
    tracker.add_order(pending)
    tracker._reconcile_missing_pending([])
    assert tracker.get_pending_count() == 1
