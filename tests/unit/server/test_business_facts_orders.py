"""订单事实保留来源、字段存在性及原生快照，不产生默认值。"""

import json
from copy import deepcopy

import pytest

from axile.server._native import _core
from scripts.business_facts_contract import business_evidence
from tests.sqlite_oracle import to_us
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows, sql

_TABLES = ("execution_order_collection", "execution_order")


def _sql_roundtrip(path, payload):
    record = persist_result(path, payload)
    return read_result(path, record), {name: rows(path, name) for name in _TABLES}


def _snapshot(**changes):
    result = {
        "order_id": "native-1",
        "symbol": "SHFE.au2606",
        "direction": "BUY",
        "order_type": "LIMIT",
        "volume": 2,
        "price": 612.25,
        "status": "PARTIALLY_FILLED",
        "filled_volume": 1.0,
        "avg_price": 612.0,
        "create_time": "2026-10-19T21:00:01.123456+08:00",
        "update_time": "2026-10-19T21:00:02.999999+08:00",
        "update_timestamp": 1792414802.999999,
        "remaining_volume": 1,
        "filled_ratio": 0.5,
        "value_conversion": {
            "price_currency": "CNY",
            "value_currency": "USD",
            "rate": 0.14,
            "reference": {"source": "frozen", "rate": 0, "observed_at": ""},
        },
        "extra": {
            "ExchangeID": "SHFE",
            "TradingDay": "20261020",
            "OrderSysID": "",
            "offset_flag": "3",
            "raw_order_data": {"VolumeTotalOriginal": 2},
        },
    }
    result.update(changes)
    return result


def test_orders_sql_roundtrip_preserves_each_origin_and_snapshot(native_database):
    original = _snapshot()
    payload = {
        "orders": [original, _snapshot(status="CANCELED"), _snapshot(order_id="")],
        "symbol_results": {
            "alias/~key": {
                "orders": [deepcopy(original), _snapshot(order_id="", symbol="SHFE.ag2612")],
            },
            "second": {"orders": []},
        },
    }

    result, loaded = _sql_roundtrip(native_database, payload)

    assert business_evidence(result) == business_evidence(payload)
    assert business_evidence(len(loaded["execution_order"])) == business_evidence(5)
    assert business_evidence(
        [row["ordinal"] for row in loaded["execution_order"] if row["path"] == "/orders"]
    ) == business_evidence([0, 1, 2])
    nested = [row for row in loaded["execution_order"] if row["map_key"] == "alias/~key"]
    assert business_evidence(nested[0]["path"]) == business_evidence("/symbol_results/alias~1~0key/orders")
    assert business_evidence(nested[0]["symbol"]) == business_evidence("SHFE.au2606")
    assert result["orders"][0]["volume"] == payload["orders"][0]["volume"]
    assert type(result["orders"][0]["filled_volume"]) is float


@pytest.mark.parametrize("value", [None, [], "unknown", {}, [None, False, 0, "unknown", [], {}]])
def test_orders_preserve_container_and_member_shapes(native_database, value):
    payload = {"orders": value, "symbol_results": {"": {"orders": value}}}

    result, _ = _sql_roundtrip(native_database, payload)

    assert business_evidence(result) == business_evidence(payload)


def test_orders_missing_does_not_manufacture_empty_collection_or_defaults(native_database):
    result, loaded = _sql_roundtrip(native_database, {"symbol_results": {"valid": {}, "bad": None}, "unrelated": 1})

    assert business_evidence(result) == business_evidence(
        {"symbol_results": {"valid": {}, "bad": None}, "unrelated": 1}
    )
    assert "orders" not in result
    assert not loaded["execution_order_collection"]
    assert not loaded["execution_order"]
    empty_result, _ = _sql_roundtrip(native_database, {"orders": [{}]})
    assert business_evidence(empty_result) == business_evidence({"orders": [{}]})


def test_orders_keep_explicit_missing_null_zero_empty_and_invalid_known_fields(native_database):
    snapshot = {
        "order_id": "",
        "symbol": None,
        "volume": "12",
        "price": 0,
        "filled_volume": None,
        "avg_price": False,
        "create_time": "",
        "update_time": "unparseable native time",
        "remaining_volume": -1,
        "filled_ratio": 1.25,
        "extra": {},
        "native_unknown": {"zero": 0, "flag": False, "empty": []},
        "value_conversion": {"price_currency": "", "rate": "0.14", "reference": None, "unknown": False},
    }

    result, loaded = _sql_roundtrip(native_database, {"orders": [snapshot]})

    assert business_evidence(result) == business_evidence({"orders": [snapshot]})
    row = loaded["execution_order"][0]
    assert business_evidence(row["price"]) == business_evidence(0)
    assert row["volume"] is None
    assert row["avg_price"] is None
    assert row["conversion_rate"] is None
    assert row["audit_evidence"]["explicit_null_fields"] == ["symbol", "filled_volume"]
    assert row["conversion_evidence"]["explicit_null_fields"] == ["reference"]
    assert result["orders"][0]["symbol"] is None
    assert result["orders"][0]["filled_volume"] is None
    assert result["orders"][0]["value_conversion"]["reference"] is None
    assert "direction" not in result["orders"][0]


