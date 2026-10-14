"""持仓估值在活跃与休市时段选取 tick 和查询快照。"""

import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from axile.common.trade_channel import TradeChannel
from axile.executor.ctp.converters import account_to_unified
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.ctp.spi import TraderSpi
from tests.unit.executor.ctp._quote_test_support import fresh_quote


def _executor(*, closed: bool) -> CTPExecutor:
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._lock = threading.RLock()
    executor._catalog_query_state = threading.local()
    executor._timeout = 0.01
    executor._instruments = {"rb2610": SimpleNamespace(ExchangeID="SHFE", VolumeMultiple=10)}
    executor._valuation_quotes = {}
    executor.initialize_websocket = Mock()
    executor._get_ctp_session_block_reason = Mock(return_value="CTP.SESSION.CLOSED" if closed else None)
    executor._query_valuation_snapshot = Mock(return_value=None)
    executor._fresh_valuation_quote_error = Mock(return_value="stale_exchange_time")
    return executor


def _position_rows() -> list[object]:
    return [SimpleNamespace(InstrumentID="rb2610", PosiDirection="2", Position=2, TodayPosition=1, PositionCost=7000)]


def test_closed_contract_uses_cached_last_price_without_waiting_or_querying() -> None:
    executor = _executor(closed=True)
    executor._valuation_quotes["rb2610"] = fresh_quote("rb2610", "20260909")

    quotes, errors = executor._position_valuation_quotes(_position_rows())
    assets = account_to_unified(SimpleNamespace(Balance=10000), _position_rows(), executor._instruments, quotes=quotes)

    assert errors == {}
    assert assets.market_value == 60720
    assert assets.positions[0].extra["valuation_reference"] is True
    executor.initialize_websocket.assert_not_called()
    executor._query_valuation_snapshot.assert_not_called()


def test_closed_contract_queries_snapshot_when_no_cached_price() -> None:
    executor = _executor(closed=True)
    executor._query_valuation_snapshot.return_value = fresh_quote("rb2610", "20260909")

    quotes, errors = executor._position_valuation_quotes(_position_rows())

    assert errors == {}
    assert quotes["rb2610"].last_price == 3036
    executor.initialize_websocket.assert_not_called()
    executor._query_valuation_snapshot.assert_called_once_with("rb2610")


def test_open_contract_queries_snapshot_after_tick_wait_expires() -> None:
    executor = _executor(closed=False)
    executor._query_valuation_snapshot.return_value = fresh_quote("rb2610", "20260909")

    quotes, errors = executor._position_valuation_quotes(_position_rows())

    assert errors == {}
    assert quotes["rb2610"].last_price == 3036
    executor.initialize_websocket.assert_called_once_with(["rb2610"])
    executor._query_valuation_snapshot.assert_called_once_with("rb2610")


def test_open_contract_preserves_cached_price_when_snapshot_fails() -> None:
    executor = _executor(closed=False)
    executor._valuation_quotes["rb2610"] = fresh_quote("rb2610", "20260909")

    quotes, errors = executor._position_valuation_quotes(_position_rows())

    assert errors == {}
    assert quotes["rb2610"].last_price == 3036
    assert quotes["rb2610"].extra["valuation_reference"] is True
    executor._query_valuation_snapshot.assert_called_once_with("rb2610")


def test_one_sided_tick_updates_valuation_without_replacing_order_quote() -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor._trading_day = "20260909"
    executor._subscriptions.add("rb2610")
    previous = fresh_quote("rb2610", "20260909")
    previous.timestamp = 1
    executor._quotes["rb2610"] = previous
    executor._normalize_quote_time = Mock()
    executor._snapshot_quote_error = Mock(
        side_effect=lambda _symbol, quote, **_kwargs: "missing_two_sided_book" if quote.last_price == 3200 else None
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    executor._on_quote(
        SimpleNamespace(
            InstrumentID="rb2610",
            TradingDay="20260909",
            ActionDay=now.strftime("%Y%m%d"),
            UpdateTime=now.strftime("%H:%M:%S"),
            LastPrice=3200,
        )
    )

    assert executor._valuation_quotes["rb2610"].last_price == 3200
    assert executor._quotes["rb2610"] is previous


def test_snapshot_query_extracts_last_price_and_keeps_source() -> None:
    executor = _executor(closed=True)
    executor._query = Mock(
        return_value=[
            SimpleNamespace(
                InstrumentID="rb2610",
                TradingDay="20260909",
                ActionDay="20260909",
                UpdateTime="14:59:59",
                LastPrice=3200,
            )
        ]
    )
    executor._normalize_quote_time = Mock()

    quote = CTPExecutor._query_valuation_snapshot(executor, "rb2610")

    assert quote is not None
    assert quote.last_price == 3200
    assert quote.extra["valuation_source"] == "ctp_snapshot_last_price"
    assert executor._valuation_quotes["rb2610"] is quote
    name, request = executor._query.call_args.args
    assert name == "ReqQryDepthMarketData"
    assert request.InstrumentID == "rb2610"
    assert executor._query.call_args.kwargs == {"symbol": "rb2610", "timeout": 0.01}


def test_snapshot_query_routes_trader_response_through_pending_request() -> None:
    executor = _executor(closed=True)
    executor._query_lock = threading.Lock()
    executor._pending_queries = {}
    executor._catalog_progress = Mock()
    sent = []

    def send(operation, name, request, *, symbol, before_send):
        sent.append((operation, name, symbol, request.InstrumentID))
        before_send(7)
        TraderSpi(executor).OnRspQryDepthMarketData(
            SimpleNamespace(InstrumentID="rb2610", LastPrice=3200), None, 7, True
        )

    executor._send_trader_request = Mock(side_effect=send)

    rows = executor._query(
        "ReqQryDepthMarketData",
        SimpleNamespace(InstrumentID="rb2610"),
        symbol="rb2610",
        timeout=0.01,
    )

    assert sent == [("query_depth_market_data", "ReqQryDepthMarketData", "rb2610", "rb2610")]
    assert rows[0].LastPrice == 3200
