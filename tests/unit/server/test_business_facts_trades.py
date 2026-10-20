"""成交事实保留原生观察、金额及费用语义。"""

from __future__ import annotations

import copy
import json

import pytest
import sqlalchemy as sa

from axile.server.business_facts.trades import (
    TABLES,
    decode_trades,
    encode_trades,
    trade,
    trade_collection,
    trade_conversion,
)
from scripts.business_facts_contract import business_evidence


def _roundtrip(payload: dict, *, database: bool = False) -> tuple[dict, dict]:
    batch = encode_trades(7, payload)
    if not database:
        return decode_trades({table.name: values for table, values in batch.items()}), batch
    metadata = sa.MetaData()
    sa.Table("executerecord", metadata, sa.Column("id", sa.Integer, primary_key=True))
    copied = {table.name: table.to_metadata(metadata) for table in TABLES}
    engine = sa.create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            connection.execute(metadata.tables["executerecord"].insert(), {"id": 7})
            for table, values in batch.items():
                if values:
                    connection.execute(copied[table.name].insert(), values)
            loaded = {name: connection.execute(sa.select(table)).mappings().all() for name, table in copied.items()}
            result = decode_trades(loaded)
        return result, batch
    finally:
        engine.dispose()


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


@pytest.mark.parametrize("database", [False, True])
def test_occurrence_identity_preserves_root_nested_duplicates_and_paths(database: bool) -> None:
    payload = {
        "trades": [_fill(), _fill(), _fill(trade_price=3511)],
        "symbol_results": {
            "BTC/USDT~demo": {"trades": [_fill(symbol="BTCUSDT"), _fill(symbol="BTCUSDT")]},
            "": {"trades": [_fill(trade_id="", symbol="")]},
            "rb2610": {"trades": []},
        },
    }
    restored, batch = _roundtrip(payload, database=database)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(len(batch[trade])) == business_evidence(6)
    assert business_evidence(batch[trade][3]["path"]) == business_evidence("/symbol_results/BTC~1USDT~0demo/trades/0")
    assert business_evidence(batch[trade_collection][1]["map_key"]) == business_evidence("BTC/USDT~demo")
    assert business_evidence(batch[trade][3]["symbol"]) == business_evidence("BTCUSDT")
    assert business_evidence(len({(row["record_id"], row["path"]) for row in batch[trade]})) == business_evidence(6)


@pytest.mark.parametrize("database", [False, True])
def test_fee_absent_null_zero_rebate_and_original_currency_remain_distinct(database: bool) -> None:
    extras = [
        {},
        {"commission": None},
        {"commission": 0, "commission_asset": "BNB"},
        {"commission": -0.002, "commission_asset": "USDT"},
        {"commission": 1.25, "commission_asset": ""},
        {"commission": "0.004", "commission_asset": None},
        {"commission": False, "commission_asset": 3},
        {"raw_trade_data": {"commission": 9.0, "tax": 0.0}},
    ]
    payload = {"trades": [_fill(extra=extra) for extra in extras]}
    restored, batch = _roundtrip(payload, database=database)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence([row["fee"] for row in batch[trade]]) == business_evidence(
        [None, None, 0, -0.002, 1.25, None, None, None]
    )
    assert business_evidence([row["fee_currency"] for row in batch[trade]]) == business_evidence(
        [None, None, "BNB", "USDT", "", None, None, None]
    )


@pytest.mark.parametrize("database", [False, True])
def test_original_native_amount_time_and_value_conversion_are_never_derived(database: bool) -> None:
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
    restored, batch = _roundtrip(payload, database=database)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(restored["trades"][0]["trade_value"]) == business_evidence(98765)
    assert "trade_value" not in restored["trades"][1]
    assert "trade_time" not in restored["trades"][1]
    assert "symbol" not in restored["trades"][1]
    assert business_evidence(len(batch[trade_conversion])) == business_evidence(1)
    assert business_evidence(batch[trade_conversion][0]["rate"]) == business_evidence(7)
    assert "value_conversion" not in batch[trade][0]["audit_evidence"].get("extensions", {})


@pytest.mark.parametrize("database", [False, True])
def test_missing_null_empty_and_malformed_collections_and_members_are_lossless(database: bool) -> None:
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
    restored, _ = _roundtrip(payload, database=database)
    expected = copy.deepcopy(payload)
    del expected["symbol_results"]["missing"]
    assert business_evidence(restored) == business_evidence(expected)
    assert business_evidence(_roundtrip({})[0]) == business_evidence({})


@pytest.mark.parametrize("database", [False, True])
def test_malformed_stable_fields_and_conversion_preserve_original_representation(database: bool) -> None:
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
    restored, _ = _roundtrip(payload, database=database)
    assert business_evidence(restored) == business_evidence(payload)
    assert business_evidence(restored) == business_evidence(payload)


def test_all_child_tables_cascade_from_terminal_record_without_native_id_uniqueness() -> None:
    for table in TABLES:
        assert business_evidence(next(iter(table.c.record_id.foreign_keys)).ondelete) == business_evidence("CASCADE")
        assert "trade_id" not in {column.name for column in table.primary_key}
    assert business_evidence(set(trade.primary_key.columns.keys())) == business_evidence({"record_id", "path"})


def test_encoder_does_not_mutate_supplied_evidence() -> None:
    payload = {"trades": [_fill(extra={"commission": 0, "nested": [None, {}]})]}
    expected = copy.deepcopy(payload)
    encode_trades(7, payload)
    assert business_evidence(payload) == business_evidence(expected)


def test_invalid_nonfinite_evidence_survives_sqlite_without_becoming_null() -> None:
    payload = {
        "trades": [
            float("inf"),
            {"trade_price": float("nan"), "extra": {"commission": float("-inf")}},
            {"value_conversion": float("inf")},
        ],
        "symbol_results": {"broken": {"trades": float("-inf")}},
    }
    restored, batch = _roundtrip(payload, database=True)
    assert business_evidence(restored["trades"][0]) == business_evidence(float("inf"))
    assert restored["trades"][1]["audit_evidence"]["invalid_fields"]["trade_price"] == {"invalid_number": "nan"}
    assert business_evidence(restored["trades"][1]["extra"]["commission"]) == business_evidence(float("-inf"))
    assert business_evidence(restored["trades"][2]["value_conversion"]) == business_evidence(float("inf"))
    assert business_evidence(restored["symbol_results"]["broken"]["trades"]) == business_evidence(float("-inf"))
    # 表中 JSON 均合法，非有限值由字段级带外元数据保留。
    for values in batch.values():
        json.dumps(values, allow_nan=False)


def test_imprecise_fee_never_publishes_a_different_native_numeric_value() -> None:
    native_fee = 2**53 + 1
    restored, batch = _roundtrip({"trades": [_fill(extra={"commission": native_fee})]}, database=True)
    assert business_evidence(restored["trades"][0]["extra"]["commission"]) == business_evidence(native_fee)
    assert batch[trade][0]["fee"] is None


def test_missing_typed_trade_facts_expose_corruption() -> None:
    payload = {"trades": [_fill(value_conversion={"rate": 7.1})]}
    batch = encode_trades(7, payload)
    loaded = {table.name: rows for table, rows in batch.items()}
    with pytest.raises(ValueError, match="Missing trade conversion facts"):
        decode_trades({**loaded, trade_conversion.name: []})
    with pytest.raises(ValueError, match="Incomplete trade collection"):
        decode_trades({**loaded, trade.name: []})
