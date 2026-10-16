"""账户记录跨 execution 保留、成交去重及有界缓存。"""

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
