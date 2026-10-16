"""F17：TARGET-POS-TASK 按单笔上限拆单（不导入 openctp）。"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import (
    CTPPositionDetail,
    _execute_position_adjustment,
)
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _SplitExecutor:
    def __init__(self) -> None:
        self.channel_type = TradeChannel.CTP
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.orders: list[UnifiedOrder] = []

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1_000_000,
            total_asset=1_000_000,
            market_value=0,
            positions=[],
        )

    def get_market_data(self) -> UnifiedPriceData:
        return UnifiedPriceData(
            symbol=self.symbol,
            last_price=3200,
            bid_price=3199,
            ask_price=3201,
            bid_volume=10,
            ask_volume=10,
            volume=100,
            turnover=3_200_000,
            timestamp=1,
            update_time="2026-09-09T09:00:00",
        )

    def get_max_order_volume(self, order_type=OrderType.LIMIT, trade_rule=None):
        _ = order_type
        if trade_rule and trade_rule.get("max_single_order_size") is not None:
            return int(trade_rule["max_single_order_size"])
        return None

    def get_order_volume_bounds(self, order_type=OrderType.LIMIT, trade_rule=None):
        return 1, self.get_max_order_volume(order_type, trade_rule)

    def place_order(self, direction, order_type, volume, price=0, **kwargs):
        limit = self.get_max_order_volume(order_type, kwargs.get("trade_rule"))
        assert limit is None or volume <= limit
        order = UnifiedOrder(
            order_id=f"o-{len(self.orders) + 1}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.SUBMITTED,
            filled_volume=0,
            avg_price=0,
            extra={"offset_flag": str(kwargs.get("offset_flag", "0"))},
        )
        self.orders.append(order)
        return order


def test_F17_target_open_splits_by_max_single_order_size() -> None:
    executor = _SplitExecutor()
    flat = CTPPositionDetail("rb2610")
    orders = _execute_position_adjustment(
        cast("ExecutorProtocol", executor),
        "rb2610",
        50,
        flat,
        executor.get_market_data(),
        "PASSIVE",
        trade_rule={"max_single_order_size": 2},
    )
    assert len(orders) == 25
    assert all(order.volume == 2 for order in orders)
    assert sum(order.volume for order in orders) == 50
    assert all(order.direction == OrderDirection.BUY for order in orders)
    assert all(order.extra["offset_flag"] == "0" for order in orders)


def test_target_rebalances_minimum_and_preserves_partial_submission(monkeypatch):
    from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import _place_volume_slices

    executor = _SplitExecutor()
    monkeypatch.setattr(executor, "get_order_volume_bounds", lambda *_: (3, 10))
    orders, submitted = _place_volume_slices(executor, OrderDirection.BUY, 12, 3200, offset_flag="0", trade_rule=None)
    assert [order.volume for order in orders] == [9, 3]
    assert submitted == 12
    original_place = executor.place_order
    count = 0

    def place(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("offline failure")
        return original_place(*args, **kwargs)

    monkeypatch.setattr(executor, "place_order", place)
    orders, submitted = _place_volume_slices(executor, OrderDirection.BUY, 30, 3200, offset_flag="0", trade_rule=None)
    assert count == 2
    assert len(orders) == 1
    assert submitted == 10
    assert executor.logger.warning.called


def test_target_session_recovery_is_not_swallowed(monkeypatch):
    import pytest

    from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import _submit_close_leg
    from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired

    executor = _SplitExecutor()

    def fail(*_args, **_kwargs):
        raise CtpSessionRecoveryRequired("offline disconnect")

    monkeypatch.setattr(executor, "place_order", fail)
    with pytest.raises(CtpSessionRecoveryRequired):
        _submit_close_leg(
            executor, executor.symbol, OrderDirection.BUY.value, 3, 3200, "3", "start", "ok", "failed {error}"
        )
