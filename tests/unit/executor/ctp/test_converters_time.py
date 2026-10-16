"""CTP 自然时间、时区及未知时间回归测试。"""

import os
import time
from datetime import datetime, timezone

import pytest

from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.ctp.converters import account_to_unified, order_to_unified, quote_to_unified, trade_to_unified


def test_position_valuation_uses_latest_price_and_true_multiplier():
    rows = [dict(InstrumentID="rb2610", PosiDirection="2", Position=2, TodayPosition=1, PositionCost=7000)]
    quote = quote_to_unified(dict(InstrumentID="rb2610", LastPrice=400))
    assets = account_to_unified(
        dict(Balance=10000, Available=3000), rows, {"rb2610": {"VolumeMultiple": 10}}, quotes={"rb2610": quote}
    )
    assert assets.total_asset == 10000
    assert assets.market_value == 8000
    assert assets.positions[0].market_value == 8000
    assert assets.positions[0].extra["position_cost"] == 7000
    assert assets.positions[0].extra["volume_multiple"] == 10
    assert assets.positions[0].extra["last_price"] == 400
    assert assets.positions[0].extra["quote_timestamp"] == quote.timestamp
    assert assets.positions[0].extra["quote_update_time"] == quote.update_time


@pytest.mark.parametrize("multiplier,reason", [(None, "missing_multiplier"), (10, "stale_exchange_time")])
def test_position_missing_valuation_preserves_holdings(multiplier, reason):
    row = dict(InstrumentID="rb2610", PosiDirection="2", Position=2, TodayPosition=1, PositionCost=7000)
    instrument = {"VolumeMultiple": multiplier} if multiplier is not None else {}
    assets = account_to_unified(
        dict(Balance=10000), [row], {"rb2610": instrument}, quote_errors={"rb2610": "stale_exchange_time"}
    )
    assert assets.total_asset == 10000
    assert assets.market_value is None
    assert assets.positions[0].volume == 2
    assert assets.positions[0].market_value is None
    assert assets.positions[0].extra["valuation_reason"] == reason
    assert assets.positions[0].extra["position_cost"] == 7000


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX tzset")
def test_quote_timestamp_is_independent_of_host_timezone():
    row = dict(ActionDay="20260904", TradingDay="20260907", UpdateTime="21:05:00", UpdateMillisec=123)
    previous = os.environ.get("TZ")
    try:
        quotes = []
        for host_tz in ("UTC", "Asia/Shanghai"):
            os.environ["TZ"] = host_tz
            time.tzset()
            quotes.append(quote_to_unified(row))
        expected = int(datetime(2026, 9, 4, 13, 5, 0, 123000, tzinfo=timezone.utc).timestamp() * 1000)
        assert [q.timestamp for q in quotes] == [expected, expected]
        assert all(q.update_time == "2026-09-04T21:05:00.123000+08:00" for q in quotes)
        assert all(q.extra["trading_day"] == "20260907" for q in quotes)
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize(
    "day,clock,expected",
    [
        ("20260904", "21:05:00", "2026-09-04T21:05:00+08:00"),
        ("20260905", "00:05:00", "2026-09-05T00:05:00+08:00"),
        ("20260907", "09:05:00", "2026-09-07T09:05:00+08:00"),
    ],
)
def test_trade_natural_date_is_separate_from_trading_day(day, clock, expected):
    trade = trade_to_unified(
        dict(TradeDate=day, TradeTime=clock, TradingDay="20260907", OrderRef="1", Volume=2, Price=100),
        trading_day="20260907",
        front_id=1,
        session_id=2,
    )
    assert trade.trade_time == expected
    assert trade.order_id == ""  # 原生成交缺少会话字段，转换器不得冒用当前会话。
    assert trade.extra["trading_day"] == "20260907"
    assert trade.extra["event_time_source"] == "TradeDate"
    assert trade.extra["event_time_status"] == "native"
    assert trade.trade_volume == 2


@pytest.mark.parametrize(
    "day,clock", [("", "21:05:00"), ("20260230", "21:05:00"), ("20260904", ""), ("20260904", "25:00:00")]
)
def test_unknown_trade_time_preserves_trade_without_fabricating_time(day, clock):
    trade = trade_to_unified(
        dict(TradeDate=day, TradeTime=clock, TradingDay="20260907", Volume=2),
        trading_day="20260907",
        front_id=1,
        session_id=2,
    )
    assert trade.trade_time == ""
    assert trade.trade_volume == 2
    assert trade.extra["event_time_status"] == "unknown"
    assert trade.extra["event_date"] == day


@pytest.mark.parametrize(
    "changes",
    [
        dict(ActionDay=""),
        dict(ActionDay="20260230"),
        dict(UpdateTime=""),
        dict(UpdateMillisec=1000),
        dict(UpdateMillisec=-1),
        dict(UpdateMillisec="bad"),
    ],
)
def test_unknown_quote_time_is_not_fresh(changes):
    row = dict(
        ActionDay="20260904",
        TradingDay="20260907",
        UpdateTime="21:05:00",
        UpdateMillisec=123,
        LastPrice=100,
        BidPrice1=99,
        AskPrice1=101,
    )
    row.update(changes)
    quote = quote_to_unified(row)
    assert quote.timestamp == 0
    assert quote.update_time == ""
    assert quote.extra["event_time_status"] == "unknown"
    assert not quote.is_valid()
    # 调用实际新鲜度门禁，未知时间不能成为可用的新行情。
    assert not OrderTracker._price_is_fresh(None, quote, time.time())


def test_order_uses_insert_date_and_keeps_trading_day_identity():
    order = order_to_unified(
        dict(InsertDate="20260904", InsertTime="21:05:00", TradingDay="20260907"),
        trading_day="20260907",
        front_id=1,
        session_id=2,
    )
    assert order.create_time == "2026-09-04T21:05:00+08:00"
    assert order.extra["trading_day"] == "20260907"
    assert order.update_time.endswith("+08:00")


def test_trade_preserves_hedge_flag_and_trading_day_for_account_columns():
    trade = trade_to_unified(
        {
            "TradeID": "t1",
            "InstrumentID": "rb2610",
            "TradingDay": "20261019",
            "TradeDate": "20261016",
            "TradeTime": "21:01:02",
            "Direction": "0",
            "OffsetFlag": "0",
            "HedgeFlag": "3",
            "Price": 3500,
            "Volume": 1,
        },
        trading_day="20261019",
        front_id=1,
        session_id=2,
        resolved_order_id="one",
    )
    assert trade.extra["hedge_flag"] == "3"
    assert trade.extra["trading_day"] == "20261019"
    assert "2026-10-16" in trade.trade_time
