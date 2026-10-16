"""真实均价、成交完整性与不同来源字段合并的回归。"""

import pytest

from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.order_observation import merge_order


def order(**changes):
    return UnifiedOrder(
        **{
            **dict(
                order_id="one",
                symbol="rb2610",
                direction="BUY",
                order_type="LIMIT",
                volume=3,
                price=120,
                status="部分成交",
                filled_volume=3,
            ),
            **changes,
        }
    )


def trade(identity, quantity, price, **extra):
    return TradeRecord.create(
        trade_id=identity, order_id="one", symbol="rb2610", trade_volume=quantity, trade_price=price, **extra
    )


def test_complete_trades_produce_weighted_average_before_pagination_and_publish_version():
    book = AccountOrderBook()
    book.apply(order())
    first = trade("first", 1, 100)
    book.activity.observe_trade(first)
    assert book.snapshot()["data"][0].avg_price == 0
    version = book.snapshot()["version"]
    book.activity.observe_trade(first)
    assert book.snapshot()["version"] == version
    book.activity.observe_trade(trade("second", 2, 106))
    assert book.snapshot()["version"] > version
    for result in (book.snapshot()["data"][0], book.activity.snapshot()["orders"][0]):
        assert result.avg_price == 104
        assert result.extra["avg_price_source"] == "trades"


@pytest.mark.parametrize("mismatch", [{"trading_day": "20261015"}, {"exchange_id": "DCE"}])
def test_trade_identity_mismatch_does_not_fill_average(mismatch):
    book = AccountOrderBook()
    book.apply(order(extra={"trading_day": "20261016", "exchange_id": "SHFE"}))
    book.activity.observe_trade(
        trade("first", 3, 100, **{"trading_day": "20261016", "exchange_id": "SHFE", **mismatch})
    )
    assert book.snapshot()["data"][0].avg_price == 0


def test_cache_eviction_cannot_publish_average_of_remaining_partial_fills():
    book = AccountOrderBook()
    book.activity._capacity = 1
    book.apply(order())
    book.activity.observe_trade(trade("first", 1, 100))
    book.activity.observe_trade(trade("second", 2, 106))
    assert book.snapshot()["data"][0].avg_price == 0


def test_native_mean_and_false_zero_attributes_survive_partial_callback():
    previous = order(
        avg_price=104, extra={"reduce_only": True, "raw_order_data": {"filled_amount": 312, "order_business": 200}}
    )
    incoming = order(extra={"reduce_only": False, "raw_order_data": {"order_business": 0, "filled_amount": None}})
    result = merge_order(previous, incoming)
    assert result.avg_price == 104
    assert result.extra["reduce_only"] is False
    assert result.extra["raw_order_data"] == {"filled_amount": 312, "order_business": 0}
    assert merge_order(previous, order(filled_volume=4)).avg_price == 0


def test_fills_ahead_of_order_and_invalid_prices_leave_mean_unknown():
    book = AccountOrderBook()
    book.apply(order(filled_volume=1))
    book.activity.observe_trade(trade("first", 3, 100))
    assert book.snapshot()["data"][0].avg_price == 0
    book.apply(order())
    book.activity.observe_trade(trade("first", 3, float("nan")))
    assert book.snapshot()["data"][0].avg_price == 0


def test_tracker_mean_uses_fill_prices_and_does_not_reuse_mean_for_old_quantity():
    from axile.executor.algorithms.utils.order_tracker import OrderTracker

    tracker = OrderTracker.__new__(OrderTracker)
    tracker.order_trades = {"one": [trade("a", 1, 100), trade("b", 2, 106)]}
    tracker.order_trades["one"][0].trade_value *= 10  # 渠道成交金额可能包含合约乘数。
    result = tracker._merge_order_update("one", order(filled_volume=1, avg_price=100), order())
    assert result.avg_price == 104
    tracker.order_trades = {}
    result = tracker._merge_order_update("one", order(filled_volume=1, avg_price=100), order())
    assert result.avg_price == 0
    result = tracker._merge_order_update("one", order(avg_price=104), order())
    assert result.avg_price == 104


def test_derived_average_is_revalidated_after_records_are_evicted():
    book = AccountOrderBook()
    book.apply(order())
    book.activity.observe_trade(trade("first", 1, 100))
    book.activity.observe_trade(trade("second", 2, 106))
    derived = book.snapshot()["data"][0]
    book.replace([derived], book.begin_sync())
    book.activity._capacity = 1
    book.activity.observe_trade(trade("third", 1, 101))
    assert book.snapshot()["data"][0].avg_price == 0