@pytest.mark.parametrize("conversion", [None, [], "missing native evidence", {}, {"rate": 0}])
def test_orders_conversion_scope_preserves_historical_shape(native_database, conversion):
    payload = {"orders": [{"order_id": "one", "value_conversion": conversion}]}

    result, _ = _sql_roundtrip(native_database, payload)

    assert business_evidence(result) == business_evidence(payload)


def test_orders_valid_stable_fields_live_in_columns_and_read_column_updates(native_database):
    snapshot = _snapshot()
    record = persist_result(native_database, {"orders": [snapshot]})
    row = rows(native_database, "execution_order")[0]

    assert (
        set(snapshot).difference({"value_conversion", "create_time", "update_time", "update_timestamp"}) <= row.keys()
    )
    assert row["create_time_us"] == to_us(snapshot["create_time"])
    assert row["update_time_us"] == to_us(snapshot["update_time"])
    assert row["update_timestamp_us"] == round(snapshot["update_timestamp"] * 1_000_000)
    assert business_evidence(row["order_id"]) == business_evidence(snapshot["order_id"])
    assert business_evidence(row["price"]) == business_evidence(snapshot["price"])
    assert business_evidence(row["conversion_rate"]) == business_evidence(0.14)
    assert business_evidence(row["extra"]) == business_evidence(snapshot["extra"])
    assert business_evidence(row["conversion_reference"]) == business_evidence(
        snapshot["value_conversion"]["reference"]
    )
    assert row["invalid_value"] is None
    assert row["conversion_invalid_value"] is None
    sql(native_database, "UPDATE execution_order SET price=613.75 WHERE record_id=?", (record["id"],))
    assert read_result(native_database, record)["orders"][0]["price"] == 613.75


def test_orders_incomplete_typed_collection_fails_instead_of_using_hidden_payload(native_database):
    record = persist_result(native_database, {"orders": [_snapshot(), _snapshot(order_id="second")]})
    sql(native_database, "DELETE FROM execution_order WHERE record_id=? AND ordinal=1", (record["id"],))

    with pytest.raises(_core.NativeError, match="incomplete"):
        read_result(native_database, record)


def test_orders_broken_parent_link_fails(native_database):
    record = persist_result(native_database, {"orders": [_snapshot()]})
    other = persist_result(native_database, {})
    # 移到有效属主，保留孤立订单，避免删除触发器同时清理子行。
    sql(
        native_database,
        "UPDATE execution_order_collection SET record_id=? WHERE record_id=?",
        (other["id"], record["id"]),
    )
    assert rows(native_database, "execution_order")

    with pytest.raises(_core.NativeError, match="orphan order facts"):
        read_result(native_database, record)


def test_orders_encode_does_not_change_live_or_migration_payload(native_database):
    payload = {"orders": [_snapshot()], "symbol_results": {"native": {"orders": [None, {}]}}}
    before = deepcopy(payload)

    persist_result(native_database, payload)

    assert business_evidence(payload) == business_evidence(before)


def test_orders_nonfinite_historical_evidence_uses_valid_json_metadata(native_database):
    payload = {
        "orders": [
            float("nan"),
            {"price": float("inf"), "value_conversion": float("-inf")},
            {"extra": {"native": float("nan")}, "value_conversion": {"reference": {"rate": float("inf")}}},
        ],
        "symbol_results": {"invalid": {"orders": float("inf")}},
    }

    result, loaded = _sql_roundtrip(native_database, payload)

    assert result["orders"][0] == {"invalid_number": "nan"}
    assert result["orders"][1]["audit_evidence"]["invalid_fields"]["price"] == {"invalid_number": "inf"}
    assert business_evidence(result["orders"][1]["value_conversion"]) == business_evidence(float("-inf"))
    assert result["orders"][2]["extra"]["native"] == {"invalid_number": "nan"}
    assert business_evidence(result["orders"][2]["value_conversion"]["reference"]["rate"]) == business_evidence(
        float("inf")
    )
    assert business_evidence(result["symbol_results"]["invalid"]["orders"]) == business_evidence(float("inf"))
    for values in loaded.values():
        for row in values:
            json.dumps(dict(row), allow_nan=False)


def test_orders_deleting_execution_cascades_all_occurrences(native_database):
    record = persist_result(native_database, {"orders": [_snapshot()], "symbol_results": {"native": {"orders": [{}]}}})
    assert all(rows(native_database, name) for name in _TABLES)

    sql(native_database, "DELETE FROM executerecord WHERE id=?", (record["id"],))

    assert all(not rows(native_database, name) for name in _TABLES)
