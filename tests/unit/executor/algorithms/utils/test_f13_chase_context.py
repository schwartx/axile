"""F13：追价上下文与换单开平/交易规则保留（不导入 openctp）。"""

from __future__ import annotations

from typing import Any

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.order_tracker import (
    ChaseConfig,
    OrderTracker,
    _chase_place_kwargs,
)
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


class _Logger:
    def debug(self, *a, **k):
        _ = a, k

    def info(self, *a, **k):
        _ = a, k

    def warning(self, *a, **k):
        _ = a, k

    def error(self, *a, **k):
        _ = a, k

    def exception(self, *a, **k):
        _ = a, k


class _FakeExecutor:
    channel_type = TradeChannel("gm")
    symbol = "rb2610"

    def __init__(self) -> None:
        self.logger = _Logger()
        self.place_calls: list[dict[str, Any]] = []
        self.audit_context: dict[str, Any] = {}

    def place_order(self, direction, order_type, volume, price=0, **kwargs):
        self.place_calls.append(
            {
                "direction": direction,
                "order_type": order_type,
                "volume": volume,
                "price": price,
                "kwargs": kwargs,
            }
        )
        return UnifiedOrder(
            order_id=f"new-{len(self.place_calls)}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.PENDING,
            filled_volume=0,
            avg_price=0,
            extra={"offset_flag": kwargs.get("offset_flag", "0")},
        )

    def get_pending_orders(self):
        return []

    def query_trades(self, order_id: str):
        _ = order_id
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
        _ = a, k

    def get_order_audit_metadata(self, order_id: str):
        _ = order_id
        return {}

    def emit_audit_event(self, **kwargs) -> bool:
        _ = kwargs
        return True

    def cancel_order(self, order_id: str) -> bool:
        _ = order_id
        return True


def _order(order_id: str = "o1", *, offset: str = "4", direction=OrderDirection.SELL) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol="rb2610",
        direction=direction,
        order_type=OrderType.LIMIT,
        volume=5,
        price=3200,
        status=OrderStatus.PENDING,
        filled_volume=0,
        avg_price=0,
        extra={"offset_flag": offset},
    )


def test_F13_add_order_creates_chase_context_with_offset_and_trade_rule() -> None:
    executor = _FakeExecutor()
    tracker = OrderTracker(
        executor=executor, chase_config=ChaseConfig(enabled=True, ticks=1, max_count=3, interval=0.1)
    )
    rule = {"price": "PASSIVE", "max_single_order_size": 2, "offset_priority": "昨今"}
    tracker.add_order(
        _order(),
        direction=OrderDirection.SELL,
        target_volume=0,
        current_volume=5,
        offset_flag="4",
        trade_rule=rule,
    )
    assert "o1" in tracker._chase_info
    info = tracker._chase_info["o1"]
    assert info["direction"] == OrderDirection.SELL
    assert info["offset_flag"] == "4"
    assert info["trade_rule"] == rule
    assert info["target_volume"] == 0
    assert info["current_volume"] == 5


def test_F13_add_order_falls_back_to_order_direction_and_extra_offset() -> None:
    executor = _FakeExecutor()
    tracker = OrderTracker(executor=executor, chase_config=ChaseConfig(enabled=True))
    tracker.add_order(_order(offset="3", direction=OrderDirection.BUY))
    info = tracker._chase_info["o1"]
    assert info["direction"] == OrderDirection.BUY
    assert info["offset_flag"] == "3"


def test_F13_chase_place_kwargs_preserves_open_close_and_trade_rule() -> None:
    kwargs = _chase_place_kwargs(
        {
            "position_side": "long",
            "offset_flag": "4",
            "trade_rule": {"max_single_order_size": 2},
        }
    )
    assert kwargs == {
        "position_side": "long",
        "offset_flag": "4",
        "trade_rule": {"max_single_order_size": 2},
    }


def test_F13_limit_reprice_uses_preserved_offset_and_trade_rule() -> None:
    """直接走换单价 place_order 路径所用的 kwargs 构造，确认不会丢开平。"""
    chase_info = {
        "symbol": "rb2610",
        "direction": OrderDirection.SELL,
        "offset_flag": "4",
        "trade_rule": {"price": "PASSIVE", "max_single_order_size": 2},
        "position_side": None,
        "chase_count": 0,
        "original_price": 3200.0,
    }
    executor = _FakeExecutor()
    kwargs = _chase_place_kwargs(chase_info)
    executor.place_order(OrderDirection.SELL, OrderType.LIMIT, 3, 3199, **kwargs)
    assert executor.place_calls[0]["kwargs"]["offset_flag"] == "4"
    assert executor.place_calls[0]["kwargs"]["trade_rule"]["max_single_order_size"] == 2


def test_F13_market_fallback_kwargs_require_offset_not_default_open() -> None:
    kwargs = _chase_place_kwargs({"offset_flag": "3", "trade_rule": {"offset_priority": "今昨"}})
    assert kwargs["offset_flag"] == "3"
    assert "position_side" not in kwargs
