"""TARGET-POS-TASK 平仓失败不得反向开仓（F16），不导入 CTP SDK。"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import (
    _execute_position_adjustment,
    _extract_ctp_position_details,
)
from axile.executor.constants.order_status import OrderStatus
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _BaseExecutor:
    def __init__(self, *, long_yd: int = 0, short_yd: int = 0) -> None:
        self.channel_type = TradeChannel.CTP
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.long_today = 0
        self.long_yesterday = long_yd
        self.short_today = 0
        self.short_yesterday = short_yd
        self.orders: list[UnifiedOrder] = []

    def _position(self, direction: PositionDirection, today: int, yesterday: int) -> Position:
        side = "long" if direction == PositionDirection.LONG else "short"
        return Position(
            symbol=self.symbol,
            volume=today + yesterday,
            available_volume=today + yesterday,
            market_value=(today + yesterday) * 3200 * 10,
            direction=direction,
            avg_price=3200,
            extra={f"{side}_td": today, f"{side}_yd": yesterday},
        )

    def plan_close_orders(self, direction, volume, account_assets):
        return plan_futures_close_orders(self.symbol, direction, volume, account_assets, "SHFE")

    def get_account_assets(self) -> UnifiedAccountAssets:
        positions = []
        if self.long_today + self.long_yesterday:
            positions.append(self._position(PositionDirection.LONG, self.long_today, self.long_yesterday))
        if self.short_today + self.short_yesterday:
            positions.append(self._position(PositionDirection.SHORT, self.short_today, self.short_yesterday))
        return UnifiedAccountAssets(
            available_cash=1_000_000,
            total_asset=1_000_000,
            market_value=sum(position.market_value for position in positions),
            positions=positions,
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
            update_time="2026-08-23T09:00:00",
        )

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        offset = str(kwargs.get("offset_flag", "0"))
        if offset != "0":
            raise RuntimeError(f"synthetic close reject offset={offset}")
        order = UnifiedOrder(
            order_id=f"open-{len(self.orders) + 1}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.FILLED,
            filled_volume=volume,
            avg_price=price,
            extra={"offset_flag": offset},
        )
        self.orders.append(order)
        return order


def test_F16_failed_close_falls_through_to_open_opposite_position() -> None:
    """多 5 目标 0：平仓全部失败时不得发出 SELL OPEN（F16）。"""
    executor = _BaseExecutor(long_yd=5)
    detail = _extract_ctp_position_details(executor.get_account_assets(), ["rb2610"])["rb2610"]
    orders = _execute_position_adjustment(
        cast("ExecutorProtocol", executor),
        "rb2610",
        0,
        detail,
        executor.get_market_data(),
        "PASSIVE",
    )
    assert orders == []
    assert executor.orders == []
    assert any("停止后续开仓" in str(call) for call in executor.logger.error.call_args_list)


def test_F16_failed_close_short_does_not_open_long() -> None:
    """空头清仓失败时不得转为 BUY OPEN。"""
    executor = _BaseExecutor(short_yd=5)
    detail = _extract_ctp_position_details(executor.get_account_assets(), ["rb2610"])["rb2610"]
    orders = _execute_position_adjustment(
        cast("ExecutorProtocol", executor),
        "rb2610",
        0,
        detail,
        executor.get_market_data(),
        "PASSIVE",
    )
    assert orders == []
    assert executor.orders == []
    assert any("停止后续开仓" in str(call) for call in executor.logger.error.call_args_list)
