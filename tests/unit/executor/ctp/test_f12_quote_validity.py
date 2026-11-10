"""原生价格哨兵与盘口价量回归。"""

import sys

import pytest

from axile.executor.ctp.converters import quote_to_unified
from axile.executor.ctp.quote_validation import fresh_valuation_quote_error, quote_error, valuation_quote_error


@pytest.mark.parametrize("invalid", [sys.float_info.max, float("nan"), float("inf"), -1, 0])
def test_invalid_native_prices_are_cleaned_at_every_level(invalid):
    row = {"LastPrice": invalid, "LowerLimitPrice": invalid, "UpperLimitPrice": invalid}
    for side in ["Bid", "Ask"]:
        for level in range(1, 6):
            row[f"{side}Price{level}"] = invalid
            row[f"{side}Volume{level}"] = 1
    quote = quote_to_unified(row)
    assert quote.last_price == 0
    assert not quote.book_valid
    assert quote.extra["lower_limit_price"] == 0
    assert quote.extra["upper_limit_price"] == 0
    for side in ["bid", "ask"]:
        assert getattr(quote, f"{side}_price") == 0
        for level in range(2, 6):
            assert getattr(quote, f"{side}_price_{level}") == 0


@pytest.mark.parametrize(
    "bid,ask,bid_volume,ask_volume,valid",
    [
        (99, 101, 1, 1, True),
        (99, 101, 0, 1, False),
        (99, 101, 1, 0, False),
        (102, 101, 1, 1, False),
        (99, sys.float_info.max, 1, 0, False),
    ],
)
def test_book_validity_requires_non_crossed_prices_and_both_volumes(bid, ask, bid_volume, ask_volume, valid):
    quote = quote_to_unified(dict(BidPrice1=bid, AskPrice1=ask, BidVolume1=bid_volume, AskVolume1=ask_volume))
    assert quote.book_valid is valid


def test_valuation_accepts_old_last_price_without_book_but_order_validation_does_not():
    quote = quote_to_unified(dict(TradingDay="20260910", LastPrice=100))
    quote.timestamp = 900_000
    quote.extra["received_at"] = 900.0

    assert valuation_quote_error(quote) is None
    assert fresh_valuation_quote_error(quote, now=1000, trading_day="20260910", max_age=5) == "stale_exchange_time"
    assert quote_error(quote, now=900, trading_day="20260910", max_age=5, tick=1) == "missing_two_sided_book"


@pytest.mark.parametrize("last_price", [0, -1, float("nan"), float("inf")])
def test_valuation_rejects_invalid_last_price(last_price):
    quote = quote_to_unified(dict(LastPrice=last_price))
    assert valuation_quote_error(quote) == "invalid_last_price"


@pytest.mark.parametrize("age,valid", [(0, True), (5, True), (5.001, False), (-1, False)])
def test_freshness_boundary_checks_exchange_and_receive_time(age, valid):
    from axile.executor.ctp.quote_validation import quote_error

    quote = quote_to_unified(
        dict(
            TradingDay="20260910",
            LastPrice=100,
            BidPrice1=99,
            AskPrice1=101,
            BidVolume1=1,
            AskVolume1=1,
            LowerLimitPrice=90,
            UpperLimitPrice=110,
        )
    )
    quote.timestamp = int((1000 - age) * 1000)
    quote.extra["received_at"] = 1000 - age
    assert (quote_error(quote, now=1000, trading_day="20260910", max_age=5, tick=1) is None) is valid


@pytest.mark.parametrize("price", [0, -1, sys.float_info.max, float("inf"), float("nan"), 89, 111, 100.5])
def test_limit_price_requires_tick_and_daily_bounds(price):
    from axile.executor.ctp.quote_validation import price_in_bounds

    assert not price_in_bounds(price, tick=1, lower=90, upper=110)


@pytest.mark.parametrize("fault", ["exchange_stale", "receive_stale", "one_sided", "tick", "limits", "day"])
def test_real_executor_blocks_stale_or_invalid_quote_at_get_and_submit(monkeypatch, fault):
    from axile.executor.ctp.ctp_execute import CtpRequestError
    from axile.executor.models.unified_order import OrderDirection, OrderType
    from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker

    broker = ScriptedBroker(monkeypatch)
    executor = broker.start()
    try:
        executor.initialize_websocket(["ag2612"])
        broker.quote()
        quote = executor._quotes["ag2612"]
        monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time", lambda: 1000.0)
        quote.timestamp = 1_000_000
        quote.extra["received_at"] = 1000.0
        if fault == "exchange_stale":
            quote.timestamp -= 11_000
        elif fault == "receive_stale":
            quote.extra["received_at"] -= 11
        elif fault == "one_sided":
            quote.ask_volume = 0
        elif fault == "tick":
            quote.ask_price = 9000.5
        elif fault == "limits":
            quote.extra["upper_limit_price"] = 8000
        else:
            quote.extra["trading_day"] = "20260908"
        executor._timeout = 0.001
        with pytest.raises(TimeoutError):
            executor.get_market_data(["ag2612"])
        with pytest.raises(CtpRequestError, match="行情不可用于新单"):
            executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000)
        broker.trader.ReqOrderInsert.assert_not_called()
    finally:
        executor.close()


def test_cache_read_and_duplicate_callback_do_not_renew_receive_time(monkeypatch):
    from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker

    broker = ScriptedBroker(monkeypatch)
    executor = broker.start()
    try:
        executor.initialize_websocket(["ag2612"])
        broker.quote()
        quote = executor._quotes["ag2612"]
        original = quote.extra["received_at"]
        stamp = quote.timestamp
        assert executor.get_market_data(["ag2612"])["ag2612"] is quote
        monkeypatch.setattr("axile.executor.ctp.ctp_execute.quote_to_unified", lambda row: quote.model_copy(deep=True))
        executor._on_quote(object())
        assert executor._quotes["ag2612"].extra["received_at"] == original
        assert executor._quotes["ag2612"].timestamp == stamp
    finally:
        executor.close()


def test_weight_sizing_rejects_stale_last_and_no_last(monkeypatch):
    from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker

    broker = ScriptedBroker(monkeypatch)
    executor = broker.start()
    try:
        executor.initialize_websocket(["ag2612"])
        broker.quote()
        quote = executor._quotes["ag2612"]
        assets = executor._recovery_snapshot["assets"]
        sizing = executor._calculate_generic_sizing(0.5, 9000, assets, {}, symbol="ag2612")
        assert sizing.status == "SIZED"
        quote.extra["received_at"] -= 10
        sizing = executor._calculate_generic_sizing(0.5, 9000, assets, {}, symbol="ag2612")
        assert sizing.status == "UNAVAILABLE"
        quote.extra["received_at"] += 10
        quote.last_price = 0
        assert executor._quote_error("ag2612") == "invalid_book_price"
        sizing = executor._calculate_generic_sizing(0.5, 9000, assets, {}, symbol="ag2612")
        assert sizing.status == "UNAVAILABLE"
    finally:
        executor.close()
