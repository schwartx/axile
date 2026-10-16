from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.algorithms.core.base import AlgorithmInput, ExecutorProtocol
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import (
    CTPTargetPosTaskParams,
    _calculate_order_price,
    _extract_ctp_position_details,
    _place_volume_slices,
    ctp_target_pos_task_algorithm,
)
from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.converters import account_to_unified
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.termination import ExecutionTerminated


def test_ctp_split_failure_keeps_already_submitted_order():
    executor = MagicMock()
    executor.get_order_volume_bounds.return_value = (1, 1)
    order = UnifiedOrder(
        order_id="first",
        symbol="A",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=10,
        status=OrderStatus.PENDING,
    )
    executor.place_order.side_effect = [order, RuntimeError("native reject")]
    tracker = MagicMock()
    orders, submitted = _place_volume_slices(
        executor, OrderDirection.BUY, 2, 10, offset_flag="0", trade_rule={}, tracker=tracker
    )
    assert orders == [order]
    assert submitted == 1
    tracker.add_order.assert_called_once()
    assert tracker.explicit_error == "报单失败，具体原因未确认"


def _native_book(reverse_rows=False, reverse_positions=False, split=False):
    rows = []
    for direction, total, today in (("2", 5, 2), ("3", 3, 1)):
        quantities = [(today, today), (total - today, 0)] if split else [(total, today)]
        for volume, td_volume in quantities:
            rows.append(
                {
                    "InstrumentID": "rb2610",
                    "PosiDirection": direction,
                    "Position": volume,
                    "TodayPosition": td_volume,
                    "YdPosition": total + 4,
                }
            )
    if reverse_rows:
        rows.reverse()
    book = account_to_unified({}, rows, {})
    if reverse_positions:
        book.positions.reverse()
    return book


@pytest.mark.parametrize("reverse_rows", [False, True])
@pytest.mark.parametrize("reverse_positions", [False, True])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("target", [2, 4])
def test_production_positions_aggregate_and_reach_target(reverse_rows, reverse_positions, split, target):
    book = _native_book(reverse_rows, reverse_positions, split)
    detail = _extract_ctp_position_details(book, ["rb2610"])["rb2610"]
    assert (detail.long_total, detail.short_total, detail.net_position) == (5, 3, 2)
    assert detail.net_position == AbstractExecutorCapabilityMixin().get_current_volume("rb2610", book)
    assert (detail.long_today, detail.long_yesterday, detail.short_today, detail.short_yesterday) == (2, 3, 1, 2)
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_account_assets = lambda: book
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=target,
            trade_rule={},
            params=CTPTargetPosTaskParams(max_wait_seconds=1),
        ),
    )
    assert result.status.value == ("NOOP" if target == 2 else "PARTIAL")
    assert result.final_volume == 2
    assert result.status == (ExecutionStatus.NOOP if target == 2 else ExecutionStatus.PARTIAL)
    assert result.memory["current_net_position"] == result.memory["final_net_position"] == 2
    assert result.memory["execution_details"]["rb2610_adjustment"]["current_net"] == 2
    assert [(order.direction, order.volume, order.extra["offset_flag"]) for order in executor.orders] == (
        [] if target == 2 else [(OrderDirection.BUY, 2, "4")]
    )


def test_final_check_does_not_accept_first_direction_as_net():
    book = _native_book()
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_account_assets = lambda: book
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=5,
            trade_rule={},
            params=CTPTargetPosTaskParams(max_wait_seconds=1),
        ),
    )
    assert result.status == ExecutionStatus.PARTIAL
    assert result.memory["final_net_position"] == 2
    assert result.memory["target_reached"] is False
    assert result.status.value == "PARTIAL"


@pytest.mark.parametrize("indices,expected", [([], (0, 0, 0)), ([0], (5, 0, 5)), ([1], (0, 3, -3))])
def test_position_aggregation_boundaries(indices, expected):
    book = _native_book()
    selected = [book.positions[index] for index in indices]
    selected.append(book.positions[0].model_copy(update={"symbol": "other"}))
    book.positions = selected
    detail = _extract_ctp_position_details(book, ["rb2610"])["rb2610"]
    assert (detail.long_total, detail.short_total, detail.net_position) == expected


