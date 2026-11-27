"""用离线账户和行情快照验证调仓信号完整路径。"""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.channels import ChannelPortfolioPreset, ChannelSchedule, get_channel, register_channel, registry
from axile.channels.market_data import MarketDataUnavailable
from axile.channels.rebalance_signal import SIGNAL_CHANNEL
from axile.common import feishu
from axile.common.default_account_notification import SIGNAL_NOTIFICATION_CODE, build_signal_feishu_card
from axile.executor import gm_market_data
from axile.executor.gm_market_data import GMMarketData, GMMarketDataConfig
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server import market_data, signal_runner, signal_service, tushare_quotes
from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account_feishu, account_signal
from axile.server.api.routes.account import router as account_router
from axile.server.api.routes.portfolio import router as portfolio_router
from axile.server.core import db
from axile.server.execution import intents, notification_outbox, scheduler
from axile.server.execution.factory import create_executor_instance
from axile.server.repositories import get_account_snapshot
from axile.server.signal_service import run_scheduled_signal
from tests.sqlite_oracle import connect

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
CUSTOM_SOURCE = {"kind": "custom", "code": QUOTES}


@pytest.mark.parametrize(
    ("code", "timeout", "error", "message"),
    [
        ("def fetch_quotes(symbols):\n    while True:\n        pass\n", 0.2, TimeoutError, "计算超时"),
        ("import os\ndef fetch_quotes(symbols):\n    os._exit(7)\n", 5, ValueError, "进程异常退出"),
    ],
)
def test_signal_quote_process_preserves_timeout_and_exit_errors(code, timeout, error, message):
    with pytest.raises(error, match=message):
        signal_runner.run_signal_quote_test(code, ["600000.SH"], timeout)


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


@pytest.mark.parametrize(
    "failure", [None, MarketDataUnavailable("掘金暂无行情：600000.SH"), TimeoutError("掘金行情查询超时")]
)
def test_gm_quote_draft_keeps_partial_results_and_closes_market(client, monkeypatch, failure):
    quote = UnifiedPriceData(
        symbol="600000.SH",
        last_price=10.25,
        bids=[(10.24, 100)],
        asks=[(10.26, 200)],
        volume=100,
        timestamp=1795417200000,
        update_time="2026-11-23T15:00:00+08:00",
    )
    market = Mock()
    market.snapshot.return_value = {"600000.SH": quote}
    market.snapshot.side_effect = failure
    opened = Mock(return_value=market)
    monkeypatch.setattr(GMMarketDataConfig, "open", opened)
    response = client.post(
        "/account/signal/quote-test",
        json={
            "symbols": [" 600000.sh ", "000001.SZ", "invalid"],
            "quote_source": {
                "kind": "gm",
                "connection_mode": "service",
                "serv_addr": "127.0.0.1:7001",
                "token": "fixture-token",
            },
        },
    )
    opened.assert_called_once_with()
    market.snapshot.assert_called_once_with(["600000.SH", "000001.SZ"], timeout=5, allow_partial=True)
    market.close.assert_called_once_with()
    assert "fixture-token" not in response.text
    assert response.status_code == 200, response.text
    result = response.json()
    result_items = result["items"]
    assert [item["symbol"] for item in result_items] == ["600000.SH", "000001.SZ", "INVALID"]
    assert "代码格式或交易所不受支持" in result_items[2]["error"]
    if failure is None:
        assert result_items[0]["quote"] == {
            "last_price": 10.25,
            "timestamp": 1795417200000,
            "bids": [[10.24, 100]],
            "asks": [[10.26, 200]],
        }
        assert result_items[1] == {"symbol": "000001.SZ", "quote": None, "error": "掘金未返回该标的行情"}
    else:
        assert all(item["quote"] is None and item["error"] == str(failure) for item in result_items[:2])


def test_market_data_requires_all_quotes_unless_test_requests_partial(monkeypatch):
    quote = Mock(spec=UnifiedPriceData)
    monkeypatch.setattr(GMMarketData, "_read", lambda *_args: {"600000.SH": quote})
    with closing(GMMarketDataConfig(token="fixture-token", address=None).open()) as market:
        with pytest.raises(MarketDataUnavailable, match="000001.SZ"):
            market.snapshot(["600000.SH", "000001.SZ"], timeout=5)
        assert market.snapshot(["600000.SH", "000001.SZ"], timeout=5, allow_partial=True) == {"600000.SH": quote}


