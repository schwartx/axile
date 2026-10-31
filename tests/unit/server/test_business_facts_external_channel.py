"""外部渠道序列化事实的离线 SQL 往返，不依赖私有插件或真实账户。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest

from scripts.business_facts_contract import business_evidence
from tests.sqlite_oracle import utc_time
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_before_assets,
    read_result,
    rows,
    sql,
)


def _sql_round_trip(path, payload: Mapping[str, object]):
    """实际提交全部领域事实，并经原生完整读取还原数组发生顺序。"""
    record = persist_result(path, deepcopy(payload))
    # 反向索引使原生加载先看到逆序发生，数组顺序必须依照 ordinal 还原。
    for name, owner in (
        ("execution_order", "record_id"),
        ("business_trade", "record_id"),
        ("business_asset_position", "asset_id"),
    ):
        sql(path, f"CREATE INDEX IF NOT EXISTS oracle_reverse_{name} ON {name}({owner},ordinal DESC)")
    loaded = {
        name: rows(path, name)
        for name in ("execution_order", "business_trade", "business_asset_snapshot", "business_asset_position")
    }
    return read_result(path, record), loaded


def _conversion() -> dict[str, object]:
    return {
        "price_currency": "CNY",
        "value_currency": "USD",
        "rate": 1.0101010101010102,
        "reference": {
            "quote_index": {"index": 1.0, "time_ms": 1792440000123, "source": "asset_index"},
            "usdt_index": {"index": 0.99, "time_ms": 1792440000111, "source": "asset_index"},
            "generation": 17,
        },
    }


def _order(*, symbol="XAGCNY", order_id="9007199254740993", day="20261020", **changes):
    value = {
        "order_id": order_id,
        "symbol": symbol,
        "direction": "SELL",
        "order_type": "LIMIT",
        "volume": 0.125,
        "price": 3210.125,
        "status": "部分成交",
        "filled_volume": 0.0625,
        "avg_price": 3209.875,
        "create_time": "2026-10-20T00:00:00.123456+08:00",
        "update_time": "2026-10-19T16:00:01.000Z",
        "value_conversion": _conversion(),
        "extra": {
            "exchange_id": "CUSTOM",
            "trading_day": day,
            "client_order_id": "offline-synthetic-~/%",
            "native_order_type": "STOP_MARKET",
            "original_order_type": "STOP_MARKET",
            "original_status": "PARTIALLY_FILLED",
            "execution_type": "TRADE",
            "reduce_only": False,
            "close_position": False,
            "position_side": "SHORT",
            "time_in_force": "GTX",
            "price_protect": False,
            "good_till_date": 0,
            "commission": None,
            "commission_asset": "",
        },
    }
    value.update(changes)
    return value


def _trade(*, symbol="XAGCNY", trade_id="9007199254740995", day="20261020", extra=None, **changes):
    value = {
        "trade_id": trade_id,
        "order_id": "9007199254740993",
        "symbol": symbol,
        "trade_time": "2026-10-20T00:00:00.123456+08:00",
        "trade_volume": 0.0625,
        "trade_price": 3209.875,
        # 历史材料可能保留舍入后的金额；存储不能重新乘价量或汇率。
        "trade_value": 202.64,
        "value_conversion": _conversion(),
        "extra": {
            "exchange_id": "CUSTOM",
            "trading_day": day,
            "symbol": symbol,
            "order_id": "9007199254740993",
            "direction": "SELL",
            "timestamp": 1792425600123,
            "realized_pnl": -1.25,
            "is_maker": False,
            "position_side": "SHORT",
        },
    }
    if extra is not None:
        value["extra"].update(extra)
    value.update(changes)
    return value


def test_custom_order_occurrences_keep_native_identity_scope_and_snapshot_order(native_database):
    pending = _order(filled_volume=0.0, avg_price=0.0, status="待成交")
    filled = _order(filled_volume=0.125, status="已成交")
    nested = [pending, filled, deepcopy(filled), _order(order_id=""), _order(day="20261019")]
    payload = {
        "orders": [_order(symbol="XAUUSD", day="20261019"), _order(order_id="")],
        "symbol_results": {
            "XAG/CNY~SHORT": {"orders": nested},
            "XAUUSD": {"orders": [_order(symbol="XAUUSD")]},
        },
    }

    actual, loaded = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    assert business_evidence(list(actual["symbol_results"])) == business_evidence(["XAG/CNY~SHORT", "XAUUSD"])
    assert business_evidence(len(actual["symbol_results"]["XAG/CNY~SHORT"]["orders"])) == business_evidence(5)
    snapshots = actual["symbol_results"]["XAG/CNY~SHORT"]["orders"]
    assert business_evidence([row["status"] for row in snapshots[:3]]) == business_evidence(
        ["待成交", "已成交", "已成交"]
    )
    assert business_evidence(snapshots[0]["order_id"]) == business_evidence("9007199254740993")
    assert snapshots[0]["create_time"] == utc_time("2026-10-20T00:00:00.123456+08:00")
    assert snapshots[0]["update_time"] == utc_time("2026-10-19T16:00:01.000Z")
    assert business_evidence(snapshots[0]["price"]) == business_evidence(3210.125)
    assert business_evidence(snapshots[0]["value_conversion"]) == business_evidence(_conversion())
    assert business_evidence(len(loaded["execution_order"])) == business_evidence(8)


@pytest.mark.parametrize("native_id", [0, "0", "", None, "000001", "900719925474099312345"])
def test_custom_native_order_and_trade_ids_are_not_coerced(native_database, native_id):
    order_payload = {"orders": [_order(order_id=native_id)]}
    trade_payload = {"trades": [_trade(trade_id=native_id, order_id=native_id)]}

    decoded_order, _ = _sql_round_trip(native_database, order_payload)
    decoded_trade, _ = _sql_round_trip(native_database, trade_payload)

    assert business_evidence(decoded_order) == business_evidence(order_payload)
    assert business_evidence(decoded_trade) == business_evidence(trade_payload)
    assert type(decoded_order["orders"][0]["order_id"]) is type(native_id)
    assert type(decoded_trade["trades"][0]["trade_id"]) is type(native_id)


@pytest.mark.parametrize(
    "fee",
    [
        {},
        {"commission": None},
        {"commission": 0, "commission_asset": "USD"},
        {"commission": 0.0, "commission_asset": "HKD"},
        {"commission": -0.0000125, "commission_asset": "HKD"},
        {"commission": 0.0123, "commission_asset": "CNY"},
        {"commission": None, "commission_asset": ""},
    ],
)
def test_custom_fees_preserve_missing_unknown_zero_rebate_and_native_asset(native_database, fee):
    payload = {"trades": [_trade(extra=fee)]}

    actual, _ = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    decoded = actual["trades"][0]
    assert business_evidence(decoded["trade_value"]) == business_evidence(202.64)
    assert decoded["trade_value"] != decoded["trade_volume"] * decoded["trade_price"]
    assert business_evidence(decoded["value_conversion"]) == business_evidence(_conversion())
    assert business_evidence("commission" in decoded["extra"]) == business_evidence("commission" in fee)
    assert business_evidence("commission_asset" in decoded["extra"]) == business_evidence("commission_asset" in fee)
    if "commission" in fee:
        assert type(decoded["extra"]["commission"]) is type(fee["commission"])


def test_custom_trades_keep_duplicate_ids_symbols_days_and_root_evidence(native_database):
    first = _trade(extra={"commission": 0.0, "commission_asset": "USD"})
    prior_day = _trade(day="20261019", trade_time="2026-10-19T16:00:00.000Z")
    payload = {
        "trades": [_trade(symbol="XAUUSD"), _trade(trade_id="")],
        "symbol_results": {
            "XAG/CNY~SHORT": {"trades": [first, deepcopy(first), prior_day]},
            "XAUUSD": {"trades": [_trade(symbol="XAUUSD")]},
        },
    }

    actual, loaded = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    nested = actual["symbol_results"]["XAG/CNY~SHORT"]["trades"]
    assert business_evidence(len(nested)) == business_evidence(3)
    assert business_evidence([row["extra"]["trading_day"] for row in nested]) == business_evidence(
        ["20261020", "20261020", "20261019"]
    )
    assert nested[2]["trade_time"] == utc_time("2026-10-19T16:00:00.000Z")
    assert business_evidence(len(loaded["business_trade"])) == business_evidence(6)


def _asset_snapshot():
    return {
        "available_cash": 0.0,
        "total_asset": 5050.50505050505,
        "market_value": 4012.5,
        "currency": "USD",
        "source": "real",
        "update_time": "2026-10-20T00:00:01.123456+08:00",
        "positions": [
            {
                "symbol": "XAGCNY",
                "direction": "SHORT",
                "volume": 0.5,
                "available_volume": 0.5,
                "market_value": 2006.25,
                "avg_price": 4012.5,
                "extra": {
                    "quote_asset": "CNY",
                    "unrealized_pnl": -2.5,
                    "position_attributes": {
                        "position_side": "SHORT",
                        "native_notional": -1986.1875,
                        "initial_margin": 79.4475,
                        "maintenance_margin": 9.9309375,
                        "isolated_margin": 0.0,
                        "isolated_wallet": 0,
                        "unrealized_pnl": -2.5,
                        "native_updated_at": 1792425600123,
                    },
                },
            },
            {
                "symbol": "XAGCNY",
                "direction": "LONG",
                "volume": 0.5,
                "available_volume": 0.0,
                "market_value": 2006.25,
                "avg_price": 4012.5,
                "extra": {
                    "quote_asset": "CNY",
                    "position_attributes": {
                        "position_side": "LONG",
                        "open_price": 3980.0,
                        "mark_price": 3972.375,
                        "liquidation_price": 0.0,
                        "margin_asset": "CNY",
                        "adl": 0,
                        "risk_updated_at": 1792425600222,
                    },
                },
            },
        ],
        "extra": {
            "equity_source": "totalMarginBalance",
            "equity_basis": "custom_margin_equity",
            "native_currency": "EUR",
            "native_equity": 5000,
            "multi_assets": True,
            "native_available": 321.0,
            "available_cash_source": "availableBalance",
            "available_cash_conversion": "unavailable_missing_ask_rate",
            "available_cash_status": "unavailable",
            "equity_conversion": "USDEUR.index",
            "asset_indices": {
                "USD": {"index": 0.99, "ask_rate": None, "source": "assumed_reference_parity"},
                "CNY": {"index": 1.0, "time_ms": 1792425600000},
            },
            "asset_index_refresh": {"generation": 17},
        },
    }


def test_custom_assets_preserve_hedged_positions_native_fields_and_unavailable_cash(native_database):
    payload = {"account_assets": _asset_snapshot()}

    actual, _ = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    snapshot = actual["account_assets"]
    assert business_evidence(snapshot["currency"]) == business_evidence("USD")
    assert business_evidence(snapshot["available_cash"]) == business_evidence(0.0)
    assert business_evidence(snapshot["extra"]["available_cash_status"]) == business_evidence("unavailable")
    assert business_evidence(snapshot["extra"]["native_available"]) == business_evidence(321.0)
    assert business_evidence(snapshot["extra"]["native_equity"]) == business_evidence(5000)
    assert business_evidence([row["direction"] for row in snapshot["positions"]]) == business_evidence(
        ["SHORT", "LONG"]
    )
    short = snapshot["positions"][0]["extra"]["position_attributes"]
    long = snapshot["positions"][1]["extra"]["position_attributes"]
    assert short["native_notional"] < 0
    assert "open_price" not in short
    assert "mark_price" not in short
    assert "liquidation_price" not in short
    assert "margin_asset" not in short
    assert business_evidence(long["open_price"]) == business_evidence(3980.0)
    assert business_evidence(long["liquidation_price"]) == business_evidence(0.0)


def test_custom_before_and_after_assets_are_independent_observations(native_database):
    before = _asset_snapshot()
    after = deepcopy(before)
    after["update_time"] = "2026-10-20T00:00:03.000Z"
    after["total_asset"] = 5048.0
    after["positions"][0]["volume"] = 0.25
    receipt = freeze_input(native_database, {}, input_id="offline-custom-before", before_assets=before)
    actual, loaded = _sql_round_trip(native_database, {"account_assets": after})
    actual["before_account_assets"] = read_before_assets(native_database, receipt["input_id"])

    assert business_evidence({key: value for key, value in actual.items() if key != "before_account_assets"}) == (
        business_evidence({"account_assets": after})
    )
    assert business_evidence(actual["before_account_assets"], context="account_assets") == business_evidence(
        before, context="account_assets"
    )
    snapshots = loaded["business_asset_snapshot"]
    assert business_evidence(len(snapshots)) == business_evidence(2)
    assert business_evidence({row["scope"] for row in snapshots}) == business_evidence({"before", "after"})
    assert business_evidence(len({row["asset_id"] for row in snapshots})) == business_evidence(2)
    for row in snapshots:
        assert (row["input_id"] is None) != (row["record_id"] is None)


@pytest.mark.parametrize("positions", [None, [], [{"symbol": "XAUUSD", "volume": 0, "extra": {}}]])
def test_custom_position_container_unknown_empty_and_explicit_zero_are_distinct(native_database, positions):
    snapshot = _asset_snapshot()
    snapshot["positions"] = positions
    payload = {"account_assets": snapshot}

    actual, _ = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    assert business_evidence(actual["account_assets"]["positions"]) == business_evidence(positions)


def test_custom_sizing_ticks_and_native_price_conversion_remain_frozen(native_database):
    payload = {
        "symbol_results": {
            "XAG/CNY~SHORT": {
                "symbol": "XAGCNY",
                "target_volume": 0.125,
                "final_volume": 0.0625,
                "first_tick": {
                    "symbol": "XAGCNY",
                    "last_price": 3210.125,
                    "bid_price": 3209.875,
                    "ask_price": 3210.25,
                    "bid_volume": 0,
                    "ask_volume": 0.125,
                    "timestamp": 1792425600123,
                    "update_time": "2026-10-20T00:00:00.123456+08:00",
                    "book_valid": False,
                    "extra": {"lastUpdateId": "9007199254740997", "source": "depth"},
                },
                "sizing": {
                    "symbol": "XAGCNY",
                    "account_weight": 0.08,
                    "equity": 5050.50505050505,
                    "target_quantity": 0.125,
                    "current_quantity": 0.0625,
                    "unit_multiplier": 1.0,
                    "reference_price": 3210.125,
                    "quantity_step": 0.001,
                    "min_quantity": 0.001,
                    "min_notional": 0,
                    "value_conversion": _conversion(),
                },
            }
        },
        "first_ticks": {"XAGCNY": {"timestamp": 1792425600001, "last_price": 3210.0}},
        "target_volume": {"XAGCNY": 0.0},
    }

    actual, _ = _sql_round_trip(native_database, payload)

    assert business_evidence(actual) == business_evidence(payload)
    nested = actual["symbol_results"]["XAG/CNY~SHORT"]
    assert business_evidence(nested["first_tick"]["timestamp"]) == business_evidence(1792425600123)
    assert business_evidence(actual["first_ticks"]["XAGCNY"]["timestamp"]) == business_evidence(1792425600001)
    assert business_evidence(nested["target_volume"]) == business_evidence(0.125)
    assert business_evidence(actual["target_volume"]["XAGCNY"]) == business_evidence(0.0)
    assert business_evidence(nested["sizing"]["value_conversion"]) == business_evidence(_conversion())
