"""账户近期记录只在显式核对时访问渠道，失败不清空已知记录。"""

from types import SimpleNamespace

import pytest

from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.server.account_activity_runtime import run_activity_command


def executor(channel="tq"):
    book = AccountOrderBook()
    return SimpleNamespace(channel_type=channel, account_orders=book)


def order(symbol="rb2610", **changes):
    return UnifiedOrder(
        order_id="one",
        symbol=symbol,
        direction="BUY",
        order_type="LIMIT",
        volume=2,
        price=100,
        status="待成交",
        **changes,
    )


def test_passive_read_never_queries_and_symbol_filter_cannot_leak_other_orders():
    instance = executor()
    instance.account_orders.apply(order())
    instance.account_orders.apply(order("ag2612"))
    result = run_activity_command(instance, {"symbol": "rb2610"})
    assert [row["symbol"] for row in result["orders"]] == ["rb2610"]
    assert result["queried_at"] is None


def test_partial_query_failure_preserves_old_data_and_does_not_publish_exception():
    instance = executor()
    instance.account_orders.apply(order())

    def fail(_symbol):
        raise RuntimeError("password=secret")

    instance.query_recent_orders = fail
    instance.query_recent_trades = lambda _symbol: [
        TradeRecord.create(trade_id="t1", symbol="rb2610", trade_price=100, trade_volume=1)
    ]
    result = run_activity_command(instance, {"refresh": True})
    assert len(result["orders"]) == len(result["trades"]) == 1
    assert result["errors"] and "secret" not in str(result)
    assert "当前交易日" in result["scope"]


def test_binance_all_view_queries_known_and_position_symbols_and_labels_coverage():
    instance = executor("binance")
    instance.account_orders.apply(order("BTCUSDT"))
    instance.get_account_assets = lambda: SimpleNamespace(positions=[SimpleNamespace(symbol="ETHUSDT")])
    queried = []
    instance.query_recent_orders = lambda symbol: queried.append(("orders", symbol)) or []
    instance.query_recent_trades = lambda symbol: queried.append(("trades", symbol)) or []
    result = run_activity_command(instance, {"refresh": True})
    assert set(queried) == {(kind, symbol) for kind in ("orders", "trades") for symbol in ("BTCUSDT", "ETHUSDT")}
    assert "仅覆盖已观测及持仓品种" in result["scope"]


def test_binance_explicit_symbol_does_not_need_positions_or_other_symbols():
    instance = executor("binance")
    queried = []
    instance.query_recent_orders = lambda symbol: queried.append(symbol) or []
    instance.query_recent_trades = lambda symbol: queried.append(symbol) or []
    result = run_activity_command(instance, {"refresh": True, "symbol": "BTCUSDT"})
    assert queried == ["BTCUSDT", "BTCUSDT"]
    assert result["errors"] == []


def test_history_orders_are_sorted_by_native_tq_time_and_public_payload_is_whitelisted():
    instance = executor()
    instance.account_orders.apply(order(extra={"insert_date_time": 1_700_000_000_000_000_000, "password": "secret"}))
    instance.account_orders.apply(order("ag2612", extra={"insert_date_time": 1_700_000_001_000_000_000}))
    result = run_activity_command(instance, {"limit": 1})
    assert result["orders"][0]["symbol"] == "ag2612"
    assert result["truncated"]
    assert "secret" not in str(result)


def test_terminal_list_is_paginated_independently_of_newer_active_orders():
    instance = executor()
    older = order().model_copy(update={"status": "已拒绝", "create_time": "2026-10-15T10:00:00"})
    newer = order("ag2612").model_copy(update={"create_time": "2026-10-16T10:00:00"})
    instance.account_orders.activity.observe_order(older)
    instance.account_orders.activity.observe_order(newer)
    result = run_activity_command(instance, {"limit": 1})
    assert result["orders"][0]["symbol"] == "ag2612"
    assert result["ended_orders"][0]["symbol"] == "rb2610"


@pytest.mark.parametrize("direction,expected", [("0", "BUY"), ("1", "SELL"), ("BUY", "BUY")])
def test_trade_projection_normalizes_channel_directions(direction, expected):
    instance = executor()
    instance.account_orders.activity.observe_trade(
        TradeRecord.create(trade_id="t1", symbol="rb2610", direction=direction, trade_volume=1)
    )
    result = run_activity_command(instance, {})
    assert result["trades"][0]["direction"] == expected