@pytest.mark.parametrize(
    "extra",
    [
        {"long_td": 6, "long_yd": 0},
        {"long_td": 2, "long_yd": 2},
        {"long_td": -1, "long_yd": 6},
        {"long_td": 2.5, "long_yd": 2.5},
        {"long_td": 2, "long_yd": 3, "long_total": 8},
        {},
    ],
)
def test_inconsistent_position_blocks_orders(extra):
    book = _native_book()
    book.positions[0].extra = extra
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_account_assets = lambda: book
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=8,
            trade_rule={},
            params=CTPTargetPosTaskParams(max_wait_seconds=1),
        ),
    )
    assert result.status == ExecutionStatus.FAILED
    assert result.error
    assert executor.orders == []


def test_converter_rejects_today_exceeding_total():
    with pytest.raises(ValueError, match="今仓与总持仓不一致"):
        account_to_unified(
            {}, [{"InstrumentID": "rb2610", "PosiDirection": "2", "Position": 1, "TodayPosition": 2}], {}
        )


@pytest.mark.parametrize("field", ["Position", "TodayPosition"])
@pytest.mark.parametrize("value", [-1, 0.5, float("nan"), float("inf"), "bad", None, True])
def test_converter_rejects_invalid_position_quantity(field, value):
    row = {"InstrumentID": "rb2610", "PosiDirection": "2", "Position": 5, "TodayPosition": 2}
    row[field] = value
    with pytest.raises(ValueError, match="持仓数量必须是非负整数"):
        account_to_unified({}, [row], {})


class _FuturesExecutor:
    def __init__(self, channel_type: TradeChannel) -> None:
        self.channel_type = channel_type
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.long_today = 0
        self.long_yesterday = 0
        self.short_today = 1
        self.short_yesterday = 1
        self.order_callbacks: list[object] = []
        self.price_callbacks: list[object] = []
        self.trade_callbacks: list[object] = []
        self.orders: list[UnifiedOrder] = []

    def plan_close_orders(self, direction, volume, account_assets):
        policy = getattr(self, "close_policy", FuturesClosePolicy())
        priority = policy.overrides.get("RB", policy.fallback)
        return plan_futures_close_orders(self.symbol, direction, volume, account_assets, "SHFE", priority=priority)

    def _position(self, direction: PositionDirection, today: int, yesterday: int) -> Position:
        side = "long" if direction == PositionDirection.LONG else "short"
        return Position(
            symbol=self.symbol,
            volume=today + yesterday,
            available_volume=today + yesterday,
            market_value=(today + yesterday) * 3200 * 10,
            direction=direction,
            avg_price=3200,
            extra={
                f"{side}_td": today,
                f"{side}_yd": yesterday,
            },
        )

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
        lots = int(volume)
        offset = str(kwargs.get("offset_flag", "0"))
        if offset == "4":
            self.short_yesterday -= lots
        elif offset == "3":
            self.short_today -= lots
        elif offset == "0" and direction is OrderDirection.BUY:
            self.long_today += lots
        order = UnifiedOrder(
            order_id=f"{self.channel_type}-{len(self.orders) + 1}",
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
        for callback in tuple(self.order_callbacks):
            callback(order)  # type: ignore[operator]
        trade = TradeRecord(
            trade_id=f"{order.order_id}-t1",
            symbol=self.symbol,
            order_id=order.order_id,
            trade_time="2026-08-23T09:00:00",
            trade_volume=float(volume),
            trade_price=float(price),
            trade_value=float(volume) * float(price),
        )
        for callback in tuple(self.trade_callbacks):
            callback(trade)  # type: ignore[operator]
        return order

    def register_order_callback(self, callback: object) -> None:
        self.order_callbacks.append(callback)

    def unregister_order_callback(self, callback: object) -> None:
        self.order_callbacks.remove(callback)

    def register_price_callback(self, callback: object) -> None:
        self.price_callbacks.append(callback)

    def unregister_price_callback(self, callback: object) -> None:
        self.price_callbacks.remove(callback)

    def register_trade_callback(self, callback: object) -> None:
        self.trade_callbacks.append(callback)

    def unregister_trade_callback(self, callback: object) -> None:
        self.trade_callbacks.remove(callback)

    def get_pending_orders(self) -> list[UnifiedOrder]:
        return []

    def query_trades(self, _order_id: str) -> list[object]:
        return []

    def cancel_order(self, _order_id: str) -> bool:
        return True

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        return None


@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
def test_target_pos_task_uses_same_close_then_open_flow(channel: TradeChannel) -> None:
    executor = _FuturesExecutor(channel)
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1,
        trade_rule={"price": "PASSIVE"},
        params=CTPTargetPosTaskParams(max_wait_seconds=1),
    )

    result = ctp_target_pos_task_algorithm(cast("ExecutorProtocol", executor), algorithm_input)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.memory["target_reached"] is True
    assert [order.extra["offset_flag"] for order in executor.orders] == ["4", "3", "0"]
    assert executor.short_yesterday == executor.short_today == 0
    assert executor.long_today == 1
    assert executor.order_callbacks == []
    assert executor.trade_callbacks == []
    assert len(result.trades) == len(executor.orders) == 3
    assert result.memory["trades_generated"] == 3
    assert [trade.order_id for trade in result.trades] == [order.order_id for order in executor.orders]


