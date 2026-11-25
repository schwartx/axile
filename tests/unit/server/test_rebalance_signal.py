"""用离线账户和行情快照验证调仓信号完整路径。"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.channels.rebalance_signal import SIGNAL_CHANNEL
from axile.common import feishu
from axile.common.default_account_notification import SIGNAL_NOTIFICATION_CODE
from axile.server.api.deps import get_scheduler
from axile.server.api.routes.account import router as account_router
from axile.server.api.routes.portfolio import router as portfolio_router
from axile.server.core import db
from axile.server.execution import intents, scheduler
from axile.server.execution.factory import create_executor_instance
from axile.server.repositories import get_account_snapshot
from axile.server.signal_service import run_scheduled_signal

TARGET = {"600000.SH": 300, "510300.SH": 500}
QUOTES = """
from axile.executor.models.unified_price import UnifiedPriceData

def fetch_quotes(symbols):
    return {
        symbol: UnifiedPriceData(
            symbol=symbol,
            last_price=10,
            bids=[(9.9, 200), (9.8, 400)],
            asks=[(10.2, 300), (10.3, 500)],
            volume=125,
            timestamp=1795417200000,
            update_time="2026-11-23T15:00:00+08:00",
        )
        for symbol in symbols
    }
"""
CALCULATE = f"""
def calculate_portfolio(context):
    assert context.get_price("510300.SH") == 10
    quote = context.get_quote("510300.SH")
    assert quote.book_valid
    assert quote.bids == [(9.9, 200), (9.8, 400)]
    assert quote.asks == [(10.2, 300), (10.3, 500)]
    assert quote.volume == 125
    assert quote.timestamp == 1795417200000
    assert context.account.total_asset == 10000
    assert not hasattr(context.executor, "place_order")
    return {{"mode": "quantity", "targets": {TARGET}}}
"""
MANUAL = {"total_asset": 10000, "available_cash": 8000, "currency": "CNY"}


@pytest.fixture
def client(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    app = FastAPI()
    app.include_router(account_router)
    app.include_router(portfolio_router)
    sched = AsyncIOScheduler()
    app.dependency_overrides[get_scheduler] = lambda: sched
    with TestClient(app) as test_client:
        yield test_client


def create_portfolio(client, market, code=CALCULATE):
    response = client.post("/portfolio/", json={"market": market, "name": market, "custom_calc_py_code": code})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def create_signal_account(client, portfolio_id):
    response = client.post(
        "/account/",
        json={
            "name": "离线信号",
            "market": "不采用请求中的市场",
            "trade_channel": SIGNAL_CHANNEL,
            "brokerage": SIGNAL_CHANNEL,
            "portfolio_id": portfolio_id,
            "account_config": {"manual_assets": MANUAL, "quote_code": QUOTES},
            "account_control_preset": "default",
            "is_started": True,
            "cron_expr": "0 0 1 1 *",
            "algorithm": {},
            "weight_precision": 0.01,
            "remark": None,
            "trade_rules": None,
            "forbidden_symbols": None,
            "risk_symbols": None,
            "write_empty_record": None,
            "feishu_key": None,
            "initial_deposit": {
                "direction": "deposit",
                "amount": 10000,
                "occurred_at": "2026-11-20T10:00:00+08:00",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_signal_schedule_persists_complete_targets_once_and_rejects_trade(client, native_database, monkeypatch):
    portfolio = create_portfolio(client, "A股")
    account = create_signal_account(client, portfolio)
    account_id = account["id"]
    assert account["market"] == "A股"
    flows = client.get(f"/account/{account_id}/cash-flows").json()
    assert flows["data"][0]["amount"] == 10000
    assert flows["currency"] == "CNY"
    assert client.get("/account/dashboard").status_code == 200

    preview = client.post(
        "/account/signal/preview",
        json={
            "portfolio_id": portfolio,
            "account_config": {"manual_assets": MANUAL, "quote_code": QUOTES},
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["target"] == {"mode": "quantity", "targets": TARGET}
    assert client.post(f"/account/execute/{account_id}").status_code == 409
    assert client.post(f"/account/empty_positions/{account_id}").status_code == 409
    assert client.post(f"/account/{account_id}/connection/reconnect").status_code == 409
    assert client.get(f"/account/{account_id}").json()["is_started"]
    with pytest.raises(ValueError, match="不支持交易执行"):
        create_executor_instance(asyncio.run(get_account_snapshot(account_id)))
    monkeypatch.setattr(intents, "submit_intent", AsyncMock(side_effect=AssertionError("信号不能入交易队列")))
    scheduled = "2026-11-23T15:30:00+08:00"
    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, scheduled))
    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat(scheduled)))

    target = client.get(f"/account/{account_id}/target_snapshot").json()
    assert target["target_mode"] == "quantity"
    assert target["raw_targets"] == target["quantities"] == TARGET
    assert client.get("/account/dashboard").status_code == 200
    with closing(sqlite3.connect(native_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_intent").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM target_weight_snapshot").fetchone() == (1,)
        (context,) = connection.execute(
            "SELECT context FROM notification_event WHERE event_type='signal.calculated'"
        ).fetchone()
    import json

    context = json.loads(context)
    assert context["positions"] is None
    assert context["targets"]["current"] == TARGET

    sender = Mock()
    monkeypatch.setattr(feishu, "push_feishu_card", sender)
    monkeypatch.setenv("AXILE_ACCOUNT_FEISHU_KEY", "offline")
    namespace = {}
    exec(SIGNAL_NOTIFICATION_CODE, namespace)  # noqa: S102 - 检查内置卡片实际发送数据
    namespace["notify"](context)
    assert all(symbol in sender.call_args.args[0]["elements"][0]["content"] for symbol in TARGET)


def test_signal_preview_rejects_scalar_quote_payload(client):
    portfolio = create_portfolio(client, "A股")
    quote_code = """
