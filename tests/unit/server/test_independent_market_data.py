"""从 API 验证 CTP 行情可以独立于交易 worker 运行。"""

import asyncio
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.channels import get_channel
from axile.channels.gm_sessions import SHANGHAI
from axile.common.trade_channel import TradeChannel
from axile.executor import gm_market_data
from axile.executor.instrument_catalog_records import Instrument
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.api.routes import market_data, portfolio
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.execution.worker_backend import manager as workers
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.market_data import close_market_data
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_portfolio_routes import _account_payload


def test_quote_route_fills_missing_specs_and_recovers_market_subscription(monkeypatch):
    pytest.importorskip("openctp_ctp")
    from axile.executor.ctp import market_data as ctp
    from axile.server.api.main import api_router

    class Feed:
        def __init__(self):
            self.subscriptions = []
            self.closed = False

        def RegisterSpi(self, spi):
            self.spi = spi

        def RegisterFront(self, addr):
            assert addr == "tcp://quotes:15000"

        def Init(self):
            self.spi.OnFrontConnected()

        def ReqUserLogin(self, request, request_id):
            assert (request.BrokerID, request.UserID, request.Password) == ("9999", "fixture", "example")
            self.spi.OnRspUserLogin(None, None, request_id, True)
            return 0

        def SubscribeMarketData(self, symbols, count):
            assert count == 1 and symbols == [b"rb2701"]
            self.subscriptions.append(symbols)
            row = SimpleNamespace(
                InstrumentID="rb2701",
                ExchangeID="",
                LastPrice=3500 + len(self.subscriptions) - 1,
                BidPrice1=3499,
                AskPrice1=3501,
                BidVolume1=10,
                AskVolume1=20,
                ActionDay="20261118",
                TradingDay="20261118",
                UpdateTime="14:29:59",
            )
            self.spi.OnRspSubMarketData(row, None, 0, True)
            self.spi.OnRtnDepthMarketData(row)
            return 0

        def Release(self):
            self.closed = True

    account = SimpleNamespace(
        id=41,
        trade_channel="ctp",
        archived_at=None,
        account_config={
            "broker_id": "9999",
            "investor_id": "fixture",
            "password": "example",
            "md_front": "tcp://quotes:15000",
        },
    )
    monkeypatch.setattr(market_data, "_get_account_snapshot_or_404", AsyncMock(return_value=account))
    manager = WorkerBackendManager()
    catalog = manager.instrument_catalog
    monkeypatch.setattr(market_data, "get_worker_backend_manager", lambda: manager)

    def check_trade_session(_account):
        if not manager._workers:
            raise AccountConnectionUnavailable("交易未连接")

    monkeypatch.setattr(manager, "_check_read_session", check_trade_session)
    feed = Feed()
    monkeypatch.setattr(ctp.md.CThostFtdcMdApi, "CreateFtdcMdApi", lambda _: feed)
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        status = "/market-data/accounts/41/status"
        quotes = "/market-data/accounts/41/quotes"
        payload = {"symbols": ["rb2701"]}
        assert client.get(status).json() == {"status": "uninitialized"}
        assert client.post(quotes, json={"symbols": [" "]}).status_code == 422
        response = client.post(quotes, json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        quote = response.json()["data"]["rb2701"]
        assert quote["last_price"] == 3500
        assert quote["bids"] == [[3499, 10]]
        assert quote["asks"] == [[3501, 20]]
        assert quote["extra"]["source"] == "ctp.market_data"
        assert "volume_multiple" not in quote["extra"]
        assert client.get(status).json() == {"status": "ready"}

        instrument = Instrument(exchange="SHFE", symbol="rb2701", kind="FUTURE", multiplier=10, tick=1)
        queries = []

        def lookup(symbols, _timeout):
            queries.append(symbols)
            return [instrument]

        catalog.register("fixture", lookup, channel="ctp")
        handle = SimpleNamespace(catalog=SimpleNamespace(source="fixture"))
        manager._workers[account.id] = handle
        monkeypatch.setattr(manager, "_is_handle_healthy", lambda value: value is handle)
        fetched = client.post(quotes, json=payload).json()["data"]["rb2701"]
        assert fetched["extra"]["volume_multiple"] == 10
        assert fetched["extra"]["price_tick"] == 1
        assert queries == [[instrument.key]]

        catalog.unregister("fixture")
        manager._workers.clear()
        qualified = client.post(quotes, json={"symbols": [instrument.key]})
        assert qualified.status_code == 200
        cached = qualified.json()["data"][instrument.key]
        assert cached["last_price"] == 3500
        assert cached["extra"]["volume_multiple"] == 10
        assert cached["extra"]["price_tick"] == 1
        assert client.post(quotes, json={"symbols": ["DCE.rb2701"]}).status_code == 503
        assert len(feed.subscriptions) == 1

        feed.spi.OnFrontDisconnected(1)
        assert client.get(status).json() == {"status": "disconnected"}
        feed.spi.OnFrontConnected()
        assert client.post(quotes, json=payload).json()["data"]["rb2701"]["last_price"] == 3501
        assert len(feed.subscriptions) == 2
    close_market_data()
    manager.close()
    assert feed.closed


def test_quote_route_can_import_old_ctp_reference_without_waiting_for_a_tick(monkeypatch):
    pytest.importorskip("openctp_ctp")
    from axile.executor.ctp.ctp_execute import CTPExecutor
    from axile.server.execution.worker_backend import worker
    from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
    from tests.unit.executor.ctp._quote_test_support import fresh_quote

    account = build_account(id=41)
    manager = WorkerBackendManager()
    executor = CTPExecutor(TradeChannel("ctp"))
    state = _WorkerBackendState()
    quote = fresh_quote("rb2701", "20261124")
    quote.timestamp -= 30 * 60 * 1000
    quote.update_time = "2026-11-24T15:00:00.500000+08:00"
    executor._valuation_quotes[quote.symbol] = quote
    executor._instruments[quote.symbol] = SimpleNamespace(ExchangeID="SHFE")
    instrument = Instrument(exchange="SHFE", symbol=quote.symbol, kind="FUTURE", multiplier=10, tick=1)
    manager.instrument_catalog.register("fixture", lambda _symbols, _timeout: [instrument], channel="ctp")
    manager.instrument_catalog.ensure([instrument.key], require_specs=True, timeout=2)

    read_market = Mock(side_effect=TimeoutError("等待 CTP 行情回报超时"))
    monkeypatch.setattr(market_data, "read_market_data", read_market)
    monkeypatch.setattr(market_data, "_get_account_snapshot_or_404", AsyncMock(return_value=account))
    monkeypatch.setattr(market_data, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(manager, "_check_read_session", lambda account: None)
    monkeypatch.setattr(
        manager, "_request_blocking", lambda account_id, request, *args: worker._handle_worker_request(request, state)
    )
    monkeypatch.setattr(worker, "_resolve_prepared_executor", lambda **kwargs: executor)
    monkeypatch.setattr(worker, "_finalize_with_orders", lambda *args: None)
    monkeypatch.setattr(executor, "_require_session_ready", lambda: None)

    app = FastAPI()
    app.include_router(market_data.router)
    route = "/market-data/accounts/41/quotes"
    payload = {"symbols": [instrument.key]}
    try:
        with TestClient(app) as client:
            response = client.post(route, json={**payload, "allow_stale": True})
            assert response.status_code == 200
            imported = response.json()["data"][instrument.key]
            assert imported["timestamp"] == quote.timestamp
            assert imported["last_price"] == quote.last_price
            assert imported["extra"]["valuation_reference"] is True
            assert imported["extra"]["volume_multiple"] == 10
            read_market.assert_not_called()
            assert client.post(route, json=payload).status_code == 504

            monkeypatch.setattr(
                manager, "_check_read_session", Mock(side_effect=AccountConnectionUnavailable("未连接"))
            )
            read_market.side_effect = None
            read_market.return_value = {instrument.key: quote}
            assert client.post(route, json={**payload, "allow_stale": True}).status_code == 200
    finally:
        manager.close()


def test_cached_contract_specs_stay_available_after_trade_disconnect(monkeypatch):
    account = SimpleNamespace(id=41, trade_channel="ctp")
    instrument = Instrument(exchange="SHFE", symbol="rb2701", kind="FUTURE", multiplier=10, tick=1)
    manager = WorkerBackendManager()

    def no_trade_session(_account):
        raise AccountConnectionUnavailable("交易未连接")

    try:
        manager.instrument_catalog.register("fixture", lambda _symbols, _timeout: [instrument], channel="ctp")
        manager.instrument_catalog.ensure([instrument.key], require_specs=True, timeout=2)
        manager.instrument_catalog.unregister("fixture")
        monkeypatch.setattr(manager, "_check_read_session", no_trade_session)
        assert asyncio.run(manager.query_instruments(account, [instrument.key], require_specs=True)) == {
            instrument.key: instrument.wire()
        }
        with pytest.raises(AccountConnectionUnavailable, match="交易未连接"):
            asyncio.run(manager.query_instruments(account, ["SHFE.rb2702"], require_specs=True))
        with pytest.raises(AccountConnectionUnavailable, match="交易未连接"):
            asyncio.run(manager.query_instruments(account, [instrument.key], use_cache=False))
    finally:
        manager.close()


def _gm_fixture_process(pipe, config):
    import gm.api

    tokens = []
    addresses = []

    def forbidden(*args, **kwargs):
        raise AssertionError("不得启动策略或登录交易")

    def current(*, symbols, include_call_auction):
        assert include_call_auction is True
        assert tokens == ["fixture-token"] and addresses == ["fixture:7001"]
        if symbols == ["SZSE.000002"]:
            Event().wait()
        return [
            {
                "symbol": symbol,
                "created_at": datetime(2026, 11, 18, 10, 0, tzinfo=SHANGHAI),
                "price": 12,
                "last_volume": 100,
                "quotes": [{"bid_p": 11.99, "bid_v": 2, "ask_p": 12.01, "ask_v": 3}],
            }
            for symbol in symbols
        ]

    gm.api.set_token = tokens.append
    gm.api.set_serv_addr = addresses.append
    gm.api.current = current
    gm.api.set_account_id = gm.api.run = forbidden
    gm_market_data._gm_quotes_worker(pipe, config)


def test_gm_public_quotes_use_separate_process_and_recover_after_timeout(monkeypatch):
    pytest.importorskip("gm.api")
    monkeypatch.setattr(gm_market_data, "_gm_quotes_worker", _gm_fixture_process)
    configure = get_channel("gm").configure_market_data
    assert configure is not None
    account = {"token": "fixture-token", "connection_mode": "service", "serv_addr": "fixture:7001"}
    config = configure(account)
    assert config == configure({**account, "account_id": "another-account"})
    source = config.open()
    assert isinstance(source, gm_market_data.GMMarketData)
    try:
        assert source.status() == "uninitialized"
        key = "000001.SZ"
        quote = source.snapshot([key], timeout=5)[key]
        assert (quote.last_price, quote.bids, quote.asks) == (12, [(11.99, 2)], [(12.01, 3)])
        assert quote.extra["source"] == "gm.current"
        assert source.status() == "ready"
        first = source._process
        assert source.snapshot(["SZSE.000001"], timeout=2)["SZSE.000001"].symbol == key
        assert source._process is first
        with pytest.raises(TimeoutError):
            source.snapshot(["SZSE.000002"], timeout=0.05)
        assert source.status() == "unavailable" and source._process is None
        assert source.snapshot([key], timeout=5)[key].last_price == 12
        assert source._process is not first
    finally:
        source.close()
    assert source.status() == "disconnected"


def test_real_account_trial_reads_only_the_data_used_by_the_function(monkeypatch):
    account = _account_payload(41) | {"long_leverage": 3.0}
    backend = WorkerBackendManager()
    queries = []

    def disconnected(_account):
        raise AccountConnectionUnavailable("交易未连接")

    def read_quotes(channel, values, symbols, *, timeout):
        queries.append((channel, symbols))
        assert 0 < timeout <= 5
        return {
            "rb2701": UnifiedPriceData(
                symbol="rb2701",
                last_price=3500,
                volume=1,
                bids=[(3499, 1)],
                asks=[(3501, 2)],
                timestamp=1,
                update_time="2026-11-18T10:00:00+08:00",
            )
        }

    def forbidden(*args, **kwargs):
        raise AssertionError("不得创建交易 worker")

    monkeypatch.setattr(portfolio, "_portfolio_native", AsyncMock(return_value=account))
    monkeypatch.setattr(workers, "get_worker_backend_manager", lambda: backend)
    monkeypatch.setattr(workers, "read_market_data", read_quotes)
    monkeypatch.setattr(backend, "_check_read_session", disconnected)
    monkeypatch.setattr(backend, "_request_blocking", forbidden)
    app = FastAPI()
    app.include_router(portfolio.router)
    with TestClient(app) as api:

        def trial(code):
            return api.post(
                "/portfolio/validate_custom_calc",
                json={"custom_calc_py_code": "def calculate_portfolio(context):\n" + code, "account_id": 41},
            ).json()

        fixed = trial("    return {'rb2701': context.settings.long_leverage}")
        assert fixed["valid"] and fixed["target"] == {"rb2701": 3} and queries == []
        market = trial(
            "    p = context.get_quote('rb2701')\n"
            "    return {'rb2701': (context.get_price('rb2701') - p.bids[0][0]) / p.asks[0][1]}"
        )
        assert market["valid"] and market["target"] == {"rb2701": 0.5}
        assert queries == [("ctp", ["rb2701"])]
        trade = trial("    return {'rb2701': len(context.positions)}")
        assert not trade["valid"] and trade["error_line"] == 2
        assert "需要先连接交易账户" in trade["error_message"]
    with pytest.raises(AccountConnectionUnavailable):
        asyncio.run(backend.calculate_portfolio(SimpleNamespace(id=41), "code"))
    backend.close()
