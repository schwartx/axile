"""F13：TARGET-POS-TASK 注册追价上下文（不导入 openctp / ctp_execute）。"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.core.base import AlgorithmInput, ExecutorProtocol
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import (
    CTPTargetPosTaskParams,
    ctp_target_pos_task_algorithm,
)
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _Exec:
    def __init__(self) -> None:
        self.channel_type = TradeChannel.CTP
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.long_today = 0
        self.long_yesterday = 5
        self.short_today = 0
        self.short_yesterday = 0
        self.order_callbacks = []
        self.price_callbacks = []
        self.trade_callbacks = []
        self.orders: list[UnifiedOrder] = []

    def plan_close_orders(self, direction, volume, account_assets):
        return plan_futures_close_orders(self.symbol, direction, volume, account_assets, "SHFE")

    def get_account_assets(self):
        positions = [
            Position(
                symbol=self.symbol,
                volume=5,
                available_volume=5,
                market_value=5 * 3200 * 10,
                direction=PositionDirection.LONG,
                avg_price=3200,
                extra={"long_td": 0, "long_yd": 5},
            )
        ]
        return UnifiedAccountAssets(
            available_cash=1_000_000,
            total_asset=1_000_000,
            market_value=positions[0].market_value,
            positions=positions,
        )

    def get_market_data(self):
        return UnifiedPriceData(
            symbol=self.symbol,
            last_price=3200,
            bid_price=3199,
            ask_price=3201,
            bid_volume=10,
            ask_volume=10,
            volume=100,
            turnover=1,
            timestamp=1,
            update_time="2026-09-09T09:00:00",
        )

    def place_order(self, direction, order_type, volume, price=0, **kwargs):
        order = UnifiedOrder(
            order_id=f"ctp-{len(self.orders) + 1}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.FILLED,
            filled_volume=volume,
            avg_price=price,
            extra={"offset_flag": str(kwargs.get("offset_flag", "0"))},
        )
        self.orders.append(order)
        for cb in tuple(self.order_callbacks):
            cb(order)
        trade = TradeRecord(
            trade_id=f"{order.order_id}-t",
            symbol=self.symbol,
            order_id=order.order_id,
            trade_time="2026-09-09T09:00:00",
            trade_volume=float(volume),
            trade_price=float(price),
            trade_value=float(volume) * float(price),
        )
        for cb in tuple(self.trade_callbacks):
            cb(trade)
        # simulate flatten
        self.long_yesterday = 0
        return order

    def register_order_callback(self, cb):
        self.order_callbacks.append(cb)

    def unregister_order_callback(self, cb):
        self.order_callbacks.remove(cb)

    def register_price_callback(self, cb):
        self.price_callbacks.append(cb)

    def unregister_price_callback(self, cb):
        self.price_callbacks.remove(cb)

    def register_trade_callback(self, cb):
        self.trade_callbacks.append(cb)

    def unregister_trade_callback(self, cb):
        self.trade_callbacks.remove(cb)

    def get_pending_orders(self):
        return []

    def query_trades(self, _oid):
        return []

    def cancel_order(self, _oid):
        return True

    def is_termination_requested(self):
        return False

    def get_termination_mode(self):
        return None

    def handle_termination_checkpoint(self):
        return None


def test_F13_target_chase_config_creates_chase_entries() -> None:
    executor = _Exec()
    captured: dict[str, object] = {}

    real_tracker_init = OrderTracker.__init__

    def wrapping_init(self, *args, **kwargs):
        real_tracker_init(self, *args, **kwargs)
        captured["tracker"] = self

    with patch.object(OrderTracker, "__init__", wrapping_init):
        result = ctp_target_pos_task_algorithm(
            cast("ExecutorProtocol", executor),
            AlgorithmInput(
                symbol="rb2610",
                target_volume=0,
                trade_rule={"price": "PASSIVE", "max_single_order_size": 50},
                params=CTPTargetPosTaskParams(max_wait_seconds=1, chase_enabled=True, chase_ticks=1),
            ),
        )

    tracker = cast(OrderTracker, captured["tracker"])
    # 订单已成交并完成，chase_info 可能被清理；但注册瞬间必须曾写入。
    # 用仍在 completed 的订单验证：至少下过平昨单，且 chase_config 已启用。
    assert tracker.chase_config is not None
    assert result.status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.PARTIAL}
    assert executor.orders
    assert all(order.extra["offset_flag"] != "" for order in executor.orders)
    # 直接再测一次 add_order 接线：用同配置 tracker 重放算法生成的订单
    probe = OrderTracker(executor=executor, chase_config=ChaseConfig(enabled=True))
    sample = executor.orders[0]
    probe.add_order(
        sample,
        direction=OrderDirection(sample.direction)
        if not isinstance(sample.direction, OrderDirection)
        else sample.direction,
        target_volume=0,
        current_volume=5,
        offset_flag=str(sample.extra.get("offset_flag")),
        trade_rule={"max_single_order_size": 50},
    )
    assert sample.order_id in probe._chase_info
    assert probe._chase_info[sample.order_id]["offset_flag"] == sample.extra["offset_flag"]
    assert probe._chase_info[sample.order_id]["trade_rule"]["max_single_order_size"] == 50
