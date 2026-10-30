"""异常夜盘日期的纯解析及真实 SPI 离线回归。"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.ctp.quote_time import resolve_quote_time
from axile.executor.ctp_product_sessions import get_ctp_product_sessions
from axile.executor.trading_calendar import ShinnyTradingCalendar
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker


@pytest.mark.parametrize(
    "day,clock,natural,status",
    [
        ("20260915", "21:01:02", "2026-09-14", "normalized"),
        ("20260914", "21:01:02", "2026-09-11", "normalized"),
        ("20260914", "00:01:02", "2026-09-12", "normalized"),
        ("20260915", "00:01:02", "2026-09-15", "native"),
        ("20260915", "09:01:02", "2026-09-15", "native"),
        ("20261008", "21:01:02", "", "unknown"),
        ("20270914", "21:01:02", "", "unknown"),
    ],
)
def test_night_dates(day, clock, natural, status):
    native = datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d") + f"T{clock}.123000+08:00"
    value, actual, _ = resolve_quote_time(
        native,
        day,
        get_ctp_product_sessions("SHFE", "ag"),
        lambda day: ShinnyTradingCalendar().is_open("china", day),
    )
    assert actual == status
    assert value[:10] == natural
    if value:
        assert value[10:] == native[10:]


@pytest.mark.parametrize("product,native", [("ag", "2026-09-11T21:01:02+08:00"), ("wr", "2026-09-14T21:01:02+08:00")])
def test_native_dates_and_no_night_unchanged(product, native):
    assert resolve_quote_time(native, "20260914", get_ctp_product_sessions("SHFE", product), lambda _: None) == (
        native,
        "native",
        "",
    )


@pytest.mark.parametrize("calendar", [lambda _: None, lambda _: False, lambda _: 1 / 0])
def test_calendar_failure_never_guesses(calendar):
    assert (
        resolve_quote_time("2026-09-14T21:01:02+08:00", "20260914", get_ctp_product_sessions("SHFE", "ag"), calendar)[1]
        == "unknown"
    )


def test_missing_and_ambiguous_sessions_and_invalid_time():
    native = "2026-09-14T21:01:02+08:00"
    sessions = get_ctp_product_sessions("SHFE", "ag")
    for table in ((), sessions + sessions):
        assert resolve_quote_time(native, "20260914", table, lambda _: True)[1] == "unknown"
    assert resolve_quote_time("", "20260914", sessions, lambda _: True)[1] == "unknown"


@pytest.fixture
def broker(monkeypatch):
    driver = ScriptedBroker(monkeypatch, day="20260915")

    def login(_request, _rid):
        driver.market_spi.OnRspUserLogin(SimpleNamespace(TradingDay=driver.day), None, 0, True)
        return 0

    driver.market.ReqUserLogin.side_effect = login
    yield driver
    driver.executor.close()


@pytest.mark.parametrize("error,day", [(True, "20260915"), (False, "20260916")])
def test_zero_login_rejects_error_and_day_mismatch(broker, error, day):
    def login(_request, _rid):
        broker.market_spi.OnRspUserLogin(
            SimpleNamespace(TradingDay=day), SimpleNamespace(ErrorID=3, ErrorMsg="denied") if error else None, 0, True
        )
        return 0

    broker.market.ReqUserLogin.side_effect = login
    with pytest.raises(CtpSessionRecoveryRequired, match="denied" if error else "交易日"):
        broker.start()


def test_zero_login_unsent_wrong_duplicate_and_invalid(broker):
    executor = broker.executor
    row = SimpleNamespace(TradingDay=broker.day)
    executor._market_logged_in(row, None, 0)
    assert not executor._md_login.done.is_set()
    executor._md_login.request_id = 123
    executor._market_logged_in(row, None, 999)
    assert not executor._md_login.done.is_set()
    broker.start()
    broker.market_spi.OnRspUserLogin(SimpleNamespace(TradingDay="bad"), None, 0, True)
    assert executor._verify_connection()
    executor._disconnected("行情", 1)
    broker.market_spi.OnRspUserLogin(row, None, 0, True)
    assert not executor._verify_connection()


def emit(broker, clock="21:01:02", day="20260915", trading_day="20260915"):
    broker.market_spi.OnRtnDepthMarketData(
        SimpleNamespace(
            InstrumentID="ag2612",
            TradingDay=trading_day,
            ActionDay=day,
            UpdateTime=clock,
            UpdateMillisec=123,
            LastPrice=9000,
            BidPrice1=8999,
            AskPrice1=9001,
            BidVolume1=1,
            AskVolume1=1,
            LowerLimitPrice=8000,
            UpperLimitPrice=10000,
        )
    )


def test_zero_login_normalized_quote_passes_gates_and_preserves_newest(broker, monkeypatch):
    executor = broker.start()
    now = datetime.fromisoformat("2026-09-14T21:01:02.124+08:00").timestamp()
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time", lambda: now)
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time_ns", lambda: round(now * 1_000_000) * 1000)
    executor.initialize_websocket(["ag2612"])
    emit(broker)
    q = executor.get_market_data(["ag2612"])["ag2612"]
    assert q.extra["event_time_status"] == "normalized"
    assert q.extra["event_date"] == "20260915"
    assert q.extra["event_millisec"] == 123
    assert q.update_time == "2026-09-14T21:01:02.123000+08:00"
    executor._call_trader_request(
        "ReqOrderInsert",
        SimpleNamespace(InstrumentID="ag2612", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
    )
    broker.trader.ReqOrderInsert.assert_called_once()
    for clock in ("21:01:01", "21:00:00", "21:02:00", "invalid"):
        emit(broker, clock)
        assert executor._quotes["ag2612"] is q
    emit(broker, trading_day="20260916")
    assert not executor._verify_connection()


@pytest.mark.parametrize("clock,direction", [("21:02:00", "future"), ("21:00:00", "past")])
def test_timeout_includes_time_evidence(broker, monkeypatch, clock, direction):
    executor = broker.start()
    now = datetime.fromisoformat("2026-09-14T21:01:02.124+08:00").timestamp()
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time", lambda: now)
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.time_ns", lambda: round(now * 1_000_000) * 1000)
    executor.initialize_websocket(["ag2612"])
    emit(broker, clock)
    executor._timeout = 0
    with pytest.raises(TimeoutError, match=direction) as error:
        executor.get_market_data(["ag2612"])
    assert "stale_exchange_time" in str(error.value)
    assert "20260915" in str(error.value)


@pytest.mark.parametrize("state", [None, "exception"])
def test_injected_calendar_never_falls_back(broker, state):
    executor = broker.start()
    executor.initialize_websocket(["ag2612"])

    def is_open(_calendar, _day):
        if state == "exception":
            raise RuntimeError("offline")
        return state

    executor._trading_calendar = SimpleNamespace(is_open=is_open)
    emit(broker)
    quote = executor._quotes["ag2612"]
    assert quote.timestamp == 0
    assert quote.extra["event_time_status"] == "unknown"
    assert quote.extra["event_time_reason"] == "calendar_unavailable"


@pytest.mark.parametrize("timezone", ["UTC", "America/New_York", "Asia/Shanghai"])
def test_host_timezone_does_not_change_normalization(broker, monkeypatch, timezone):
    import os
    import time

    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", timezone)
    time.tzset()
    try:
        executor = broker.start()
        executor.initialize_websocket(["ag2612"])
        emit(broker)
        assert executor._quotes["ag2612"].timestamp == int(
            datetime.fromisoformat("2026-09-14T21:01:02.123+08:00").timestamp() * 1000
        )
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
