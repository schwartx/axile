"""撤单门禁和规划行情并发更新的审查回归。"""

from types import SimpleNamespace

import pytest

from axile.common.serialization import copy_data
from axile.executor.ctp.ctp_execute import CtpRequestError, CtpSessionRecoveryRequired
from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker


@pytest.fixture
def broker(monkeypatch):
    driver = ScriptedBroker(monkeypatch)
    driver.start()
    driver.executor.initialize_websocket(["ag2612"])
    driver.quote()
    driver.trader.ReqOrderAction.return_value = 0
    yield driver
    driver.executor.close()


def test_unassociated_trade_blocks_insert_but_allows_identified_cancel(broker):
    executor = broker.executor
    unknown = SimpleNamespace(**(vars(broker.trade) | {"OrderSysID": "late", "TradeID": "late"}))
    executor._on_trade(unknown)
    with pytest.raises(CtpRequestError, match="未归属"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000)
    assert executor._cancel_order_impl("ag2612", "20260909:7:1:10")
    broker.trader.ReqOrderAction.assert_called_once()
    broker.trader.ReqOrderInsert.assert_not_called()
    executor._disconnected("交易", 1)
    with pytest.raises(CtpSessionRecoveryRequired):
        executor._cancel_order_impl("ag2612", "20260909:7:1:10")
    assert broker.trader.ReqOrderAction.call_count == 1


def test_sizing_refreshes_price_when_callback_updates_planning_snapshot(broker):
    executor = broker.executor
    planning = executor.get_market_data(["ag2612"])
    original = planning["ag2612"]
    executor._quotes["ag2612"] = copy_data(original, update={"last_price": 9001})
    assets = executor._recovery_snapshot["assets"]
    result = executor.calculate_target_sizing({"ag2612": 0.5}, assets, planning, {}, {})["ag2612"]
    assert result.status == "SIZED"
    assert result.reference_price == 9001
    assert result.raw_quantity == pytest.approx(assets.total_asset * 0.5 / (9001 * 15))
    assert original.last_price == 9000


def test_sizing_does_not_refresh_from_stale_cache(broker):
    executor = broker.executor
    planning = executor.get_market_data(["ag2612"])
    stale = copy_data(planning["ag2612"], deep=True)
    stale.extra["received_at"] -= 10
    executor._quotes["ag2612"] = stale
    result = executor.calculate_target_sizing({"ag2612": 0.5}, executor._recovery_snapshot["assets"], planning, {}, {})[
        "ag2612"
    ]
    assert result.status == "UNAVAILABLE"


def test_sizing_uses_same_snapshot_if_cache_changes_during_validation(broker, monkeypatch):
    executor = broker.executor
    planning = executor.get_market_data(["ag2612"])
    selected = copy_data(planning["ag2612"], update={"last_price": 9001})
    executor._quotes["ag2612"] = selected
    validate = executor._snapshot_quote_error

    def update_during_validation(symbol, quote):
        executor._quotes[symbol] = copy_data(selected, update={"last_price": 9002})
        return validate(symbol, quote)

    monkeypatch.setattr(executor, "_snapshot_quote_error", update_during_validation)
    assets = executor._recovery_snapshot["assets"]
    result = executor.calculate_target_sizing({"ag2612": 0.5}, assets, planning, {}, {})["ag2612"]
    assert result.status == "SIZED"
    assert result.reference_price == 9001
    assert result.unit_notional == 9001 * 15
    assert result.raw_quantity == pytest.approx(assets.total_asset * 0.5 / result.unit_notional)
    assert executor._quotes["ag2612"].last_price == 9002


@pytest.mark.parametrize("day", ["20260908", "20260910"])
def test_cross_day_trade_invalidates_session_and_blocks_new_orders(broker, day):
    executor = broker.executor
    trade = SimpleNamespace(**(vars(broker.trade) | {"TradingDay": day}))
    broker.trader_spi.OnRtnTrade(trade)
    assert not executor._verify_connection()
    assert "交易日" in executor._invalid_reason
    with pytest.raises(CtpSessionRecoveryRequired):
        executor._require_new_order_ready("ag2612")
    broker.trader.ReqOrderInsert.assert_not_called()


def test_unassociated_fill_replays_before_cancel_callback_submits_fallback(broker, monkeypatch):
    from openctp_ctp import thosttraderapi as td

    from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
    from axile.executor.ctp.converters import order_to_unified
    from tests.fixtures.algorithm_fakes import FallbackExecutor

    executor = broker.executor
    adapter = FallbackExecutor()
    adapter.symbol = "ag2612"
    tracker = OrderTracker(adapter, chase_config=ChaseConfig(enabled=True))
    native = SimpleNamespace(
        **(
            vars(broker.order)
            | {
                "OrderRef": "11",
                "OrderSysID": "late",
                "VolumeTotalOriginal": 3,
                "VolumeTraded": 0,
                "OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing,
                "LimitPrice": 9000,
                "OrderPriceType": td.THOST_FTDC_OPT_LimitPrice,
            }
        )
    )
    parent = order_to_unified(native, trading_day=broker.day, front_id=7, session_id=1)
    tracker.add_order(parent, direction=OrderDirection.BUY)
    tracker._chase_info[parent.order_id]["market_order_fallback_pending_cancel"] = True
    tracker.latest_prices["ag2612"] = executor._quotes["ag2612"]
    executor.register_trade_callback(tracker.on_trade_record)
    executor.register_order_callback(tracker.on_order_update)
    place = adapter.place_order

    def guarded_place(*args, **kwargs):
        executor._require_new_order_ready("ag2612")
        return place(*args, **kwargs)

    monkeypatch.setattr(adapter, "place_order", guarded_place)
    trade = SimpleNamespace(**(vars(broker.trade) | {"OrderRef": "11", "OrderSysID": "late", "TradeID": "late"}))
    broker.trader_spi.OnRtnTrade(trade)
    assert executor._unassociated_trades
    native.OrderStatus = td.THOST_FTDC_OST_Canceled
    # 即使订单累计成交字段落后，已重放的成交也必须用于剩余量计算。
    broker.trader_spi.OnRtnOrder(native)
    assert not executor._unassociated_trades
    assert [call["volume"] for call in adapter.place_calls] == [2]
    assert adapter.place_calls[0]["order_type"] == OrderType.MARKET
    assert len(tracker.get_all_trades()) == 1
    broker.trader_spi.OnRtnTrade(trade)
    broker.trader_spi.OnRtnOrder(native)
    assert len(adapter.place_calls) == 1