def fetch_quotes(symbols):
    return {symbol: {"last_price": 10, "update_time": "2026-11-23T15:00:00+08:00"} for symbol in symbols}
"""
    preview = client.post(
        "/account/signal/preview",
        json={"portfolio_id": portfolio, "account_config": {"manual_assets": MANUAL, "quote_code": quote_code}},
    )
    assert preview.status_code == 422, preview.text
    assert "行情函数必须返回 UnifiedPriceData：510300.SH" in str(preview.json()["detail"])


def test_signal_rebinding_and_account_function_refresh(client):
    account = create_signal_account(client, create_portfolio(client, "A股"))
    account_id = account["id"]
    futures = create_portfolio(client, "期货")
    positions = [
        {"symbol": "600000.SH", "volume": 200, "available_volume": 120, "direction": "多头", "market_value": None}
    ]
    manual = client.patch(
        f"/account/{account_id}", json={"account_config": {"manual_assets": {**MANUAL, "positions": positions}}}
    )
    assert manual.status_code == 200, manual.text
    observed = client.post(f"/account/{account_id}/assets/refresh")
    assert observed.status_code == 200, observed.text
    assert observed.json()["assets"]["positions"][0]["available_volume"] == 120
    code = "def fetch_account(context):\n    assert context['market'] == '期货'\n    return {'total_asset': 10000, 'available_cash': 9980, 'currency': 'CNY', 'positions': []}"
    response = client.patch(
        f"/account/{account_id}",
        json={
            "portfolio_id": futures,
            "account_config": {"account_code": code, "manual_assets": None},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["market"] == "期货"
    refreshed = client.post(f"/account/{account_id}/assets/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["assets"]["available_cash"] == 9980
    assert refreshed.json()["assets"]["extra"]["positions_known"] is True


def test_signal_account_allows_unbound_portfolio(client):
    account = create_signal_account(client, None)
    account_id = account["id"]
    assert account["portfolio_id"] is None
    assert account["market"] == ""

    refreshed = client.post(f"/account/{account_id}/assets/refresh")
    assert refreshed.status_code == 200, refreshed.text

    portfolio = create_portfolio(client, "期货")
    bound = client.patch(f"/account/{account_id}", json={"portfolio_id": portfolio})
    assert bound.status_code == 200, bound.text
    assert bound.json()["portfolio_id"] == portfolio
    assert bound.json()["market"] == "期货"

    unbound = client.patch(f"/account/{account_id}", json={"portfolio_id": None})
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["portfolio_id"] is None
    assert unbound.json()["market"] == ""
