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


def test_external_all_view_queries_known_and_position_symbols_and_labels_coverage():
    instance = executor("external")
    instance.account_orders.apply(order("rb2610"))
    instance.get_account_assets = lambda: SimpleNamespace(positions=[SimpleNamespace(symbol="ag2612")])
    queried = []
    instance.query_recent_orders = lambda symbol: queried.append(("orders", symbol)) or []
    instance.query_recent_trades = lambda symbol: queried.append(("trades", symbol)) or []
    result = run_activity_command(instance, {"refresh": True})
    assert set(queried) == {(kind, symbol) for kind in ("orders", "trades") for symbol in ("rb2610", "ag2612")}
    assert "仅覆盖已观测及持仓品种" in result["scope"]


def test_external_explicit_symbol_does_not_need_positions_or_other_symbols():
    instance = executor("external")
    queried = []
    instance.query_recent_orders = lambda symbol: queried.append(symbol) or []
    instance.query_recent_trades = lambda symbol: queried.append(symbol) or []
    result = run_activity_command(instance, {"refresh": True, "symbol": "rb2610"})
    assert queried == ["rb2610", "rb2610"]
    assert result["errors"] == []


@pytest.mark.parametrize("symbol", [None, "XRPUSDT"])
def test_binance_history_discovers_closed_positions_on_a_fresh_connection(symbol):
    instance = executor("binance")
    assert instance.account_orders.activity.symbols() == []
    queried = []
    instance.query_recent_orders = lambda value: (
        queried.append(("orders", value))
        or [order("XRPUSDT").model_copy(update={"status": "已成交", "filled_volume": 2})]
    )
    instance.query_recent_trades = lambda value: (
        queried.append(("trades", value))
        or [TradeRecord.create(trade_id="fill", symbol="XRPUSDT", trade_price=100, trade_volume=2)]
    )
    result = run_activity_command(instance, {"refresh": True, "symbol": symbol})
    assert queried == [("orders", symbol), ("trades", symbol)]
    assert result["orders"][0]["symbol"] == result["trades"][0]["symbol"] == "XRPUSDT"
    assert result["symbols"] == ["XRPUSDT"]
    assert "最近24小时" in result["scope"]
    assert ("全账户" if symbol is None else "指定品种") in result["scope"]
    assert result["errors"] == []


def test_binance_account_history_failure_is_not_a_successful_empty_result():
    instance = executor("binance")

    def fail(_symbol):
        raise RuntimeError("api_key=secret")

    instance.query_recent_orders = instance.query_recent_trades = fail
    result = run_activity_command(instance, {"refresh": True})
    assert len(result["errors"]) == 2
    assert all("账户" in error and "核对失败" in error for error in result["errors"])
    assert "secret" not in str(result)


def test_binance_account_history_warns_when_exchange_limit_is_reached():
    instance = executor("binance")
    instance.query_recent_orders = lambda _symbol: [order("XRPUSDT")] * 1000
    instance.query_recent_trades = lambda _symbol: []
    result = run_activity_command(instance, {"refresh": True})
    assert any("账户委托" in error and "截断" in error for error in result["errors"])


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


@pytest.mark.parametrize(
    "effect,expected", [(1, "OPEN"), (2, "CLOSE"), (3, "CLOSETODAY"), (4, "CLOSEYESTERDAY"), (0, None)]
)
def test_gm_trade_and_order_share_native_offset_and_keep_distinct_times(effect, expected):
    from axile.executor.gm.common import convert_gm_order_to_unified, convert_gm_trade_to_trade_record

    instance = executor("gm")
    submitted = "2026-10-16T10:26:17+08:00"
    filled = "2026-10-16T13:59:46+08:00"
    row = {
        "cl_ord_id": "gm-one",
        "symbol": "SZSE.159920",
        "side": 1,
        "position_effect": effect,
        "volume": 5500,
        "price": 1.425,
        "status": 3,
        "order_type": 1,
        "filled_volume": 5500,
        "created_at": submitted,
        "updated_at": filled,
    }
    instance.account_orders.activity.observe_order(convert_gm_order_to_unified(row))
    instance.account_orders.activity.observe_trade(
        convert_gm_trade_to_trade_record({**row, "created_at": filled, "exec_id": "fill-one"})
    )
    result = run_activity_command(instance, {})
    assert result["orders"][0]["create_time"] == submitted
    assert result["orders"][0]["update_time"] == filled
    assert result["trades"][0]["time"] == filled
    assert result["orders"][0]["offset"] == result["trades"][0]["offset"] == expected
    assert result["trades"][0]["direction"] == "BUY"