def test_target_pos_task_params_expose_execution_defaults() -> None:
    params = CTPTargetPosTaskParams()

    assert params.model_dump() == {
        "chase_enabled": False,
        "chase_ticks": 1,
        "max_chase_count": 5,
        "chase_interval": 5.0,
        "max_wait_seconds": 60,
        "price_strategy": "PASSIVE",
        "close_order_type": "LIMIT",
        "open_order_type": "LIMIT",
    }


@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
def test_target_pos_task_market_close_keeps_offsets_and_limit_open(channel: TradeChannel) -> None:
    executor = _FuturesExecutor(channel)
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=1,
            trade_rule={},
            params=CTPTargetPosTaskParams(close_order_type="MARKET", chase_enabled=True, max_wait_seconds=1),
        ),
    )

    assert result.status == ExecutionStatus.SUCCEEDED
    assert [(order.order_type, order.price, order.extra["offset_flag"]) for order in executor.orders] == [
        (OrderType.MARKET, 0, "4"),
        (OrderType.MARKET, 0, "3"),
        (OrderType.LIMIT, 3199, "0"),
    ]
    assert result.memory["execution_details"]["rb2610_adjustment"]["close_order_type"] == "MARKET"


@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
def test_target_pos_task_market_open_does_not_require_quote(channel: TradeChannel) -> None:
    executor = _FuturesExecutor(channel)
    executor.short_today = executor.short_yesterday = 0
    executor.get_market_data = lambda: None
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=1,
            trade_rule={},
            params=CTPTargetPosTaskParams(open_order_type="MARKET", max_wait_seconds=1),
        ),
    )

    assert result.status == ExecutionStatus.SUCCEEDED
    assert [(order.order_type, order.price, order.extra["offset_flag"]) for order in executor.orders] == [
        (OrderType.MARKET, 0, "0")
    ]


def test_target_pos_task_missing_quote_fails_limit_without_order() -> None:
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_market_data = lambda: None
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(symbol="rb2610", target_volume=1, trade_rule={}, params=CTPTargetPosTaskParams()),
    )

    assert result.status == ExecutionStatus.FAILED
    assert "缺少有效行情" in (result.error or "")
    assert executor.orders == []


def test_target_pos_task_market_close_then_missing_quote_stops_limit_open() -> None:
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_market_data = lambda: None
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610",
            target_volume=1,
            trade_rule={},
            params=CTPTargetPosTaskParams(close_order_type="MARKET", max_wait_seconds=1),
        ),
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert "开仓缺少有效行情" in (result.error or "")
    assert [order.extra["offset_flag"] for order in executor.orders] == ["4", "3"]
    assert all(order.order_type == OrderType.MARKET for order in executor.orders)


