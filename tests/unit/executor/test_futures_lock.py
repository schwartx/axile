"""CTP/TQ 共用平今转锁仓，实际运行各算法和执行会话。"""

from unittest.mock import Mock

import pytest

from axile.common.order_param_model import OrderParamModel
from axile.common.trade_channel import TradeChannel
from axile.executor.constants.order_status import OrderStatus
from axile.executor.futures_close_runtime import FuturesCloseRuntimeMixin
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, UnifiedOrder
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.algorithms.test_target_pos_task_channels import _FuturesExecutor
from tests.unit.executor.test_base_executor_symbol_dispatch import _TestExecutor


class _LockExecutor(FuturesCloseRuntimeMixin, _TestExecutor):
    order_param_model = OrderParamModel.OFFSET

    def __init__(self, positions=(1, 0, 0, 0), channel=TradeChannel.CTP):
        super().__init__()
        self.channel_type = channel
        self.book = _FuturesExecutor(channel)
        self.book.symbol = "IF2612"
        self.book.long_today, self.book.long_yesterday, self.book.short_today, self.book.short_yesterday = positions
        self.cancel_all_orders = Mock()
        if channel == TradeChannel.TQ:
            from axile.executor.tq.converters import account_to_unified
            from axile.executor.tq.symbols import TQInstrument, TQSymbolResolver

            resolver = TQSymbolResolver([TQInstrument("CFFEX.IF2612", "IF2612", "CFFEX", "FUTURE")])
            self.book.get_account_assets = lambda: account_to_unified(
                {"balance": 1_000_000, "available": 1_000_000},
                {
                    "CFFEX.IF2612": {
                        "pos_long_today": self.book.long_today,
                        "pos_long_his": self.book.long_yesterday,
                        "pos_short_today": self.book.short_today,
                        "pos_short_his": self.book.short_yesterday,
                    }
                },
                resolver,
            )

    def get_account_assets(self):
        return self.book.get_account_assets()

    def plan_close_orders(self, symbol, direction, volume, assets):
        return plan_futures_close_orders(symbol, direction, volume, assets, "CFFEX")

    def _place_order_impl(self, symbol, direction, order_type, volume, price=0, **kwargs):
        offsets = {"open": "0", "close": "1", "close_today": "3", "close_yesterday": "4"}
        flag = str(kwargs.get("offset_flag", "0"))
        kwargs["offset_flag"] = offsets.get(flag, flag)
        return self.book.place_order(direction, order_type, volume, price, **kwargs)

    def register_order_callback_for_symbol(self, callback, symbol):
        self.book.register_order_callback(callback)

    def unregister_order_callback_for_symbol(self, callback, symbol):
        self.book.unregister_order_callback(callback)

    def register_trade_callback_for_symbol(self, callback, symbol):
        self.book.register_trade_callback(callback)

    def unregister_trade_callback_for_symbol(self, callback, symbol):
        self.book.unregister_trade_callback(callback)

    def register_price_callback_for_symbol(self, callback, symbol):
        self.book.register_price_callback(callback)

    def unregister_price_callback_for_symbol(self, callback, symbol):
        self.book.unregister_price_callback(callback)

    def _get_pending_orders_for_execution(self, symbol):
        return []

    def _query_trades_for_execution(self, symbol, order_id):
        return []