@pytest.mark.parametrize("positions", [None, []])
def test_signal_account_test_uses_draft_context_and_preserves_holding_state(client, positions):
    code = f"""
def fetch_account(context):
    assert context == {{"account_id": None, "name": "草稿账户", "market": "A股"}}
    return {{"total_asset": 100000, "available_cash": 80000, "currency": "CNY", "positions": {positions!r}}}
"""
    response = client.post("/account/signal/account-test", json={"code": code, "name": "草稿账户", "market": "A股"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total_asset": 100000.0,
        "available_cash": 80000.0,
        "currency": "CNY",
        "positions": positions,
    }


def test_signal_account_test_rejects_incomplete_snapshot(client):
    response = client.post("/account/signal/account-test", json={"code": "def fetch_account(context): return {}"})
    assert response.status_code == 422
    assert "total_asset" in response.text


def test_gm_quote_test_reuses_listening_terminal(client, monkeypatch):
    directory = r"C:\Program Files\GoldMiner3"
    port = Mock(return_value=7615)
    launch = Mock()
    worker = Mock()
    worker.Pipe.return_value = (Mock(), Mock())
    worker.Pipe.return_value[0].recv.return_value = {}
    monkeypatch.setattr(gm_market_data, "gm_rpc_port", port)
    monkeypatch.setattr(gm_market_data, "wait_for_gm_port", Mock(return_value=True))
    monkeypatch.setattr(gm_market_data, "launch_gm_terminal", launch)
    monkeypatch.setattr(gm_market_data, "get_context", Mock(return_value=worker))
    response = client.post(
        "/account/signal/quote-test",
        json={
            "symbols": ["600000.SH"],
            "quote_source": {"kind": "gm", "token": "gm-fixture", "terminal_path": directory},
        },
    )
    assert response.status_code == 200, response.text
    port.assert_called_once_with(Path(directory))
    launch.assert_not_called()
    assert worker.Process.call_args.kwargs["args"][1].address == "127.0.0.1:7615"


def test_tushare_quote_test_keeps_partial_results(client, monkeypatch):
    query = Mock(
        return_value=[
            {"ts_code": "600000.SH", "close": 10, "vol": 100, "amount": 1000, "trade_time": "2026-11-23 15:00:00"}
        ]
    )
    monkeypatch.setattr(tushare_quotes, "_query", query)
    response = client.post(
        "/account/signal/quote-test",
        json={"symbols": ["600000.sh", "000001.SZ"], "quote_source": {"kind": "tushare", "token": "ts-fixture"}},
    )
    assert response.status_code == 200, response.text
    first, missing = response.json()["items"]
    assert first["symbol"] == "600000.SH" and first["quote"]["last_price"] == 10
    assert missing == {"symbol": "000001.SZ", "quote": None, "error": "Tushare 未返回该标的行情"}


def test_custom_quote_test_runs_draft_and_preserves_symbol_case(client):
    quote_code = QUOTES.replace("for symbol in symbols", "for symbol in symbols[:1]")
    response = client.post(
        "/account/signal/quote-test",
        json={"symbols": ["custom.symbol", "missing"], "quote_source": {"kind": "custom", "code": quote_code}},
    )
    assert response.status_code == 200, response.text
    first, missing = response.json()["items"]
    assert first["symbol"] == "custom.symbol" and first["quote"]["last_price"] == 10
    assert missing == {"symbol": "missing", "quote": None, "error": "行情函数未返回该标的行情"}


def create_portfolio(client, market, code=CALCULATE):
    response = client.post("/portfolio/", json={"market": market, "name": market, "custom_calc_py_code": code})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def signal_account_payload(portfolio_id, quote_source=CUSTOM_SOURCE, *, market="A股"):
    return {
        "name": "离线信号",
        "market": market,
        "trade_channel": SIGNAL_CHANNEL,
        "brokerage": SIGNAL_CHANNEL,
        "portfolio_id": portfolio_id,
        "account_config": {"manual_assets": MANUAL, "quote_source": quote_source, "follow_target": False},
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
    }


def create_signal_account(client, portfolio_id, quote_source=CUSTOM_SOURCE, *, market="A股"):
    response = client.post("/account/", json=signal_account_payload(portfolio_id, quote_source, market=market))
    assert response.status_code == 201, response.text
    return response.json()


def test_signal_calculation_uses_saved_leverage_and_trial_overrides(client):
    code = """
def calculate_portfolio(context):
    return {"mode": "quantity", "targets": {
        "600000.SH": 100 * context.settings.long_leverage,
        "510300.SH": -100 * context.settings.short_leverage,
    }}
"""
    portfolio_id = create_portfolio(client, "A股", code)
    payload = signal_account_payload(portfolio_id)
    response = client.post("/account/", json={**payload, "long_leverage": 2, "short_leverage": 0})
    assert response.status_code == 201, response.text
    account_id = response.json()["id"]
    url = f"/account/{account_id}"

    response = client.post(f"{url}/target_snapshot/refresh")
    assert response.status_code == 200, response.text
    assert response.json()["quantities"] == {"600000.SH": 200, "510300.SH": 0}

    response = client.post(
        "/portfolio/validate_custom_calc",
        json={"account_id": account_id, "custom_calc_py_code": code, "leverage_overrides": {"short_leverage": 2.5}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["target"] == {"600000.SH": 200, "510300.SH": -250}
    assert client.get(url).json()["short_leverage"] == 0

    response = client.patch(url, json={"short_leverage": 1.5})
    assert response.status_code == 200, response.text
    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T10:30:00+08:00")))
    assert client.get(f"{url}/target_snapshot").json()["quantities"] == {"600000.SH": 200, "510300.SH": -150}


def test_signal_card_distinguishes_empty_missing_and_failed_targets():
    for targets, error, message in [
        ({}, None, "目标空仓"),
        (None, None, "尚未计算目标持仓"),
        ({}, "行情读取失败", "行情读取失败"),
    ]:
        card = build_signal_feishu_card("示例", targets, "股", error)
        status = "调仓信号失败" if error else "调仓信号"
        assert card["header"]["title"]["content"] == "示例"
        assert card["header"]["subtitle"]["content"] == status
        assert card["body"]["elements"][0]["text"]["content"] == message

    card = build_signal_feishu_card("示例", {"TEST": -1234.56789}, "手", at="2026-11-27T01:30:00+00:00")
    assert card["header"]["subtitle"]["content"] == "调仓信号"
    assert card["body"]["elements"][-1]["content"] == "2026-11-27 09:30"
    assert signal_card_targets(card) == {"TEST": "-1,234.56789 手"}


def signal_card_targets(card):
    columns = next(element["columns"] for element in card["body"]["elements"] if element["tag"] == "column_set")
    symbols = columns[0]["elements"][0]["text"]["content"].splitlines()
    quantities = columns[1]["elements"][0]["content"].splitlines()
    return dict(zip(symbols, quantities, strict=True))


def test_signal_notification_tests_use_sample_without_saved_account(client, monkeypatch):
    sender = Mock()
    monkeypatch.setattr(account_signal, "push_feishu_card", sender)
    url = "/account/signal/notification-test"
    sample = {"name": "草稿账户", "market": "A股"}
    webhook = {**sample, "mode": "feishu", "draft": "draft-key"}
    response = client.post(url, json=webhook)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    card, key = sender.call_args.args
    assert key == "draft-key"
    assert card["config"]["width_mode"] == "compact"
    assert card["header"]["title"]["content"] == "【测试】草稿账户"
    section, positions, _timestamp = card["body"]["elements"]
    assert section["content"] == "**目标持仓**"
    assert positions["columns"][1]["elements"][0]["text_align"] == "right"
    assert list(signal_card_targets(card).values()) == ["100 股", "100 股"]

    sender.side_effect = RuntimeError("机器人拒绝 draft-key")
    response = client.post(url, json=webhook)
    assert response.json() == {"ok": False, "message": "机器人拒绝 [已隐藏]"}

    code = """
def notify(context):
    assert context["is_test"] and context["execution"]["is_test"]
    assert context["event_type"] == "signal.calculated"
    assert context["account"] == {"id": None, "name": "草稿账户", "market": "A股"}
    assert context["targets"]["mode"] == "quantity"
    assert 100 in context["targets"]["current"].values()
    assert context["quantity_unit"] == "股"
"""
    response = client.post(url, json={**sample, "mode": "function", "draft": code})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True, response.text

    response = client.post(
        url, json={**sample, "mode": "function", "draft": 'def notify(context):\n    raise RuntimeError("试跑失败")'}
    )
    assert response.json()["ok"] is False
    assert "试跑失败" in response.json()["message"]
    assert response.json()["error_line"] == 2


def test_signal_schedule_persists_complete_targets_once_and_rejects_trade(client, native_database, monkeypatch):
    portfolio = create_portfolio(client, "A股")
    account = create_signal_account(client, portfolio)
    account_id = account["id"]
    assert account["market"] == "A股"
    flows = client.get(f"/account/{account_id}/cash-flows").json()
    assert flows["data"][0]["amount"] == 10000
    assert flows["currency"] == "CNY"
    assert client.get("/account/dashboard").status_code == 200

    assert client.post(f"/account/execute/{account_id}").status_code == 409
    assert client.post(f"/account/empty_positions/{account_id}").status_code == 409
    assert client.post(f"/account/{account_id}/connection/reconnect").status_code == 409
    assert client.get(f"/account/{account_id}").json()["is_started"]
    with pytest.raises(ValueError, match="不支持交易执行"):
        create_executor_instance(asyncio.run(get_account_snapshot(account_id)))
    monkeypatch.setattr(intents, "submit_intent", AsyncMock(side_effect=AssertionError("信号不能入交易队列")))
    closed = "2026-11-23T15:30:00+08:00"
    opened = "2026-11-23T10:30:00+08:00"
    preview = client.post(
        "/account/schedule-preview",
        json={"trade_channel": "gm", "cron_expr": "30 10,15 * * *", "scheduled_ats": [closed, opened]},
    )
    assert preview.status_code == 200, preview.text
    assert [item["action"] for item in preview.json()["items"]] == ["execute", "skip"]
    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, closed))
    with closing(connect(native_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM target_weight_snapshot").fetchone() == (0,)
    scheduled = opened
    calculate = AsyncMock(wraps=signal_service.calculate_signal)
    monkeypatch.setattr(signal_service, "calculate_signal", calculate)
    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, scheduled))
    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat(scheduled)))
    assert calculate.await_count == 1

    target = client.get(f"/account/{account_id}/target_snapshot").json()
    assert target["target_mode"] == "quantity"
    assert target["raw_targets"] == target["quantities"] == TARGET
    assert client.get("/account/dashboard").status_code == 200
    with closing(connect(native_database)) as connection:
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
    card = sender.call_args.args[0]
    assert card["header"]["title"]["content"] == "离线信号"
    assert card["header"]["subtitle"]["content"].endswith(" · 定时生成")
    assert signal_card_targets(card) == {symbol: f"{quantity} 股" for symbol, quantity in TARGET.items()}