def test_target_pos_task_market_slices_use_market_bounds() -> None:
    executor = MagicMock()
    executor.get_order_volume_bounds.return_value = (1, 2)
    executor.place_order.side_effect = [
        UnifiedOrder(
            order_id=str(index),
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            volume=2,
            price=0,
            status=OrderStatus.PENDING,
        )
        for index in range(2)
    ]
    tracker = MagicMock()
    orders, submitted = _place_volume_slices(
        executor,
        OrderDirection.BUY,
        4,
        0,
        offset_flag="4",
        trade_rule={},
        tracker=tracker,
        order_type=OrderType.MARKET,
    )

    assert submitted == 4
    assert len(orders) == 2
    executor.get_order_volume_bounds.assert_called_once_with(OrderType.MARKET, {})
    assert all(call.kwargs["chase_enabled"] is False for call in tracker.add_order.call_args_list)
    assert [call.kwargs["details"]["order"]["order_type"] for call in executor.emit_audit_event.call_args_list] == [
        "MARKET",
        "MARKET",
    ]


@pytest.mark.parametrize(
    ("price_strategy", "direction", "expected"),
    [
        ("PASSIVE", OrderDirection.BUY, 3199),
        ("PASSIVE", OrderDirection.SELL, 3201),
        ("ACTIVE", OrderDirection.BUY, 3201),
        ("ACTIVE", OrderDirection.SELL, 3199),
    ],
)
def test_target_pos_task_price_strategy_uses_expected_book_side(
    price_strategy: str,
    direction: OrderDirection,
    expected: float,
) -> None:
    market_data = _FuturesExecutor(TradeChannel.CTP).get_market_data()

    assert _calculate_order_price(market_data, price_strategy, direction) == expected


def test_target_pos_task_active_price_falls_back_to_last_price() -> None:
    market_data = _FuturesExecutor(TradeChannel.CTP).get_market_data().model_copy(update={"ask_price": 0})

    assert _calculate_order_price(market_data, "ACTIVE", OrderDirection.BUY) == 3200


@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
def test_target_pos_task_uses_account_close_policy(channel: TradeChannel) -> None:
    executor = _FuturesExecutor(channel)
    executor.close_policy = FuturesClosePolicy(fallback="today_first")
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1,
        trade_rule={},
        params=CTPTargetPosTaskParams(
            max_wait_seconds=1,
            price_strategy="ACTIVE",
        ),
    )

    result = ctp_target_pos_task_algorithm(cast("ExecutorProtocol", executor), algorithm_input)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert [order.extra["offset_flag"] for order in executor.orders] == ["3", "4", "0"]
    assert [order.price for order in executor.orders] == [3201, 3201, 3201]


def test_target_pos_task_uses_product_rule_over_default() -> None:
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.close_policy = FuturesClosePolicy(fallback="today_first", overrides={"RB": "yesterday_first"})
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1,
        trade_rule={"price": "PASSIVE"},
        params=CTPTargetPosTaskParams(
            max_wait_seconds=1,
            price_strategy="ACTIVE",
        ),
    )

    result = ctp_target_pos_task_algorithm(cast("ExecutorProtocol", executor), algorithm_input)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert [order.extra["offset_flag"] for order in executor.orders] == ["4", "3", "0"]
    assert [order.price for order in executor.orders] == [3199, 3199, 3199]


def test_target_pos_task_propagates_session_recovery_from_final_account_query() -> None:
    class _RecoveryExecutor(_FuturesExecutor):
        def __init__(self) -> None:
            super().__init__(TradeChannel.CTP)
            self.account_asset_calls = 0

        def get_account_assets(self) -> UnifiedAccountAssets:
            self.account_asset_calls += 1
            if self.account_asset_calls == 2:
                raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)
            return super().get_account_assets()

    executor = _RecoveryExecutor()
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=-2,
        trade_rule={},
        params=CTPTargetPosTaskParams(max_wait_seconds=1),
    )

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        ctp_target_pos_task_algorithm(cast("ExecutorProtocol", executor), algorithm_input)

    assert executor.account_asset_calls == 2