def run(executor, target, algorithm="TARGET-POS-TASK", enabled=True, params=None, trade_rules=None, **extra):
    from axile.executor.algorithms.core.loader import load_default_algorithms

    load_default_algorithms()
    params = {"max_wait_seconds": 1, **(params or {})}
    if algorithm == "TWAP":
        params = {"total_duration": 1, "slices": 1, **params}
    if algorithm == "POV":
        params = {"max_duration": 1, "interval_seconds": 0.1, **params}
    return executor.execute(
        UnifiedStandardInput(
            channel_type=executor.channel_type,
            account_config=executor.account_config
            if executor.channel_type == TradeChannel.CTP
            else {"account_mode": "kq", "tq_username": "test", "tq_password": "test"},
            curr_target={"IF2612": target / 10000},
            algorithm={"method": algorithm, "params": params},
            futures_close_policy={"avoid_close_today": ["IF"] if enabled else []},
            trade_rules=trade_rules or {},
            extra=extra,
        )
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = ClockStub()
    clock.current = 1_800_000_000
    monkeypatch.setattr("axile.executor.algorithms.utils.clock._default_clock", clock)
    return clock


@pytest.mark.parametrize("algorithm", ["TARGET-POS-TASK", "SINGLE-MAKER", "TWAP", "POV"])
@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
@pytest.mark.parametrize(
    "positions,target,expected",
    [
        ((0, 0, 0, 0), 1, [("BUY", 1, "0")]),
        ((1, 0, 0, 0), 2, [("BUY", 1, "0")]),
        ((0, 1, 0, 1), 2, [("BUY", 1, "1"), ("BUY", 1, "0")]),
        ((1, 0, 0, 0), 0, [("SELL", 1, "0")]),
        ((0, 0, 1, 0), 0, [("BUY", 1, "0")]),
        ((1, 2, 0, 0), 0, [("SELL", 3, "0")]),
        ((0, 1, 0, 1), 0, [("SELL", 1, "1"), ("BUY", 1, "1")]),
        ((0, 1, 0, 1), 1, [("BUY", 1, "1")]),
        ((0, 1, 0, 1), -1, [("SELL", 1, "1")]),
        ((0, 2, 1, 0), 0, [("SELL", 1, "1")]),
        ((1, 0, 1, 0), 0, []),
    ],
)
def test_algorithms_share_lock_and_unlock(algorithm, channel, positions, target, expected):
    executor = _LockExecutor(positions, channel)
    result = run(executor, target, algorithm)
    assert result.success, result.model_dump()
    assert result.symbol_results["IF2612"].final_volume == target
    actual = [(o.direction, o.volume, o.extra["offset_flag"]) for o in executor.book.orders]
    assert actual == expected
    assert executor.cancel_all_orders.call_count == bool(expected)
    assert not executor.book.order_callbacks and not executor.book.price_callbacks and not executor.book.trade_callbacks
    count = len(executor.book.orders)
    assert run(executor, target, algorithm).status == ExecutionStatus.NOOP
    assert len(executor.book.orders) == count


@pytest.mark.parametrize("algorithm", ["TARGET-POS-TASK", "SINGLE-MAKER", "TWAP", "POV"])
def test_next_day_cleanup_and_clear_bypass(algorithm):
    executor = _LockExecutor()
    assert run(executor, 0, algorithm).success
    executor.book.long_today, executor.book.long_yesterday = 0, executor.book.long_today
    executor.book.short_today, executor.book.short_yesterday = 0, executor.book.short_today
    assert run(executor, 0, algorithm).success
    assert not executor.get_account_assets().positions
    executor = _LockExecutor((1, 0, 1, 0))
    assert run(executor, 0, algorithm, close_all_positions=True).success
    assert not executor.get_account_assets().positions
    assert [o.extra["offset_flag"] for o in executor.book.orders] == ["1", "1"]


def test_disabled_rule_preserves_net_only_behavior():
    executor = _LockExecutor((0, 1, 0, 1))
    assert run(executor, 0, enabled=False).success
    assert executor.book.orders == []
    executor = _LockExecutor()
    assert run(executor, 0, enabled=False).success
    assert executor.book.orders[0].extra["offset_flag"] == "1"


def test_frozen_positions_block_before_cancelling_or_sending():
    executor = _LockExecutor()
    assets = executor.get_account_assets()
    assets.positions[0].available_volume = 0
    executor.get_account_assets = lambda: assets
    assert not run(executor, 0).success
    assert executor.book.orders == []
    executor.cancel_all_orders.assert_not_called()


@pytest.mark.parametrize("target", [0, 2])
def test_rejected_unlock_stops_following_legs_and_keeps_evidence(target):
    executor = _LockExecutor((0, 1, 0, 1))

    def reject(direction, order_type, volume, price=0, **kwargs):
        order = UnifiedOrder(
            symbol="IF2612",
            order_id="rejected",
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.REJECTED,
            extra={"offset_flag": kwargs["offset_flag"]},
        )
        executor.book.orders.append(order)
        return order

    executor.book.place_order = reject
    result = run(executor, target)
    assert not result.success
    assert len(executor.book.orders) == 1
    assert result.symbol_results["IF2612"].orders[0].status == OrderStatus.REJECTED
    assert executor.book.orders[0].extra["offset_flag"] == "1"


@pytest.mark.parametrize("algorithm", ["TARGET-POS-TASK", "SINGLE-MAKER", "TWAP", "POV"])
def test_quote_block_during_unlock_preserves_reason(algorithm):
    executor = _LockExecutor((0, 1, 0, 1))
    original = executor.get_market_data
    reads = 0

    def quote(symbols):
        nonlocal reads
        reads += 1
        if reads > 1:
            from axile.executor.market_data_validation import MarketDataUnavailableError

            raise MarketDataUnavailableError("IF2612", "行情尚未确认")
        return original(symbols)

    executor.get_market_data = quote
    result = run(executor, 0, algorithm)
    assert not result.success
    assert result.symbol_results["IF2612"].reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
    assert executor.book.orders == []


def test_new_today_position_stops_stale_unlock_before_sending():
    executor = _LockExecutor((0, 1, 0, 1))
    original = executor.plan_futures_order

    def changed(*args, **kwargs):
        executor.book.long_today = 1
        return original(*args[:3], executor.get_account_assets(), **kwargs)

    executor.plan_futures_order = changed
    result = run(executor, 0)
    assert not result.success
    assert executor.book.orders == []


def test_partial_hedge_retries_only_missing_lots():
    executor = _LockExecutor()
    original = executor.book.place_order

    def fail_second(*args, **kwargs):
        if executor.book.orders:
            raise RuntimeError("connection lost")
        return original(*args, **kwargs)

    executor.book.place_order = fail_second
    result = run(executor, -1)
    assert not result.success
    assert len(result.symbol_results["IF2612"].orders) == 1
    executor.book.place_order = original
    assert run(executor, -1).success
    assert [(o.volume, o.extra["offset_flag"]) for o in executor.book.orders] == [(1, "0"), (1, "0")]


def test_wrong_gross_position_cannot_be_rejudged_success_from_net_zero():
    executor = _LockExecutor((0, 1, 0, 1))
    original = executor.book.place_order

    def no_position_change(*args, **kwargs):
        order = original(*args, **kwargs)
        executor.book.long_yesterday = 1
        return order

    executor.book.place_order = no_position_change
    result = run(executor, 0)
    assert not result.success
    assert len(result.symbol_results["IF2612"].orders) == 1
    assert result.symbol_results["IF2612"].final_volume == 0


def test_unavailable_snapshot_keeps_previous_order_evidence():
    executor = _LockExecutor((0, 1, 0, 1))
    original = executor.get_account_assets

    def assets():
        if executor.book.orders:
            raise RuntimeError("position query unavailable")
        return original()

    executor.get_account_assets = assets
    result = run(executor, 0)
    assert not result.success
    assert len(result.symbol_results["IF2612"].orders) == 1
    assert result.symbol_results["IF2612"].final_volume is None


def test_converted_open_uses_open_order_type():
    executor = _LockExecutor()
    from axile.executor.models.unified_order import OrderType

    result = run(executor, 0, params={"close_order_type": "LIMIT", "open_order_type": "MARKET"})
    assert result.success
    assert executor.book.orders[0].order_type == OrderType.MARKET


@pytest.mark.parametrize("algorithm", ["TARGET-POS-TASK", "SINGLE-MAKER", "TWAP", "POV"])
def test_quote_failure_after_first_unlock_keeps_partial_progress(algorithm):
    executor = _LockExecutor((0, 1, 0, 1))
    original = executor.get_market_data

    def quote(symbols):
        if executor.book.orders:
            from axile.executor.market_data_validation import MarketDataUnavailableError

            raise MarketDataUnavailableError("IF2612", "行情尚未确认")
        return original(symbols)

    executor.get_market_data = quote
    result = run(executor, 0, algorithm)
    symbol = result.symbol_results["IF2612"]
    assert result.status == ExecutionStatus.PARTIAL
    assert symbol.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
    assert len(symbol.orders) == 1
    assert symbol.final_volume == -1


def test_pending_query_failure_keeps_completed_leg():
    executor = _LockExecutor((0, 1, 0, 1))

    def pending(symbol):
        if executor.book.orders:
            raise RuntimeError("pending order query failed")
        return []

    executor._get_pending_orders_for_execution = pending
    result = run(executor, 0)
    assert not result.success
    assert len(result.symbol_results["IF2612"].orders) == 1
    assert len(executor.book.orders) == 1


@pytest.mark.parametrize("algorithm", ["SINGLE-MAKER", "TWAP", "POV"])
@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
@pytest.mark.parametrize("long_today,target,direction", [(1, -10, OrderDirection.SELL), (0, 10, OrderDirection.BUY)])
@pytest.mark.parametrize("max_order_size", [None, 3])
def test_planned_open_is_not_limited_by_protected_opposite_position(
    algorithm, channel, long_today, target, direction, max_order_size
):
    executor = _LockExecutor((long_today, 0, 1 - long_today, 0), channel)
    if max_order_size is not None:
        executor.get_order_volume_bounds = lambda *args: (1, max_order_size)
    result = run(executor, target, algorithm)
    assert result.success
    assert result.symbol_results["IF2612"].final_volume == target
    assert sum(order.volume for order in executor.book.orders) == 11
    assert all(order.direction == direction and order.extra["offset_flag"] == "0" for order in executor.book.orders)
    if max_order_size is not None:
        assert all(order.volume <= max_order_size for order in executor.book.orders)
    assert (executor.book.long_today, executor.book.short_today) == ((1, 11) if long_today else (11, 1))


@pytest.mark.parametrize("algorithm", ["SINGLE-MAKER", "TWAP", "POV"])
def test_account_trade_rule_reaches_real_algorithm_and_session(algorithm):
    executor = _LockExecutor()
    executor.get_order_volume_bounds = lambda symbol, order_type, rule=None: (
        1,
        (rule or {}).get("max_single_order_size"),
    )
    result = run(executor, -5, algorithm, trade_rules={"IF2612": {"max_single_order_size": 2}})
    assert result.success
    assert [order.volume for order in executor.book.orders] == [2, 2, 2]
    assert all(order.extra["offset_flag"] == "0" for order in executor.book.orders)


@pytest.mark.parametrize("channel", [TradeChannel.CTP, TradeChannel.TQ])
def test_clear_cancels_frozen_orders_before_planning_actual_closes(channel):
    executor = _LockExecutor((1, 0, 0, 0), channel)
    original = executor.get_account_assets
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True

    def snapshot():
        assets = original()
        if not cancelled:
            for position in assets.positions:
                position.available_volume = 0
        return assets

    executor.cancel_all_orders.side_effect = cancel
    executor.get_account_assets = snapshot
    if channel == TradeChannel.TQ:
        from axile.executor.models.unified_input import TQAccountConfig

        executor.account_config = TQAccountConfig(account_mode="kq", tq_username="test", tq_password="test")
    result = executor.empty_positions(
        cleanup=False,
        algorithm={"method": "TARGET-POS-TASK", "params": {"max_wait_seconds": 1}},
        futures_close_policy={"avoid_close_today": ["IF"]},
    )
    assert result.success
    assert cancelled
    assert [order.extra["offset_flag"] for order in executor.book.orders] == ["1"]
    assert not executor.get_account_assets().positions


@pytest.mark.parametrize("algorithm", ["TARGET-POS-TASK", "SINGLE-MAKER"])
def test_clear_can_close_available_lots_while_previous_chunks_are_frozen(algorithm):
    executor = _LockExecutor((1, 0, 6, 0))
    executor.get_order_volume_bounds = lambda *args: (1, 2)
    original = executor.get_account_assets

    def snapshot():
        assets = original()
        if len(executor.book.orders) == 2:
            for position in assets.positions:
                if position.direction == "空头":
                    position.available_volume -= 2
        return assets

    executor.get_account_assets = snapshot
    result = run(executor, 0, algorithm, close_all_positions=True)
    assert result.success
    assert not executor.get_account_assets().positions
    assert [(order.direction, order.volume, order.extra["offset_flag"]) for order in executor.book.orders] == [
        ("SELL", 1, "1"),
        ("BUY", 2, "1"),
        ("BUY", 2, "1"),
        ("BUY", 2, "1"),
    ]


@pytest.mark.parametrize("target", [1, -1])
def test_twap_carries_sub_lot_slices_without_sending_fractional_orders(target):
    executor = _LockExecutor((0, 0, 0, 0))
    executor.get_order_volume_bounds = lambda *args: (1, None)
    result = run(executor, target, "TWAP", params={"total_duration": 2, "slices": 2})
    assert result.success
    assert [order.volume for order in executor.book.orders] == [1]
    assert result.symbol_results["IF2612"].final_volume == target