def test_manual_signal_publishes_for_paused_account_without_trade_intent(client, native_database, monkeypatch):
    import json

    wake = Mock()
    monkeypatch.setattr("axile.server.signal_service.wake_notifications", wake)
    portfolio = create_portfolio(client, "A股")
    account_id = create_signal_account(client, portfolio)["id"]
    url = f"/account/{account_id}"
    paused = client.patch(url, json={"is_started": False})
    assert paused.status_code == 200, paused.text
    assert not paused.json()["is_started"]

    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T10:30:00+08:00")))
    wake.assert_not_called()

    response = client.post(f"{url}/signal/run")
    assert response.status_code == 200, response.text
    wake.assert_called_once_with(account_id)
    assert client.get(f"{url}/target_snapshot").json()["quantities"] == TARGET
    assert client.get(f"{url}/asset_snapshots").json()["data"][0]["assets"]["total_asset"] == MANUAL["total_asset"]
    with closing(connect(native_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_intent").fetchone() == (0,)
        (context,) = connection.execute(
            "SELECT context FROM notification_event WHERE id=?", (response.json()["event_id"],)
        ).fetchone()
    context = json.loads(context)
    assert context["execution"]["trigger_source"] == "manual"
    assert context["scheduled_at"] is None
    assert context["targets"]["target_volume"] == TARGET

    copy = client.post(f"{url}/copy", json={"name": "备份信号账户"})
    assert copy.status_code == 201, copy.text
    assert not copy.json()["is_started"]
    assert client.post(f"{url}/archive").status_code == 200
    assert client.post(f"{url}/signal/run").status_code == 409
    assert client.delete(f"/account/{copy.json()['id']}").status_code == 200


def test_signal_records_page_filters_saved_signals(client, monkeypatch):
    monkeypatch.setattr("axile.server.signal_service.wake_notifications", Mock())
    portfolio = create_portfolio(client, "A股")
    account_id = create_signal_account(client, portfolio)["id"]
    url = f"/account/{account_id}"
    history = f"{url}/signal/records"
    assert client.get(history).json() == {"items": [], "next_cursor": None}

    refreshed = client.post(f"{url}/target_snapshot/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert client.get(history).json()["items"] == []
    manual = client.post(f"{url}/signal/run")
    assert manual.status_code == 200, manual.text
    monkeypatch.setattr("axile.server.signal_service.calculate_signal", AsyncMock(side_effect=ValueError("计算失败")))
    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T10:30:00+08:00")))

    latest = client.get(history, params={"limit": 1}).json()
    (failed,) = latest["items"]
    assert failed["trigger_source"] == "scheduler"
    assert failed["status"] == "FAILED" and failed["error"] and failed["targets"] == {}
    assert failed["scheduled_at"] == "2026-11-23T02:30:00+00:00"
    assert failed["sizing_prices"] is None and failed["settlement_prices"] is None
    older = client.get(history, params={"before": latest["next_cursor"], "limit": 1}).json()
    (succeeded,) = older["items"]
    assert succeeded["id"] == manual.json()["event_id"]
    assert succeeded["trigger_source"] == "manual" and succeeded["status"] == "SUCCEEDED"
    assert succeeded["targets"] == TARGET and succeeded["notification_status"] == "pending"
    assert older["next_cursor"] is None
    assert client.get(history, params={"status": "SUCCEEDED", "days": 7, "symbol": "300.sh"}).json() == older
    assert client.get(history, params={"symbol": "%"}).json()["items"] == []
    assert client.get(history, params={"limit": 101}).status_code == 422
    other = client.post("/account/", json=signal_account_payload(portfolio) | {"name": "另一信号账户"})
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    assert client.get(f"/account/{other_id}/signal/records").json()["items"] == []
    assert client.get(f"/account/{other_id + 999}/signal/records").status_code == 404


def test_manual_signal_failure_is_recorded_without_sending_notification(client, monkeypatch):
    portfolio = create_portfolio(client, "A股")
    account_id = create_signal_account(client, portfolio)["id"]
    url = f"/account/{account_id}"
    updated = client.patch(url, json={"execution_notification_code": "def notify(context): pass"})
    assert updated.status_code == 200, updated.text
    monkeypatch.setattr(signal_service, "calculate_signal", AsyncMock(side_effect=ValueError("模拟行情错误")))
    monkeypatch.setattr(signal_service, "wake_notifications", Mock())
    send = Mock()
    monkeypatch.setattr(notification_outbox, "run_account_notification", send)

    response = client.post(f"{url}/signal/run")
    assert response.status_code == 422 and "模拟行情错误" in response.text
    asyncio.run(notification_outbox._dispatch(account_id))
    send.assert_not_called()
    (record,) = client.get(f"{url}/signal/records", params={"status": "FAILED"}).json()["items"]
    assert record["trigger_source"] == "manual" and record["error"]
    assert record["notification_status"] == "succeeded"
    assert client.get(f"{url}/target_snapshot").json()["calculated_at"] is None


def test_weight_signal_trial_and_snapshots_keep_weights_and_publish_quantities(client, native_database, monkeypatch):
    weights = {"600000.SH": 0.48, "510300.SH": -0.23}
    code = f"""
def calculate_portfolio(context):
    assert context.get_price("600000.SH") == 10
    assert context.settings.weight_precision == 0.1
    return {weights!r}
"""
    quotes = QUOTES.replace(
        "def fetch_quotes(symbols):",
        "seen = set()\n\ndef fetch_quotes(symbols):\n    assert not seen.intersection(symbols)\n    seen.update(symbols)",
    )
    portfolio = create_portfolio(client, "A股", code)
    account_id = create_signal_account(client, portfolio, {"kind": "custom", "code": quotes})["id"]
    account_url = f"/account/{account_id}"
    updated = client.patch(account_url, json={"long_leverage": 1.2, "short_leverage": 2, "weight_precision": 0.1})
    assert updated.status_code == 200, updated.text
    trial = client.post("/portfolio/validate_custom_calc", json={"account_id": account_id, "custom_calc_py_code": code})
    assert trial.status_code == 200, trial.text
    assert trial.json()["valid"] and trial.json()["target_mode"] == "weight"
    assert trial.json()["target"] == weights

    refreshed = client.post(f"{account_url}/target_snapshot/refresh")
    assert refreshed.status_code == 200, refreshed.text
    quantities = {"600000.SH": 600, "510300.SH": -500}
    expected = {
        "target_mode": "weight",
        "raw_targets": weights,
        "account_weights": {"600000.SH": 0.6, "510300.SH": -0.5},
        "quantities": quantities,
        "sizing_prices": {"600000.SH": 10, "510300.SH": 10},
    }
    assert refreshed.json() | expected == refreshed.json()

    portfolio_refresh = client.post(f"/portfolio/{portfolio}/target_snapshot/refresh")
    assert portfolio_refresh.status_code == 200, portfolio_refresh.text
    assert portfolio_refresh.json()["target_mode"] == "weight"
    assert portfolio_refresh.json()["weights"] == weights
    assert portfolio_refresh.json()["sizing_prices"] == expected["sizing_prices"]
    assert client.get(f"/portfolio/{portfolio}/target_snapshot").json()["sizing_prices"] == expected["sizing_prices"]
    assert client.get(f"{account_url}/target_snapshot").json()["quantities"] == quantities

    sender = Mock()
    monkeypatch.setattr(account_feishu, "push_feishu_card", sender)
    tested = client.post(f"{account_url}/feishu/test", json={"feishu_key": "fixture-key"})
    assert tested.status_code == 200 and tested.json()["ok"], tested.text
    (card, _key) = sender.call_args.args
    assert card["header"]["title"]["content"] == "【测试】离线信号"
    assert card["header"]["subtitle"]["content"] == "调仓信号 · A股"
    assert signal_card_targets(card) == {symbol: f"{quantity:g} 股" for symbol, quantity in quantities.items()}

    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T10:30:00+08:00")))
    target = client.get(f"{account_url}/target_snapshot").json()
    assert target | expected == target
    assert target["source"] == "signal"
    (record,) = client.get(f"{account_url}/signal/records").json()["items"]
    assert record["sizing_prices"] == expected["sizing_prices"]
    with closing(connect(native_database)) as connection:
        (notified,) = connection.execute(
            "SELECT json_extract(context,'$.targets') FROM notification_event WHERE event_type='signal.calculated'"
        ).fetchone()

    import json

    assert json.loads(notified) == {"mode": "quantity", "current": quantities, "target_volume": quantities}


@pytest.mark.parametrize("has_multiplier", [True, False])
def test_futures_weight_signal_requires_contract_multiplier_on_publication(client, has_multiplier):
    code = 'def calculate_portfolio(context): return {"mode": "weight", "targets": {"rb2610": -0.28}}'
    quote_code = (
        QUOTES.replace("last_price=10,", 'last_price=10, extra={"volume_multiple": 10},') if has_multiplier else QUOTES
    )
    portfolio = create_portfolio(client, "期货", code)
    account = create_signal_account(client, portfolio, {"kind": "custom", "code": quote_code}, market="期货")
    response = client.post(f"/account/{account['id']}/target_snapshot/refresh")
    if has_multiplier:
        assert response.status_code == 200, response.text
        assert response.json()["quantities"] == {"rb2610": -28.0}
    else:
        assert response.status_code == 400
        assert "缺少有效合约乘数" in response.text


def test_ctp_portfolio_trial_reports_missing_catalog_multiplier(client, monkeypatch):
    pytest.importorskip("openctp_ctp")
    from axile.server.execution.worker_backend import manager

    symbol = "rb2701"
    quote = UnifiedPriceData(
        symbol=symbol,
        last_price=10,
        bids=[],
        asks=[],
        volume=1,
        timestamp=1795417200000,
        update_time="2026-11-23T15:00:00+08:00",
        extra={"exchange_id": "SHFE"},
    )
    monkeypatch.setattr(signal_runner, "read_configured_market_data", Mock(return_value={symbol: quote}))
    catalog = Mock()
    catalog.cached.return_value = None
    code = f"def calculate_portfolio(context): return {{{symbol!r}: 0.2}}"
    portfolio = create_portfolio(client, "期货", code)
    source = {"kind": "ctp", "md_front": "tcp://quotes.example.test:15000"}
    account_id = create_signal_account(client, portfolio, source, market="期货")["id"]
    monkeypatch.setattr(manager, "get_worker_backend_manager", Mock(return_value=Mock(instrument_catalog=catalog)))
    payload = {"account_id": account_id, "custom_calc_py_code": code}
    trial = client.post("/portfolio/validate_custom_calc", json=payload)
    assert trial.status_code == 200, trial.text
    assert not trial.json()["valid"]
    assert "刷新共享合约目录" in trial.json()["error"]

    catalog.cached.return_value = Mock(has_specs=True, multiplier=10)
    trial = client.post("/portfolio/validate_custom_calc", json=payload)
    assert trial.status_code == 200, trial.text
    assert trial.json()["valid"] and trial.json()["target"] == {symbol: 0.2}


def test_stock_signal_auction_options_preview_persist_and_schedule(client, monkeypatch):
    now = datetime.fromisoformat("2026-11-23T09:00:00+08:00")
    monkeypatch.setattr(scheduler, "clock_now", lambda **_kwargs: now)
    cron = "30 9 * * *"
    options = {"gm": {"opening_locked": {"enabled": True, "time": "09:22"}}}
    preview = client.post(
        "/account/schedule-preview",
        json={
            "trade_channel": "gm",
            "cron_expr": cron,
            "channel_options": options,
            "after": now.isoformat(),
            "limit": 2,
        },
    )
    assert preview.status_code == 200, preview.text
    auction = "2026-11-23T09:22:00+08:00"
    regular = "2026-11-23T09:30:00+08:00"
    assert [(row["scheduled_at"], row["action"]) for row in preview.json()["items"]] == [
        (auction, "execute"),
        (regular, "execute"),
    ]

    portfolio = create_portfolio(client, "A股")
    payload = signal_account_payload(portfolio) | {"cron_expr": cron, "channel_options": options}
    created = client.post("/account/", json=payload)
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    account_url = f"/account/{account_id}"
    assert client.get(account_url).json()["channel_options"]["gm"]["opening_locked"] == options["gm"]["opening_locked"]
    assert client.get(f"{account_url}/next_run_time").json()["next_execution_times"][:2] == [auction, regular]

    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, auction))
    assert client.get(f"{account_url}/target_snapshot").json()["quantities"] == TARGET

    disabled = {"gm": {"opening_locked": {"enabled": False, "time": "09:22"}}}
    updated = client.patch(account_url, json={"channel_options": disabled})
    assert updated.status_code == 200, updated.text
    assert client.get(f"{account_url}/next_run_time").json()["next_execution_times"][0] == regular
    run_signal = AsyncMock()
    monkeypatch.setattr("axile.server.signal_service.run_scheduled_signal", run_signal)
    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, auction))
    run_signal.assert_not_awaited()

    futures = signal_account_payload(None, market="期货")
    assert client.post("/account/", json=futures | {"channel_options": options}).status_code == 422
    future_account_id = create_signal_account(client, None, market="期货")["id"]
    assert client.patch(f"/account/{future_account_id}", json={"channel_options": options}).status_code == 422