def test_gm_missing_native_times_do_not_become_local_sampling_time():
    from axile.executor.gm.common import convert_gm_order_to_unified, convert_gm_trade_to_trade_record

    instance = executor("gm")
    row = {"cl_ord_id": "gm-one", "symbol": "SZSE.159920", "side": 1, "volume": 1, "price": 1}
    instance.account_orders.activity.observe_order(convert_gm_order_to_unified(row))
    instance.account_orders.activity.observe_trade(convert_gm_trade_to_trade_record({**row, "exec_id": "fill-one"}))
    result = run_activity_command(instance, {})
    assert result["orders"][0]["create_time"] is None
    assert result["orders"][0]["update_time"] is None
    assert result["trades"][0]["time"] == ""


@pytest.mark.parametrize(
    "channel,extra,expected",
    [
        (
            "ctp",
            {"trading_day": "20261019", "hedge_flag": "3", "offset_flag": "0"},
            {"trading_day": "20261019", "hedge_flag": "3", "offset": "0"},
        ),
        (
            "tq",
            {"exchange_trade_id": "exchange-fill-1", "offset": "CLOSETODAY"},
            {"exchange_trade_id": "exchange-fill-1", "offset": "CLOSETODAY"},
        ),
        (
            "gm",
            {"raw_trade_data": {"amount": 7837.5, "commission": 0, "cost": 7837.5, "password": "secret"}},
            {"amount": 7837.5, "commission": 0, "cost": 7837.5},
        ),
        (
            "external",
            {
                "commission": "0",
                "commission_asset": "CNY",
                "realized_pnl": "-2.5",
                "is_maker": False,
                "position_side": "SHORT",
            },
            {
                "commission": 0,
                "commission_asset": "CNY",
                "realized_pnl": -2.5,
                "is_maker": False,
                "position_side": "SHORT",
            },
        ),
    ],
)
def test_channel_trade_columns_preserve_native_fields_and_zero_values(channel, extra, expected):
    instance = executor(channel)
    instance.account_orders.activity.observe_trade(
        TradeRecord.create(trade_id="one", symbol="symbol", trade_price=100, trade_volume=1, extra=extra)
    )
    result = run_activity_command(instance, {})
    assert result["channel"] == channel
    assert {key: result["trades"][0][key] for key in expected} == expected
    assert "secret" not in str(result)


def test_missing_native_amount_and_fees_do_not_fall_back_to_price_times_quantity_or_zero():
    instance = executor("gm")
    instance.account_orders.activity.observe_trade(
        TradeRecord.create(
            trade_id="one",
            symbol="symbol",
            trade_price=100,
            trade_volume=2,
            extra={"raw_trade_data": {"amount": "NaN", "commission": None, "cost": "invalid"}},
        )
    )
    trade = run_activity_command(instance, {})["trades"][0]
    assert trade["amount"] is trade["commission"] is trade["cost"] is trade["is_maker"] is None


