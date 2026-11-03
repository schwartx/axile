"""OrderTracker 成交去重回归（F10），刻意不导入 CTP SDK。"""

from __future__ import annotations

from typing import Any

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder


class _Logger:
    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs


class _FakeExecutor:
    channel_type = TradeChannel.GM
    symbol = "rb2610"

    def __init__(self) -> None:
        self.logger = _Logger()
        self.audit_context: dict[str, Any] = {}

    def get_current_volume(self, account_assets: UnifiedAccountAssets) -> float:
        _ = account_assets
        return 0.0

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = direction, order_type, volume, price, kwargs
        raise NotImplementedError

    def get_pending_orders(self) -> list[UnifiedOrder]:
        return []

    def query_trades(self, order_id: str) -> list[object]:
        _ = order_id
        return []

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        return None

    def set_audit_context(self, context: dict[str, Any]) -> None:
        self.audit_context = dict(context)

    def next_audit_seq(self) -> int:
        return 1

    def register_order_audit_metadata(self, order_id: str, metadata: dict[str, Any]) -> None:
        _ = order_id, metadata

    def get_order_audit_metadata(self, order_id: str) -> dict[str, Any]:
        _ = order_id
        return {}

    def emit_audit_event(self, **kwargs: object) -> bool:
        _ = kwargs
        return True


def _pending_order(order_id: str = "o1", volume: float = 2) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=volume,
        price=3200,
        status=OrderStatus.PENDING,
        filled_volume=0.0,
        avg_price=0.0,
    )


def test_F10_distinct_same_second_trades_are_deduplicated() -> None:
    """有可信 TradeID 时，同秒同价同量的不同成交必须都保留（F10）。"""
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_pending_order())
    common = {
        "symbol": "rb2610",
        "order_id": "o1",
        "trade_time": "2026-09-09T09:01:01",
        "trade_volume": 1.0,
        "trade_price": 3200.0,
        "trade_value": 3200.0,
    }
    tracker.on_trade_record(TradeRecord(trade_id="100", **common))
    tracker.on_trade_record(TradeRecord(trade_id="101", **common))
    trades = tracker.get_all_trades()
    assert len(trades) == 2
    assert {trade.trade_id for trade in trades} == {"100", "101"}
    assert sum(trade.trade_volume for trade in trades) == 2.0


def test_F10_same_trade_id_is_still_deduplicated() -> None:
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_pending_order(volume=1))
    trade = TradeRecord(
        trade_id="100",
        symbol="rb2610",
        order_id="o1",
        trade_time="2026-09-09T09:01:01",
        trade_volume=1.0,
        trade_price=3200.0,
        trade_value=3200.0,
    )
    tracker.on_trade_record(trade)
    tracker.on_trade_record(trade.model_copy(deep=True))
    assert len(tracker.get_all_trades()) == 1


def test_F10_missing_trade_id_still_uses_time_price_volume_fallback() -> None:
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_pending_order(volume=1))
    common = {
        "symbol": "rb2610",
        "order_id": "o1",
        "trade_time": "2026-09-09T09:01:01",
        "trade_volume": 1.0,
        "trade_price": 3200.0,
        "trade_value": 3200.0,
    }
    tracker.on_trade_record(TradeRecord(trade_id="", **common))
    tracker.on_trade_record(TradeRecord(trade_id="   ", **common))
    assert len(tracker.get_all_trades()) == 1


def test_status_callbacks_preserve_order_creation_time():
    tracker = OrderTracker(executor=_FakeExecutor())
    original = _pending_order("creation-time")
    original.create_time = "2026-11-02T19:59:53.974000+08:00"
    tracker.add_order(original)
    canceled = original.model_copy(
        update={
            "status": OrderStatus.CANCELED,
            "create_time": "2026-11-02T20:00:54.661000+08:00",
            "update_time": "2026-11-02T20:00:54.424000+08:00",
        }
    )
    tracker.on_order_update(canceled)
    result = tracker.completed_orders[original.order_id]
    assert result.create_time == original.create_time
    assert result.update_time == canceled.update_time
    assert result.status == OrderStatus.CANCELED