@pytest.mark.parametrize("source", ["unavailable", "assumed", "error"])
def test_target_pos_task_does_not_trade_when_start_snapshot_is_unknown(source):
    executor = _FuturesExecutor(TradeChannel.CTP)
    executor.get_account_assets = MagicMock(
        return_value=UnifiedAccountAssets.unavailable().model_copy(update={"source": source})
    )
    executor.place_order = MagicMock()
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610", target_volume=0, trade_rule={}, params=CTPTargetPosTaskParams(max_wait_seconds=1)
        ),
    )
    assert result.status == ExecutionStatus.FAILED
    assert result.error == "初始持仓尚未确认"
    assert result.orders == []
    executor.place_order.assert_not_called()


def test_target_pos_task_forwards_tracker_cancel_error(monkeypatch):
    executor = _FuturesExecutor(TradeChannel.CTP)
    tracker = MagicMock()
    tracker.get_pending_count.return_value = 1
    tracker.get_all_orders.return_value = []
    tracker.get_all_trades.return_value = []
    tracker.explicit_error = None
    tracker.explicit_blocked_error = None

    def wait_and_fail(timeout):
        _ = timeout
        tracker.explicit_error = "撤单失败，订单终态尚未确认"
        raise RuntimeError("部分订单撤销失败: 1")

    tracker.wait_for_completion.side_effect = wait_and_fail
    monkeypatch.setattr(
        "axile.executor.algorithms.defaults.ctp_target_pos_task.impl.setup_order_tracker",
        lambda *args, **kwargs: tracker,
    )
    monkeypatch.setattr(
        "axile.executor.algorithms.defaults.ctp_target_pos_task.impl.teardown_order_tracker",
        lambda *args, **kwargs: None,
    )
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610", target_volume=0, trade_rule={}, params=CTPTargetPosTaskParams(max_wait_seconds=1)
        ),
    )
    assert result.error == "撤单失败，订单终态尚未确认"
    assert result.status == ExecutionStatus.PARTIAL


@pytest.mark.parametrize("source", ["unavailable", "assumed", "error"])
def test_target_pos_task_does_not_claim_flat_when_final_snapshot_is_unavailable(source):
    executor = _FuturesExecutor(TradeChannel.CTP)
    initial = executor.get_account_assets()
    unavailable = UnifiedAccountAssets.unavailable().model_copy(update={"source": source})
    executor.get_account_assets = MagicMock(side_effect=[initial, initial, unavailable])
    result = ctp_target_pos_task_algorithm(
        cast("ExecutorProtocol", executor),
        AlgorithmInput(
            symbol="rb2610", target_volume=0, trade_rule={}, params=CTPTargetPosTaskParams(max_wait_seconds=1)
        ),
    )
    assert result.status == ExecutionStatus.PARTIAL
    assert result.final_volume is None
    assert result.error == "最终持仓尚未确认"
    assert result.orders == executor.orders
    assert result.trades


@pytest.mark.parametrize("stage", ["submit", "final_query"])
@pytest.mark.parametrize("error_type", [ExecutionTerminated, MemoryError])
def test_target_pos_task_propagates_interruptions(monkeypatch, stage, error_type):
    executor = _FuturesExecutor(TradeChannel.CTP)
    error = (
        ExecutionTerminated(reason="stop", mode="cancel_pending", cancel_failed_order_ids=["pending"])
        if error_type is ExecutionTerminated
        else MemoryError()
    )
    initial = executor.get_account_assets()
    if stage == "submit":
        monkeypatch.setattr(executor, "place_order", MagicMock(side_effect=error))
    else:
        monkeypatch.setattr(executor, "get_account_assets", MagicMock(side_effect=[initial, initial, error, initial]))
    with pytest.raises(error_type) as raised:
        ctp_target_pos_task_algorithm(
            cast("ExecutorProtocol", executor),
            AlgorithmInput(
                symbol="rb2610", target_volume=0, trade_rule={}, params=CTPTargetPosTaskParams(max_wait_seconds=1)
            ),
        )
    assert raised.value is error
    assert executor.order_callbacks == []
    assert executor.trade_callbacks == []