def test_signal_portfolio_trial_rejects_scalar_quote_payload(client):
    portfolio = create_portfolio(client, "A股")
    quote_code = """
def fetch_quotes(symbols):
    return {symbol: {"last_price": 10, "update_time": "2026-11-23T15:00:00+08:00"} for symbol in symbols}
"""
    account_id = create_signal_account(client, portfolio, {"kind": "custom", "code": quote_code})["id"]
    trial = client.post(
        "/portfolio/validate_custom_calc",
        json={"account_id": account_id, "custom_calc_py_code": CALCULATE},
    )
    assert trial.status_code == 200, trial.text
    assert trial.json()["valid"] is False
    assert "行情函数必须返回 UnifiedPriceData：510300.SH" in trial.json()["error"]


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "gm", "token": "gm-fixture", "connection_mode": "service", "serv_addr": "127.0.0.1:7001"},
        {"kind": "tushare", "token": "ts-fixture", "url": "https://quotes.example.test/v1/tushare"},
    ],
)
def test_signal_presets_read_quotes_in_portfolio_trial_and_schedule(client, monkeypatch, source):
    quote = UnifiedPriceData(
        symbol="510300.SH",
        last_price=10,
        bids=[(9.9, 200), (9.8, 400)],
        asks=[(10.2, 300), (10.3, 500)],
        volume=125,
        timestamp=1795417200000,
        update_time="2026-11-23T15:00:00+08:00",
    )
    gm = Mock(return_value={quote.symbol: quote})
    tushare = Mock(return_value={quote.symbol: quote})
    monkeypatch.setattr(signal_runner, "read_configured_market_data", gm)
    monkeypatch.setattr(signal_runner, "read_tushare_quotes", tushare)
    portfolio = create_portfolio(client, "A股")
    account_id = create_signal_account(client, portfolio, source)["id"]
    saved = client.get(f"/account/{account_id}/signal-config").json()["account_config"]["quote_source"]
    assert all(saved[key] == value for key, value in source.items())
    trial = client.post(
        "/portfolio/validate_custom_calc",
        json={"account_id": account_id, "custom_calc_py_code": CALCULATE},
    )
    assert trial.status_code == 200, trial.text
    assert trial.json()["target"] == TARGET

    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T15:30:00+08:00")))
    assert client.get(f"/account/{account_id}/target_snapshot").json()["quantities"] == TARGET
    if source["kind"] == "gm":
        assert gm.call_count == 2
        assert gm.call_args.args == (
            ("gm", GMMarketDataConfig(source["token"], source["serv_addr"])),
            [quote.symbol],
        )
        tushare.assert_not_called()
    else:
        assert tushare.call_count == 2
        assert tushare.call_args.args[:3] == (source["url"], source["token"], [quote.symbol])
        gm.assert_not_called()


