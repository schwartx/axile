"""账户委托基线、并发回报与执行归属的离线回归。"""

from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_order import UnifiedOrder


def order(*, filled=0, status="待成交", symbol="rb2610"):
    return UnifiedOrder(
        order_id="one",
        symbol=symbol,
        direction="BUY",
        order_type="LIMIT",
        volume=3,
        price=3500,
        filled_volume=filled,
        status=status,
    )


def test_empty_book_is_unknown_until_successful_baseline():
    book = AccountOrderBook()
    assert not book.snapshot()["complete"]
    book.replace([], book.begin_sync())
    assert book.snapshot()["complete"] and not book.snapshot()["data"]


def test_callback_during_query_wins_and_terminal_cannot_revive():
    book = AccountOrderBook()
    watermark = book.begin_sync()
    book.apply(order(filled=3, status="已成交"))
    book.replace([order()], watermark)
    book.apply(order(filled=1, status="部分成交"))
    snapshot = book.snapshot()
    assert snapshot["complete"] and not snapshot["data"]
    assert snapshot["resolved"][0].filled_volume == 3


def test_disconnect_during_query_cannot_restore_complete():
    book = AccountOrderBook()
    watermark = book.begin_sync()
    book.invalidate("断线")
    book.replace([order()], watermark)
    assert not book.snapshot()["complete"]
    book.replace([order()], book.begin_sync())
    assert book.snapshot()["complete"]


def test_overlapping_full_query_cannot_replace_a_baseline_already_installed():
    book = AccountOrderBook()
    earlier = book.begin_sync()
    book.replace([order(filled=1, status="部分成交")], book.begin_sync())
    version = book.version
    book.replace([], earlier)
    snapshot = book.snapshot()
    assert snapshot["complete"] and snapshot["version"] == version
    assert snapshot["data"][0].filled_volume == 1


def test_baseline_missing_previous_order_stays_unknown_until_terminal_proof():
    book = AccountOrderBook()
    book.replace([order(filled=1, status="部分成交")], 0)
    book.replace([], book.begin_sync())
    assert not book.snapshot()["complete"]
    assert book.snapshot()["data"][0].filled_volume == 1
    book.apply(order(filled=1, status="已撤销"))
    assert book.snapshot()["complete"]


def test_sdk_complete_replacement_can_remove_old_day_and_returned_data_is_isolated():
    book = AccountOrderBook()
    book.replace([order()], 0)
    value = book.snapshot()["data"][0]
    value.filled_volume = 999
    assert book.snapshot()["data"][0].filled_volume == 0
    book.replace([], book.begin_sync(), drop_missing=True)
    assert book.snapshot()["complete"] and not book.snapshot()["data"]


def test_symbols_and_late_execution_metadata_remain_independent():
    book = AccountOrderBook()
    book.replace([order(), order(symbol="ag2612")], 0)
    book.remember_metadata("one", {"audit_context": {"execution_id": "old-execution"}})
    assert len(book.snapshot()["data"]) == 2
    assert book.metadata("one")["audit_context"]["execution_id"] == "old-execution"
    book.metadata("one")["audit_context"]["execution_id"] = "mutated"
    assert book.metadata("one")["audit_context"]["execution_id"] == "old-execution"


def test_same_order_number_cannot_leak_execution_metadata_across_symbols():
    book = AccountOrderBook()
    for symbol, execution_id in (("rb2610", "first"), ("ag2612", "second")):
        book.remember_metadata("one", {"symbol": symbol, "audit_context": {"execution_id": execution_id}})
    assert book.metadata("one", "rb2610")["audit_context"]["execution_id"] == "first"
    assert book.metadata("one", "ag2612")["audit_context"]["execution_id"] == "second"
    assert book.metadata("one", "unknown") == {}
    assert book.metadata("one") == {}