def test_order_average_and_channel_fields_are_whitelisted_and_paginated_after_aggregation():
    instance = executor("ctp")
    book = instance.account_orders
    book.apply(
        order(filled_volume=3, extra={"hedge_flag": "3", "time_condition": "3", "password": "secret"}).model_copy(
            update={"volume": 3}
        )
    )
    for identity, quantity, price in (("one", 1, 100), ("two", 2, 106)):
        book.activity.observe_trade(
            TradeRecord.create(
                trade_id=identity, symbol="rb2610", order_id="one", trade_volume=quantity, trade_price=price
            )
        )
    result = run_activity_command(instance, {"limit": 1})
    assert result["orders"][0]["avg_price"] == 104
    assert result["orders"][0]["avg_price_source"] == "trades"
    assert result["orders"][0]["attributes"] == {"hedge_flag": "3", "time_condition": "3"}
    assert "secret" not in str(result)
    assert len(result["trades"]) == 1


def test_order_attributes_keep_false_zero_and_hide_invalid_values_and_raw_payload():
    from axile.server.account_orders_runtime import public_order

    result = public_order(
        order(
            filled_volume=0,
            avg_price=104,
            extra={
                "raw_order_data": {
                    "filled_amount": 0,
                    "order_business": 0,
                    "reduce_only": False,
                    "password": "secret",
                    "stop_price": "NaN",
                    "min_volume": True,
                }
            },
        )
    )
    assert result["avg_price"] is result["avg_price_source"] is None
    assert result["attributes"] == {"filled_amount": 0, "order_business": "0", "reduce_only": False}
    assert "raw_order_data" not in result
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "channel,extra,expected",
    [
        (
            "ctp",
            {
                "exchange_id": "SHFE",
                "order_sys_id": "sys-1",
                "order_ref": "ref-1",
                "event_date": "20261016",
                "event_clock": "21:01:02",
            },
            {
                "exchange_id": "SHFE",
                "order_sys_id": "sys-1",
                "order_ref": "ref-1",
                "event_date": "20261016",
                "event_clock": "21:01:02",
            },
        ),
        (
            "tq",
            {"exchange_id": "SHFE", "tq_symbol": "SHFE.rb2610"},
            {"exchange_id": "SHFE", "tq_symbol": "SHFE.rb2610"},
        ),
        (
            "gm",
            {"gm_symbol": "SHSE.600000", "cl_ord_id": "client-1", "exchange_order_id": "counter-1"},
            {"gm_symbol": "SHSE.600000", "cl_ord_id": "client-1", "exchange_order_id": "counter-1"},
        ),
        (
            "external",
            {"client_order_id": "client-1", "execution_type": "TRADE", "order_status": "PARTIALLY_FILLED"},
            {"client_order_id": "client-1", "execution_type": "TRADE", "order_status": "PARTIALLY_FILLED"},
        ),
    ],
)
def test_trade_details_use_only_native_record_fields_and_hide_raw_payload(channel, extra, expected):
    instance = executor(channel)
    instance.account_orders.apply(order().model_copy(update={"status": "已成交"}))
    instance.account_orders.activity.observe_trade(
        TradeRecord.create(
            trade_id="fill",
            symbol="rb2610",
            order_id="one",
            trade_volume=1,
            trade_price=100,
            extra={**extra, "password": "secret", "raw_trade_data": {"token": "secret", "account_id": "private"}},
        )
    )
    result = run_activity_command(instance, {})["trades"][0]
    assert result["attributes"] == expected
    assert "secret" not in str(result)
    assert "private" not in str(result)
    assert result["attributes"].get("order_status") != "已成交"


def test_gm_callback_details_keep_native_symbol_without_exposing_raw_fields():
    instance = executor("gm")
    instance.account_orders.activity.observe_trade(
        TradeRecord.create(
            trade_id="fill",
            symbol="600000.SH",
            order_id="one",
            trade_volume=1,
            trade_price=100,
            extra={"channel_type": "gm", "raw_trade_data": {"symbol": "SHSE.600000", "password": "secret"}},
        )
    )
    assert run_activity_command(instance, {})["trades"][0]["attributes"] == {"gm_symbol": "SHSE.600000"}