@pytest.mark.parametrize(
    ("source", "symbol"),
    [
        ({"kind": "ctp", "md_front": "tcp://quotes.example.test:15000"}, "rb2701"),
        ({"kind": "tq", "tq_username": "fixture-user", "tq_password": "fixture-password"}, "SHFE.rb2701"),
    ],
)
def test_signal_futures_quotes_work_in_quote_test_portfolio_trial_and_schedule(client, monkeypatch, source, symbol):
    if source["kind"] == "ctp":
        pytest.importorskip("openctp_ctp")
        from axile.executor.ctp.market_data import CtpMarketDataConfig

        config_type = CtpMarketDataConfig
        expected_config = CtpMarketDataConfig(source["md_front"], "", "", "")
    else:
        from axile.executor.tq.market_data import TQMarketDataConfig

        config_type = TQMarketDataConfig
        expected_config = TQMarketDataConfig(source["tq_username"], source["tq_password"])

    quote = UnifiedPriceData(
        symbol=symbol,
        last_price=10,
        bids=[(9.9, 200), (9.8, 400)],
        asks=[(10.2, 300), (10.3, 500)],
        volume=125,
        timestamp=1795417200000,
        update_time="2026-11-23T15:00:00+08:00",
    )
    draft_feed, saved_feed = (Mock(snapshot=Mock(return_value={symbol: quote})) for _ in range(2))
    opened = Mock(side_effect=[draft_feed, saved_feed])
    monkeypatch.setattr(config_type, "open", lambda config: opened(config))
    monkeypatch.setattr(market_data, "_sources", {})

    response = client.post("/account/signal/quote-test", json={"symbols": [f" {symbol} "], "quote_source": source})
    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["symbol"] == symbol
    assert row["quote"]["last_price"] == 10
    assert row["quote"]["bids"] == [[9.9, 200], [9.8, 400]]
    assert row["error"] is None
    draft_feed.snapshot.assert_called_once_with([symbol], timeout=5)
    draft_feed.close.assert_called_once_with()

    code = CALCULATE.replace(str(TARGET), repr({symbol: 2})).replace("510300.SH", symbol)
    portfolio = create_portfolio(client, "期货", code)
    account_id = create_signal_account(client, portfolio, source, market="期货")["id"]
    saved = client.get(f"/account/{account_id}/signal-config").json()["account_config"]["quote_source"]
    assert {key: saved[key] for key in source} == source
    trial = client.post(
        "/portfolio/validate_custom_calc",
        json={"account_id": account_id, "custom_calc_py_code": code},
    )
    assert trial.status_code == 200, trial.text
    assert trial.json()["target"] == {symbol: 2}
    asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat("2026-11-23T15:30:00+08:00")))
    assert client.get(f"/account/{account_id}/target_snapshot").json()["quantities"] == {symbol: 2}
    assert [call.args for call in opened.call_args_list] == [(expected_config,), (expected_config,)]
    assert [call.args for call in saved_feed.snapshot.call_args_list] == [([symbol],), ([symbol],)]
    market_data.close_market_data()
    saved_feed.close.assert_called_once_with()


