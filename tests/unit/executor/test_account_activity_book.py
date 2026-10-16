"""账户记录跨 execution 保留、成交去重及有界缓存。"""

import pytest

from axile.executor.account_activity import AccountActivityBook
from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder


def order(symbol="rb2610", **changes):
    return UnifiedOrder(
        order_id="same-id", symbol=symbol, direction="BUY", order_type="LIMIT", volume=3, price=100, **changes
    )


def test_orders_keep_terminal_and_filled_quantity_when_late_baseline_arrives():
    book = AccountActivityBook()
    book.observe_order(order(status="已成交", filled_volume=3))
    book.observe_order(order(status="部分成交", filled_volume=1))
    book.observe_order(order("ag2612", status="已撤销", filled_volume=1))
    assert len(book.snapshot()["orders"]) == 2
    assert book.snapshot("rb2610")["orders"][0].filled_volume == 3
    returned = book.snapshot()["orders"][0]
    returned.filled_volume = 0
    assert book.snapshot("rb2610")["orders"][0].filled_volume == 3


def test_pending_query_does_not_erase_recent_terminal_records():
    book = AccountOrderBook()
    book.apply(order(status="已撤销"))
    book.replace([], book.begin_sync())
    assert book.snapshot()["data"] == []
    assert book.activity.snapshot()["orders"][0].status == "已撤销"


def test_partial_fills_are_independent_and_queries_do_not_duplicate_callbacks():
    book = AccountActivityBook()
    for identity in ("first", "first", "second"):
        book.observe_trade(
            TradeRecord.create(trade_id=identity, symbol="rb2610", order_id="same-id", trade_volume=1, trade_price=100)
        )
    assert len(book.snapshot()["trades"]) == 2
    assert sum(trade.trade_volume for trade in book.snapshot()["trades"]) == 2


def test_failures_do_not_change_order_state_and_all_caches_are_bounded():
    book = AccountActivityBook(capacity=2)
    book.observe_order(order(status="待成交"))
    for identity in ("one", "two", "three", "three"):
        book.record_failure("cancel_order", "rb2610", "same-id", "撤单失败", event_id=identity)
    assert book.snapshot()["orders"][0].status == "待成交"
    assert [row["event_id"] for row in book.snapshot()["failures"]] == ["two", "three"]


def test_query_completeness_is_not_promoted_from_one_symbol_to_account():
    book = AccountActivityBook()
    book.mark_query("rb2610", scope="当前交易日", errors=[])
    assert book.snapshot("rb2610")["queried_at"]
    assert book.snapshot()["queried_at"] is None


def test_native_trade_id_reused_on_another_trading_day_is_not_deduplicated():
    book = AccountActivityBook()
    for day in ("20261015", "20261016"):
        book.observe_trade(
            TradeRecord.create(trade_id="one", symbol="rb2610", trade_volume=1, trading_day=day, exchange_id="SHFE")
        )
    assert len(book.snapshot()["trades"]) == 2


@pytest.mark.parametrize("callback_first", [True, False])
def test_trade_query_and_callback_merge_without_losing_event_details(callback_first):
    book = AccountActivityBook()
    callback = TradeRecord.create(
        trade_id="one",
        symbol="SHFE.rb2610",
        order_id="order-1",
        trade_volume=1,
        trade_price=100,
        trade_time="2026-10-16T10:00:00",
        client_order_id="client-1",
        execution_type="TRADE",
        order_status="PARTIALLY_FILLED",
        commission=0,
        is_maker=False,
        raw_trade_data={"commission": 0, "reference": "native"},
    )
    query = callback.model_copy(
        update={"extra": {"client_order_id": "", "commission": None, "raw_trade_data": {"commission": None}}}, deep=True
    )
    for value in [callback, query] if callback_first else [query, callback]:
        book.observe_trade(value)
    result = book.snapshot()["trades"]
    assert len(result) == 1
    assert result[0].extra["client_order_id"] == "client-1"
    assert result[0].extra["execution_type"] == "TRADE"
    assert result[0].extra["order_status"] == "PARTIALLY_FILLED"
    assert result[0].extra["commission"] == 0
    assert result[0].extra["is_maker"] is False
    assert result[0].extra["raw_trade_data"] == {"commission": 0, "reference": "native"}
    version = book.version
    book.observe_trade(query)
    assert book.version == version
    result[0].extra["client_order_id"] = "mutated"
    assert book.snapshot()["trades"][0].extra["client_order_id"] == "client-1"


def test_trade_merge_keeps_zero_and_false_updates_and_original_order_link():
    book = AccountActivityBook()
    first = TradeRecord.create(
        trade_id="one",
        symbol="SHFE.rb2610",
        order_id="order-1",
        trade_volume=1,
        trade_price=100,
        commission=1,
        is_maker=True,
    )
    book.observe_trade(first)
    book.observe_trade(
        first.model_copy(update={"order_id": "", "trade_time": "", "extra": {"commission": 0, "is_maker": False}})
    )
    result = book.snapshot()["trades"][0]
    assert result.order_id == "order-1"
    assert result.trade_time == first.trade_time
    assert result.extra == {"commission": 0, "is_maker": False}
