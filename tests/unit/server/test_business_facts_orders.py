"""订单事实保留来源、字段存在性及原生快照，不产生默认值。"""

import json
from copy import deepcopy

import pytest
import sqlalchemy as sa

from axile.server.business_facts.orders import (
    ORDER_FIELDS,
    TABLES,
    decode_orders,
    encode_orders,
    order,
    order_collection,
)
from axile.server.db.models.execution import ExecuteRecord  # noqa: F401
from scripts.business_facts_contract import business_evidence


def _sql_roundtrip(payload):
    engine = sa.create_engine("sqlite://")
    batch = encode_orders(19, payload)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE executerecord (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO executerecord (id) VALUES (19)")
        for table in TABLES:
            table.create(connection)
            if batch[table]:
                connection.execute(table.insert(), batch[table])
        loaded = {table.name: connection.execute(sa.select(table)).mappings().all() for table in TABLES}
    engine.dispose()
    return decode_orders(loaded), loaded


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


def test_orders_sql_roundtrip_preserves_each_origin_and_snapshot():
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

    result, loaded = _sql_roundtrip(payload)

    assert business_evidence(result) == business_evidence(payload)
    assert business_evidence(len(loaded[order.name])) == business_evidence(5)
    assert business_evidence(
        [row["ordinal"] for row in loaded[order.name] if row["path"] == "/orders"]
    ) == business_evidence([0, 1, 2])
    nested = [row for row in loaded[order.name] if row["map_key"] == "alias/~key"]
    assert business_evidence(nested[0]["path"]) == business_evidence("/symbol_results/alias~1~0key/orders")
    assert business_evidence(nested[0]["symbol"]) == business_evidence("SHFE.au2606")
    assert result["orders"][0]["volume"] == payload["orders"][0]["volume"]
    assert type(result["orders"][0]["filled_volume"]) is float


@pytest.mark.parametrize("value", [None, [], "unknown", {}, [None, False, 0, "unknown", [], {}]])
def test_orders_preserve_container_and_member_shapes(value):
    payload = {"orders": value, "symbol_results": {"": {"orders": value}}}

    result, _ = _sql_roundtrip(payload)

    assert business_evidence(result) == business_evidence(payload)


def test_orders_missing_does_not_manufacture_empty_collection_or_defaults():
    result, loaded = _sql_roundtrip({"symbol_results": {"valid": {}, "bad": None}, "unrelated": 1})

    assert business_evidence(result) == business_evidence({})
    assert not loaded[order_collection.name]
    assert not loaded[order.name]
    empty_result, _ = _sql_roundtrip({"orders": [{}]})
    assert business_evidence(empty_result) == business_evidence({"orders": [{}]})


def test_orders_keep_explicit_missing_null_zero_empty_and_invalid_known_fields():
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

    result, loaded = _sql_roundtrip({"orders": [snapshot]})

    assert business_evidence(result) == business_evidence({"orders": [snapshot]})
    row = loaded[order.name][0]
    assert business_evidence(row["price"]) == business_evidence(0)
    assert row["volume"] is None
    assert row["avg_price"] is None
    assert row["conversion_rate"] is None
    assert "direction" not in result["orders"][0]


@pytest.mark.parametrize("conversion", [None, [], "missing native evidence", {}, {"rate": 0}])
def test_orders_conversion_scope_preserves_historical_shape(conversion):
    payload = {"orders": [{"order_id": "one", "value_conversion": conversion}]}

    result, _ = _sql_roundtrip(payload)

    assert business_evidence(result) == business_evidence(payload)


def test_orders_valid_stable_fields_live_in_columns_and_read_column_updates():
    snapshot = _snapshot()
    batch = encode_orders(19, {"orders": [snapshot]})
    row = batch[order][0]

    assert all(name in row for name in ORDER_FIELDS)
    assert business_evidence(row["order_id"]) == business_evidence(snapshot["order_id"])
    assert business_evidence(row["price"]) == business_evidence(snapshot["price"])
    assert business_evidence(row["conversion_rate"]) == business_evidence(0.14)
    assert business_evidence(row["extra"]) == business_evidence(snapshot["extra"])
    assert business_evidence(row["conversion_reference"]) == business_evidence(
        snapshot["value_conversion"]["reference"]
    )
    assert row["invalid_value"] is None
    assert row["conversion_invalid_value"] is None
    row["price"] = 613.75
    decoded = decode_orders({table.name: values for table, values in batch.items()})
    assert business_evidence(decoded["orders"][0]["price"]) == business_evidence(613.75)


def test_orders_incomplete_typed_collection_fails_instead_of_using_hidden_payload():
    batch = encode_orders(19, {"orders": [_snapshot(), _snapshot(order_id="second")]})
    batch[order].pop()

    with pytest.raises(ValueError, match="成员不完整"):
        decode_orders({table.name: values for table, values in batch.items()})


def test_orders_broken_parent_link_fails():
    batch = encode_orders(19, {"orders": [_snapshot()]})
    batch[order_collection].clear()

    with pytest.raises(ValueError, match="缺少所属集合"):
        decode_orders({table.name: values for table, values in batch.items()})


def test_orders_encode_does_not_change_live_or_migration_payload():
    payload = {"orders": [_snapshot()], "symbol_results": {"native": {"orders": [None, {}]}}}
    before = deepcopy(payload)

    encode_orders(19, payload)

    assert business_evidence(payload) == business_evidence(before)


def test_orders_nonfinite_historical_evidence_uses_valid_json_metadata():
    payload = {
        "orders": [
            float("nan"),
            {"price": float("inf"), "value_conversion": float("-inf")},
            {"extra": {"native": float("nan")}, "value_conversion": {"reference": {"rate": float("inf")}}},
        ],
        "symbol_results": {"invalid": {"orders": float("inf")}},
    }

    result, loaded = _sql_roundtrip(payload)

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


def test_orders_deleting_execution_cascades_all_occurrences():
    batch = encode_orders(19, {"orders": [_snapshot()], "symbol_results": {"native": {"orders": [{}]}}})
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE executerecord (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO executerecord (id) VALUES (19)")
        for table in TABLES:
            table.create(connection)
            connection.execute(table.insert(), batch[table])
        connection.exec_driver_sql("DELETE FROM executerecord WHERE id=19")
        assert all(connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0 for table in TABLES)
    engine.dispose()