def test_signal_rebinding_and_account_function_refresh(client):
    account = create_signal_account(client, create_portfolio(client, "A股"))
    account_id = account["id"]
    rebinding = create_portfolio(client, "A股")
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
    code = "def fetch_account(context):\n    assert context['market'] == 'A股'\n    return {'total_asset': 10000, 'available_cash': 9980, 'currency': 'CNY', 'positions': []}"
    response = client.patch(
        f"/account/{account_id}",
        json={
            "portfolio_id": rebinding,
            "account_config": {
                "account_code": code,
                "manual_assets": None,
                "quote_source": {"kind": "tushare", "token": "ts-fixture"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["market"] == "A股"
    assert response.json()["portfolio_id"] == rebinding
    source = client.get(f"/account/{account_id}/signal-config").json()["account_config"]["quote_source"]
    assert (source["kind"], source["url"], source["code"]) == ("tushare", "https://api.tushare.pro", "")
    refreshed = client.post(f"/account/{account_id}/assets/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["assets"]["available_cash"] == 9980
    assert refreshed.json()["assets"]["extra"]["positions_known"] is True


@pytest.mark.parametrize("market", ["A股", "期货"])
def test_signal_account_allows_unbound_portfolio(client, market):
    account = create_signal_account(client, None, market=market)
    account_id = account["id"]
    assert account["portfolio_id"] is None
    assert account["market"] == market

    refreshed = client.post(f"/account/{account_id}/assets/refresh")
    assert refreshed.status_code == 200, refreshed.text

    portfolio = create_portfolio(client, market)
    bound = client.patch(f"/account/{account_id}", json={"portfolio_id": portfolio})
    assert bound.status_code == 200, bound.text
    assert bound.json()["portfolio_id"] == portfolio
    assert bound.json()["market"] == market

    unbound = client.patch(f"/account/{account_id}", json={"portfolio_id": None})
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["portfolio_id"] is None
    assert unbound.json()["market"] == market


def test_signal_market_is_required_and_cannot_drift_from_binding(client):
    stock = create_portfolio(client, "A股")
    futures = create_portfolio(client, "期货")
    missing = signal_account_payload(None, market="")
    assert client.post("/account/", json=missing).status_code == 422
    assert client.post("/account/", json=signal_account_payload(stock, market="期货")).status_code == 422
    source = {"kind": "tushare", "token": "fixture"}
    assert client.post("/account/", json=signal_account_payload(None, source, market="期货")).status_code == 422

    account_id = create_signal_account(client, futures, market="期货")["id"]
    assert client.patch(f"/account/{account_id}", json={"market": "A股"}).status_code == 422
    assert client.patch(f"/account/{account_id}", json={"portfolio_id": stock}).status_code == 422
    assert client.patch(f"/portfolio/{futures}", json={"market": "A股"}).status_code == 422


def test_registered_plugin_market_drives_signal_schedule(client, monkeypatch):
    base = get_channel("tq")
    monkeypatch.setattr(registry, "_registry", registry._registry.copy())
    descriptor = replace(
        base.descriptor,
        channel="external-fixture",
        market="external-fixture",
        currency="USDT",
        schedule=ChannelSchedule(kind="continuous"),
        calendar=None,
        portfolio=ChannelPortfolioPreset(market_label="外部市场", example_symbols=("TESTUSDT",)),
    )
    register_channel(replace(base, descriptor=descriptor))
    portfolio = create_portfolio(client, "外部市场")
    account_id = create_signal_account(client, portfolio, market="外部市场")["id"]

    asyncio.run(scheduler.execute_scheduled_rebalance(account_id, "2026-11-22T03:00:00+08:00"))
    response = client.get(f"/account/{account_id}/target_snapshot")
    assert response.json()["quantities"] == TARGET


def test_manual_follow_updates_ledger_without_changing_scheduled_node_baseline(client, monkeypatch):
    monkeypatch.setattr(signal_service, "wake_notifications", Mock())
    code = f"""
def calculate_portfolio(context):
    targets = {{}} if context.get_price("510300.SH") >= 12 else {TARGET}
    return {{"mode": "quantity", "targets": targets}}
"""
    portfolio = create_portfolio(client, "A股", code)
    payload = signal_account_payload(portfolio) | {
        "cron_expr": "30 9,10 * * mon-fri|0 11 * * mon-fri",
        "is_started": False,
    }
    payload["account_config"]["manual_assets"] = MANUAL | {"available_cash": 10000}
    payload["account_config"]["follow_target"] = True
    created = client.post("/account/", json=payload)
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    url = f"/account/{account_id}"
    config = client.get(f"{url}/signal-config").json()["account_config"]
    assert config["follow_target"] is True
    assert config["follow_fee_bps"] == 5

    def run(clock, day="23"):
        asyncio.run(run_scheduled_signal(account_id, datetime.fromisoformat(f"2026-11-{day}T{clock}:00+08:00")))

    def latest():
        return client.get(f"{url}/signal/records", params={"limit": 1, "trigger_source": "scheduler"}).json()["items"][
            0
        ]

    def holdings():
        assets = client.get(f"{url}/asset_snapshots").json()["data"][0]["assets"]
        return assets["source"], {p["symbol"]: p["volume"] for p in assets["positions"]}

    assert client.post(f"{url}/target_snapshot/refresh").status_code == 200
    assert client.get(f"{url}/signal/records").json()["items"] == []
    manual = client.post(f"{url}/signal/run")
    assert manual.status_code == 200, manual.text
    assert manual.json()["message"] == "跟随已结转"
    assert holdings() == ("signal_follow", TARGET)
    assets = client.get(f"{url}/asset_snapshots").json()["data"][0]["assets"]
    assert (assets["available_cash"], assets["total_asset"]) == (1996, 9996)
    (record,) = client.get(f"{url}/signal/records").json()["items"]
    assert record["node_change"] is None
    assert record["adjustment"] == {
        "kind": "order",
        "basis": "signal_holdings",
        "status": "compared",
        "changes": {symbol: {"before": 0, "after": quantity, "delta": quantity} for symbol, quantity in TARGET.items()},
    }
    card = build_signal_feishu_card("示例", TARGET, "股", adjustment=record["adjustment"])
    assert "600000.SH  买入 300 股" in card["body"]["elements"][1]["text"]["content"]
    assert record["follow_ledger"] == {
        "status": "settled",
        "positions": TARGET,
        "cash": 1996,
        "equity": 9996,
        "fee": 4,
        "fee_bps": 5,
    }
    started = client.patch(url, json={"is_started": True})
    assert started.status_code == 200, started.text
    run("09:30")
    assert latest()["node_change"] == {"status": "first"}
    assert latest()["followed_target"] is True
    assert latest()["follow_ledger"]["fee"] == 0
    assert latest()["follow_ledger"]["cash"] == 1996
    run("09:30")
    assert latest()["follow_ledger"]["cash"] == 1996
    repriced = client.patch(
        url,
        json={
            "account_config": {
                "quote_source": CUSTOM_SOURCE
                | {
                    "code": QUOTES.replace("last_price=10,", "last_price=11,"),
                }
            }
        },
    )
    assert repriced.status_code == 200, repriced.text
    run("11:00")
    assert latest()["follow_ledger"] == {
        "status": "settled",
        "positions": TARGET,
        "cash": 1996,
        "equity": 10796,
        "fee": 0,
        "fee_bps": 5,
    }
    assert latest()["node_change"] == {"status": "missing"}
    run("09:30", "24")
    assert latest()["node_change"] == {
        "status": "compared",
        "previous_scheduled_at": "2026-11-23T03:00:00+00:00",
        "changes": {},
    }

    repriced = client.patch(
        url,
        json={
            "account_config": {
                "quote_source": CUSTOM_SOURCE
                | {
                    "code": QUOTES.replace("last_price=10,", "last_price=12,"),
                }
            }
        },
    )
    assert repriced.status_code == 200, repriced.text
    sold = client.post(f"{url}/signal/run")
    assert sold.status_code == 200, sold.text
    assert sold.json()["message"] == "跟随已结转"
    assert holdings() == ("signal_follow", {})
    records = client.get(f"{url}/signal/records").json()["items"]
    record = records[0]
    assert record["sizing_prices"] is None
    assert record["settlement_prices"] == dict.fromkeys(TARGET, 12)
    assert records[-1]["settlement_prices"] == dict.fromkeys(TARGET, 10)
    card = build_signal_feishu_card("示例", {}, "股", adjustment=record["adjustment"])
    assert "510300.SH  卖出 500 股" in card["body"]["elements"][1]["text"]["content"]
    assert record["follow_ledger"] == {
        "status": "settled",
        "positions": {},
        "cash": 11591.2,
        "equity": 11591.2,
        "fee": 4.8,
        "fee_bps": 5,
    }
    run("10:30", "24")
    assert latest()["adjustment"]["changes"] == {}
    assert latest()["follow_ledger"]["fee"] == 0
    assert latest()["follow_ledger"]["cash"] == 11591.2
    assert latest()["node_change"]["changes"] == {
        symbol: {"before": quantity, "after": 0, "delta": -quantity} for symbol, quantity in TARGET.items()
    }

    response = client.patch(url, json={"account_config": {"follow_target": False}})
    assert response.status_code == 200, response.text
    assert client.post(f"{url}/signal/run").status_code == 200
    assert holdings() == ("signal_manual", {})


def test_signal_function_reports_adjustment_separately_from_target_change():
    assets = UnifiedAccountAssets(
        total_asset=10000,
        available_cash=10000,
        currency="CNY",
        update_time="",
        source="signal_function",
        market_value=None,
        extra={"positions_known": False},
    )
    assert signal_service._adjustment(assets, TARGET) == {"status": "unknown"}
    assets.extra["positions_known"] = True
    adjustment = signal_service._adjustment(assets, TARGET)
    assert adjustment["changes"] == {
        symbol: {"before": 0, "after": quantity, "delta": quantity} for symbol, quantity in TARGET.items()
    }
    card = build_signal_feishu_card("示例", TARGET, "股", adjustment=adjustment)
    assert card["body"]["elements"][0]["content"] == "**待调整**"
    assert signal_card_targets(card) == {symbol: f"{quantity} 股" for symbol, quantity in TARGET.items()}


def test_follow_keeps_signal_target_and_cash_when_fees_make_it_unaffordable(client, monkeypatch):
    monkeypatch.setattr(signal_service, "wake_notifications", Mock())
    code = f"""
def calculate_portfolio(context):
    assert context.account.total_asset == 8000
    return {{"mode": "quantity", "targets": {TARGET}}}
"""
    portfolio = create_portfolio(client, "A股", code)
    payload = signal_account_payload(portfolio)
    payload["account_config"]["follow_target"] = True
    response = client.post("/account/", json=payload)
    assert response.status_code == 201, response.text
    account_id = response.json()["id"]
    result = client.post(f"/account/{account_id}/signal/run")
    assert result.status_code == 200, result.text
    assert result.json()["message"] == "信号已生成，资金不足未结转"
    (record,) = client.get(f"/account/{account_id}/signal/records").json()["items"]
    assert record["targets"] == TARGET
    assert record["followed_target"] is False
    assert record["follow_ledger"] == {
        "status": "insufficient_cash",
        "positions": {},
        "cash": 8000,
        "equity": 8000,
        "fee": 0,
        "fee_bps": 5,
    }


@pytest.mark.parametrize(("legacy_rate", "expected_bps"), [(None, 5), (0.0003, 3)])
def test_new_manual_signal_defaults_to_follow_and_normalizes_fee_bps(client, legacy_rate, expected_bps):
    payload = signal_account_payload(None)
    payload["account_config"].pop("follow_target")
    if legacy_rate is not None:
        payload["account_config"]["follow_fee_rate"] = legacy_rate
    result = client.post("/account/", json=payload)
    assert result.status_code == 201, result.text
    account_id = result.json()["id"]
    config = client.get(f"/account/{account_id}/signal-config").json()["account_config"]
    assert config["follow_target"] is True
    assert config["follow_fee_bps"] == expected_bps
    assert "follow_fee_rate" not in config
