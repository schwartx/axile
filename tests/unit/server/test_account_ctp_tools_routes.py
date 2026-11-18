"""通过账户路由和 worker 验证 CTP 只读工具。"""

from threading import local
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.instrument_catalog import InstrumentCatalog
from axile.executor.instrument_catalog_records import Instrument
from axile.server.api.routes import account_ctp_tools, instrument_catalog
from axile.server.execution.worker_backend import worker
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.unit.server._execution_test_support import build_account


@pytest.fixture
def ctp_client(monkeypatch):
    pytest.importorskip("openctp_ctp")
    from axile.executor.ctp.ctp_execute import CTPExecutor

    account = build_account(id=41)
    monkeypatch.setattr(account_ctp_tools, "_get_account_snapshot_or_404", AsyncMock(return_value=account))
    manager = WorkerBackendManager()
    monkeypatch.setattr(account_ctp_tools, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    monkeypatch.setattr(worker, "_finalize_executor", lambda _: None)

    executor = object.__new__(CTPExecutor)
    executor._config = lambda: SimpleNamespace(broker_id="9999", investor_id="test", app_id="app", auth_code="auth")
    executor._instruments = {"rb2701": SimpleNamespace(ExchangeID="SHFE")}
    executor._timeout = 15
    executor._catalog_query_state = local()

    def resolve(**options):
        assert options["account"].id == 41 and not options["allow_connect"] and not options["allow_recovery"]
        return executor

    def request(account_id, message, _timeout):
        assert account_id == 41 and message.execution_id is None
        return worker._handle_worker_request(message, _WorkerBackendState())

    monkeypatch.setattr(worker, "_resolve_prepared_executor", resolve)
    monkeypatch.setattr(manager, "_request_blocking", request)
    app = FastAPI()
    app.include_router(account_ctp_tools.router)
    with TestClient(app) as client:
        yield client, executor


@pytest.mark.parametrize("relative", [0, 1])
def test_margin_query_uses_current_account_and_returns_cabinet_values(ctp_client, relative):
    client, executor = ctp_client
    row = SimpleNamespace(
        InstrumentID="rb2701",
        HedgeFlag="1",
        LongMarginRatioByMoney=0.125,
        LongMarginRatioByVolume=0.0,
        ShortMarginRatioByMoney=0.14,
        ShortMarginRatioByVolume=2.5,
        IsRelative=relative,
    )
    calls = []
    rows = [row]

    def query(name, request, *, symbol, timeout):
        calls.append((name, request.BrokerID, request.InvestorID, request.InstrumentID, request.HedgeFlag))
        assert symbol == "rb2701" and request.ExchangeID == "SHFE" and timeout == 5
        return rows

    executor._query = query

    assert client.get("/41/margin-rates", params={"symbol": ""}).status_code == 422
    response = client.get("/41/margin-rates", params={"symbol": "rb2701"})
    rows.clear()
    empty = client.get("/41/margin-rates", params={"symbol": "rb2701"})
    assert empty.status_code == 502
    assert "柜台未返回唯一的合约投机保证金参数" in empty.json()["detail"]["message"]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "instrument_id": "rb2701",
        "is_relative": bool(relative),
        "long_ratio": 0.125,
        "long_per_lot": 0.0,
        "short_ratio": 0.14,
        "short_per_lot": 2.5,
    }
    assert calls == [("ReqQryInstrumentMarginRate", "9999", "test", "rb2701", "1")] * 2


def test_commission_query_preserves_open_close_and_close_today(ctp_client):
    client, executor = ctp_client

    def query(name, request, *, symbol, timeout):
        assert name == "ReqQryInstrumentCommissionRate" and timeout == 3
        assert request.InvestorID == "test" and request.InstrumentID == symbol == "rb2701"
        return [
            SimpleNamespace(InstrumentID="rb"),
            SimpleNamespace(
                InstrumentID="rb2701",
                OpenRatioByMoney=0.0001,
                OpenRatioByVolume=1.5,
                CloseRatioByMoney=0,
                CloseRatioByVolume=2,
                CloseTodayRatioByMoney=0,
                CloseTodayRatioByVolume=0,
            ),
        ]

    executor._query = query
    response = client.get("/41/commission-rates", params={"symbol": "rb2701"})
    assert response.status_code == 200
    assert response.json() == {
        "instrument_id": "rb2701",
        "open_ratio": 0.0001,
        "open_per_lot": 1.5,
        "close_ratio": 0,
        "close_per_lot": 2,
        "close_today_ratio": 0,
        "close_today_per_lot": 0,
    }


def test_settlement_query_only_reads_and_reassembles_raw_statement(ctp_client):
    client, executor = ctp_client
    days = []

    def query(name, request, *, timeout):
        assert name == "ReqQrySettlementInfo" and timeout == 10
        assert request.InvestorID == "test"
        days.append(request.TradingDay)
        common = {"TradingDay": "20261117", "SettlementID": 1, "CurrencyID": "CNY", "AccountID": "test"}
        return [
            SimpleNamespace(**common, SequenceNo=2, Content="手续费  ￥20\r\n"),
            SimpleNamespace(**common, SequenceNo=1, Content="期货结算单\r\n"),
        ]

    executor._query = query
    assert client.get("/41/settlement-statement", params={"trading_day": "bad"}).status_code == 422
    response = client.get("/41/settlement-statement", params={"trading_day": "2026-11-17"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "documents": [
            {
                "trading_day": "20261117",
                "settlement_id": 1,
                "currency": "CNY",
                "content": "期货结算单\r\n手续费  ￥20\r\n",
            }
        ]
    }
    assert client.get("/41/settlement-statement").status_code == 200
    assert client.get("/41/settlement-statement", params={"trading_day": "2026-11-16"}).json() == {"documents": []}
    assert days == ["20261117", "", "20261116"]


def test_margin_suggestions_only_use_cached_current_futures(monkeypatch):
    catalog = InstrumentCatalog()
    rows = [
        Instrument("SHFE", "rb2701", "FUTURE"),
        Instrument("SHFE", "rb2701C3000", "OPTION"),
        Instrument("SHFE", "rb2401", "FUTURE", expiry="20000101"),
        Instrument("DCE", "i2701", "FUTURE"),
    ]
    catalog._rows = {row.key: row for row in rows}
    manager = SimpleNamespace(instrument_catalog=catalog)
    monkeypatch.setattr(instrument_catalog, "get_worker_backend_manager", lambda: manager)
    app = FastAPI()
    app.include_router(instrument_catalog.router)

    with TestClient(app) as client:
        response = client.get("/instrument-catalog/search", params={"prefix": "RB"})
        assert response.json() == {"data": [{"symbol": "rb2701", "exchange": "SHFE"}], "cache_ready": True}
        assert client.get("/instrument-catalog/search", params={"prefix": "xx"}).json() == {
            "data": [],
            "cache_ready": True,
        }
        catalog._rows.clear()
        assert client.get("/instrument-catalog/search", params={"prefix": "rb"}).json() == {
            "data": [],
            "cache_ready": False,
        }
