"""成交事实保留原生观察、金额及费用语义。"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from axile.server._native import _core
from scripts.business_facts_contract import business_evidence
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows, sql

_TABLES = ("business_trade_collection", "business_trade", "business_trade_conversion")


def _roundtrip(path, payload, *, is_success=0):
    record = persist_result(path, payload, is_success=is_success)
    return read_result(path, record), {name: rows(path, name) for name in _TABLES}


def _fill(**changes: object) -> dict:
    return {
        "trade_id": "same-native-id",
        "symbol": "rb2610",
        "order_id": "",
        "trade_time": "2026-10-20T09:31:02.000123+08:00",
        "trade_volume": 2,
        "trade_price": 3510.0,
        # 已保存值可能含冻结的合约乘数，不能重新计算为价格乘数量。
        "trade_value": 70200.0,
        "extra": {"exchange_id": "SHFE", "trading_day": "20261020"},
        **changes,
    }


@pytest.mark.parametrize("is_success", [0, 1])
def test_occurrence_identity_preserves_root_nested_duplicates_and_paths(native_database, is_success: int) -> None:
    payload = {
        "trades": [_fill(), _fill(), _fill(trade_price=3511)],
        "symbol_results": {
            "XAU/USD~demo": {"trades": [_fill(symbol="XAUUSD"), _fill(symbol="XAUUSD")]},
            "": {"trades": [_fill(trade_id="", symbol="")]},
            "rb2610": {"trades": []},
        },
    }
    restored, batch = _roundtrip(native_database, payload, is_success=is_success)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(len(batch["business_trade"])) == business_evidence(6)
    assert business_evidence(batch["business_trade"][3]["path"]) == business_evidence(
        "/symbol_results/XAU~1USD~0demo/trades/0"
    )
    assert business_evidence(batch["business_trade_collection"][1]["map_key"]) == business_evidence("XAU/USD~demo")
    assert business_evidence(batch["business_trade"][3]["symbol"]) == business_evidence("XAUUSD")
    assert business_evidence(
        len({(row["record_id"], row["path"]) for row in batch["business_trade"]})
    ) == business_evidence(6)


@pytest.mark.parametrize("is_success", [0, 1])
def test_fee_absent_null_zero_rebate_and_original_currency_remain_distinct(native_database, is_success: int) -> None:
    extras = [
        {},
        {"commission": None},
        {"commission": 0, "commission_asset": "HKD"},
        {"commission": -0.002, "commission_asset": "USD"},
        {"commission": 1.25, "commission_asset": ""},
        {"commission": "0.004", "commission_asset": None},
        {"commission": False, "commission_asset": 3},
        {"raw_trade_data": {"commission": 9.0, "tax": 0.0}},
    ]
    payload = {"trades": [_fill(extra=extra) for extra in extras]}
    restored, batch = _roundtrip(native_database, payload, is_success=is_success)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence([row["fee"] for row in batch["business_trade"]]) == business_evidence(
        [None, None, 0, -0.002, 1.25, None, None, None]
    )
    assert business_evidence([row["fee_currency"] for row in batch["business_trade"]]) == business_evidence(
        [None, None, "HKD", "USD", "", None, None, None]
    )


@pytest.mark.parametrize("is_success", [0, 1])
def test_original_native_amount_time_and_value_conversion_are_never_derived(native_database, is_success: int) -> None:
    observed = _fill(
        trade_time="2026-10-19T19:31:02Z",
        trade_value=98765,
        value_conversion={
            "price_currency": "USD",
            "value_currency": "CNY",
            "rate": 7,
            "reference": {"source": "frozen_planning", "quote": None, "zero": 0},
        },
    )
    missing = {"trade_volume": 2, "trade_price": 3510}
    payload = {"trades": [observed, missing]}
    restored, batch = _roundtrip(native_database, payload, is_success=is_success)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(restored["trades"][0]["trade_value"]) == business_evidence(98765)
    assert "trade_value" not in restored["trades"][1]
    assert "trade_time" not in restored["trades"][1]
    assert "symbol" not in restored["trades"][1]
    assert business_evidence(len(batch["business_trade_conversion"])) == business_evidence(1)
    assert business_evidence(batch["business_trade_conversion"][0]["rate"]) == business_evidence(7)
    assert "value_conversion" not in batch["business_trade"][0]["audit_evidence"].get("extensions", {})


@pytest.mark.parametrize("is_success", [0, 1])
def test_missing_null_empty_and_malformed_collections_and_members_are_lossless(
    native_database, is_success: int
) -> None:
    payload = {
        "trades": None,
        "symbol_results": {
            "missing": {},
            "empty": {"trades": []},
            "null": {"trades": None},
            "invalid": {"trades": {"legacy": True}},
            "members": {"trades": [None, 0, False, "bad", [], {}, {"trade_value": None}]},
        },
    }
    restored, _ = _roundtrip(native_database, payload, is_success=is_success)
    assert business_evidence(restored) == business_evidence(payload)
    assert "trades" not in restored["symbol_results"]["missing"]
    assert business_evidence(_roundtrip(native_database, {})[0]) == business_evidence({})


@pytest.mark.parametrize("is_success", [0, 1])
def test_malformed_stable_fields_and_conversion_preserve_original_representation(
    native_database, is_success: int
) -> None:
    payload = {
        "trades": [
            {
                "trade_id": 9,
                "trade_volume": "1.5",
                "trade_price": False,
                "trade_value": {"unavailable": True},
                "value_conversion": {"rate": "7.1", "price_currency": None, "reference": []},
                "extra": None,
                "new_field": [1, None, {"native": "proof"}],
            },
            {"value_conversion": None},
            {"value_conversion": []},
            {"value_conversion": {}},
        ]
    }
    restored, _ = _roundtrip(native_database, payload, is_success=is_success)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(restored) == business_evidence(payload)


def test_all_child_tables_cascade_from_terminal_record_without_native_id_uniqueness(native_database) -> None:
    payload = {"trades": [_fill(value_conversion={"rate": 7}), _fill(value_conversion={"rate": 7})]}
    record = persist_result(native_database, payload)
    with closing(sqlite3.connect(native_database)) as connection:
        for name in _TABLES:
            keys = connection.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
            assert any(key[2] == "executerecord" and key[3] == "record_id" and key[6] == "CASCADE" for key in keys)
            columns = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
            assert "trade_id" not in {column[1] for column in columns if column[5]}
        columns = connection.execute("PRAGMA table_info(business_trade)").fetchall()
        assert {column[1] for column in columns if column[5]} == {"record_id", "path"}
    assert all(rows(native_database, name) for name in _TABLES)
    sql(native_database, "DELETE FROM executerecord WHERE id=?", (record["id"],))
    assert all(not rows(native_database, name) for name in _TABLES)


def test_encoder_does_not_mutate_supplied_evidence(native_database) -> None:
    payload = {"trades": [_fill(extra={"commission": 0, "nested": [None, {}]})]}
    expected = copy.deepcopy(payload)
    persist_result(native_database, payload)
    assert business_evidence(payload) == business_evidence(expected)


def test_invalid_nonfinite_evidence_survives_sqlite_without_becoming_null(native_database) -> None:
    payload = {
        "trades": [
            float("inf"),
            {"trade_price": float("nan"), "extra": {"commission": float("-inf")}},
            {"value_conversion": float("inf")},
        ],
        "symbol_results": {"broken": {"trades": float("-inf")}},
    }
    restored, batch = _roundtrip(native_database, payload)
    assert business_evidence(restored["trades"][0]) == business_evidence(float("inf"))
    assert restored["trades"][1]["audit_evidence"]["invalid_fields"]["trade_price"] == {"invalid_number": "nan"}
    assert business_evidence(restored["trades"][1]["extra"]["commission"]) == business_evidence(float("-inf"))
    assert business_evidence(restored["trades"][2]["value_conversion"]) == business_evidence(float("inf"))
    assert business_evidence(restored["symbol_results"]["broken"]["trades"]) == business_evidence(float("-inf"))
    # 表中 JSON 均合法，非有限值由字段级带外元数据保留。
    for values in batch.values():
        json.dumps(values, allow_nan=False)


def test_imprecise_fee_never_publishes_a_different_native_numeric_value(native_database) -> None:
    native_fee = 2**53 + 1
    restored, batch = _roundtrip(native_database, {"trades": [_fill(extra={"commission": native_fee})]})
    assert business_evidence(restored["trades"][0]["extra"]["commission"]) == business_evidence(native_fee)
    assert batch["business_trade"][0]["fee"] is None


@pytest.mark.parametrize(
    "missing_table, message",
    [("business_trade_conversion", "missing trade conversion"), ("business_trade", "incomplete")],
)
def test_missing_typed_trade_facts_expose_corruption(native_database, missing_table, message) -> None:
    record = persist_result(native_database, {"trades": [_fill(value_conversion={"rate": 7.1})]})
    sql(native_database, f'DELETE FROM "{missing_table}" WHERE record_id=?', (record["id"],))

    with pytest.raises(_core.NativeError, match=message):
        read_result(native_database, record)
