"""郑商所夜盘交易日归一化与行情、换日保护回归。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.ctp.quote_time import resolve_quote_trading_day
from axile.executor.ctp_product_sessions import get_ctp_product_sessions
from axile.executor.trading_calendar import ShinnyTradingCalendar
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker


@pytest.mark.parametrize(
    "exchange,product,native,raw,expected",
    [
        ("CZCE", "CF", "2026-11-16T21:24:40+08:00", "20261116", "20261117"),
        ("CZCE", "SR", "2026-11-13T21:24:40+08:00", "20261113", "20261116"),
        ("CZCE", "CF", "2026-11-16T21:24:40+08:00", "20261117", "20261117"),
        ("CZCE", "CF", "2026-11-16T09:24:40+08:00", "20261116", "20261116"),
        ("CZCE", "AP", "2026-11-16T21:24:40+08:00", "20261116", "20261116"),
        ("CZCE", "CF", "2026-11-16T23:00:00+08:00", "20261116", "20261116"),
        ("SHFE", "ag", "2026-11-16T21:24:40+08:00", "20261116", "20261116"),
        ("DCE", "a", "2026-11-17T21:24:40+08:00", "20261117", "20261117"),
        ("CZCE", "CF", "2026-09-30T21:24:40+08:00", "20260930", ""),
        ("CZCE", "CF", "2026-11-14T21:24:40+08:00", "20261114", ""),
        ("CZCE", "CF", "2027-11-16T21:24:40+08:00", "20271116", ""),
        ("CZCE", "CF", "", "20261116", "20261116"),
        ("CZCE", "CF", "2026-11-16T21:24:40+08:00", "bad", "bad"),
    ],
)
def test_quote_trading_day(exchange, product, native, raw, expected):
    calendar = ShinnyTradingCalendar()
    assert (
        resolve_quote_trading_day(
            native, raw, get_ctp_product_sessions(exchange, product), lambda day: calendar.is_open("china", day)
        )
        == expected
    )


@pytest.mark.parametrize("state", [None, "exception"])
def test_czce_calendar_failure_never_guesses(state):
    def calendar(_day):
        if state == "exception":
            raise RuntimeError("offline")
        return state

    assert (
        resolve_quote_trading_day(
            "2026-11-16T21:24:40+08:00", "20261116", get_ctp_product_sessions("CZCE", "CF"), calendar
        )
        == ""
    )


def quote_row(*, day="20261116", trading_day=None, clock="21:24:40", **overrides):
    values = dict(
        InstrumentID="CF701",
        TradingDay=day if trading_day is None else trading_day,
        ActionDay=day,
        UpdateTime=clock,
        UpdateMillisec=0,
        LastPrice=9000,
        BidPrice1=8999,
        AskPrice1=9001,
        BidVolume1=1,
        AskVolume1=1,
        LowerLimitPrice=8000,
        UpperLimitPrice=10000,
    )
    return SimpleNamespace(**(values | overrides))


@pytest.fixture
def broker(monkeypatch, request):
    driver = ScriptedBroker(monkeypatch, day=getattr(request, "param", "20261117"))
    driver.rows["ReqQryInstrument"].append(
        SimpleNamespace(
            InstrumentID="CF701",
            ExchangeID="CZCE",
            ProductID="CF",
            PriceTick=1,
            VolumeMultiple=5,
            ProductClass=td.THOST_FTDC_PC_Futures,
        )
    )
    now = datetime.fromisoformat("2026-11-16T21:24:41+08:00").timestamp()
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time", lambda: now)
    driver.start()
    driver.executor.initialize_websocket(["CF701"])
    yield driver
    driver.executor.close()


@pytest.mark.parametrize("trading_day", ["20261116", "20261117"])
def test_czce_night_quote_passes_sizing_and_order_gates(broker, trading_day):
    broker.market_spi.OnRtnDepthMarketData(quote_row(trading_day=trading_day))
    executor = broker.executor
    quote = executor.get_market_data(["CF701"])["CF701"]
    assert quote.extra["trading_day"] == "20261117"
    if trading_day == "20261116":
        assert quote.extra["raw_trading_day"] == "20261116"
    assert quote.update_time == "2026-11-16T21:24:40+08:00"
    assert executor.market_data_error(quote, purpose="sizing") is None
    executor._call_trader_request(
        "ReqOrderInsert",
        SimpleNamespace(InstrumentID="CF701", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
    )
    broker.trader.ReqOrderInsert.assert_called_once()


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"clock": "21:23:00"}, "stale_exchange_time"),
        ({"clock": "21:25:00"}, "stale_exchange_time"),
        ({"BidVolume1": 0}, "missing_two_sided_book"),
        ({"LowerLimitPrice": 0}, "missing_price_limits_or_tick"),
        ({"trading_day": "20261113"}, "trading_day_mismatch"),
        ({"trading_day": ""}, "trading_day_mismatch"),
        ({"clock": "invalid"}, "trading_day_mismatch"),
    ],
)
def test_czce_invalid_quote_still_blocks_order(broker, overrides, reason):
    executor = broker.executor
    broker.market_spi.OnRtnDepthMarketData(quote_row(**overrides))
    assert executor._quote_diagnostic("CF701")["last_rejection"]["reason"] == reason
    executor._timeout = 0
    with pytest.raises(TimeoutError, match="行情等待超时"):
        executor.get_market_data(["CF701"])
    broker.trader.ReqOrderInsert.assert_not_called()


@pytest.mark.parametrize("broker", ["20261116"], indirect=True)
def test_czce_night_rollover_invalidates_previous_day_session(broker):
    executor = broker.executor
    broker.market_spi.OnRtnDepthMarketData(quote_row())
    assert not executor._verify_connection()
    assert executor._quotes == {}
    with pytest.raises(CtpSessionRecoveryRequired):
        executor.get_market_data(["CF701"])
    broker.trader.ReqOrderInsert.assert_not_called()


def test_czce_snapshot_uses_same_trading_day_normalization(broker):
    executor = broker.executor
    executor._query = Mock(return_value=[quote_row()])
    quote = executor._query_valuation_snapshot("CF701")
    assert quote is not None
    assert quote.extra["trading_day"] == "20261117"
    assert quote.extra["raw_trading_day"] == "20261116"
    assert executor._fresh_valuation_quote_error(quote) is None
