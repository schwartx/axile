"""品种事实在迁移和实时写入中保持原始证据。"""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

import pytest

from axile.server._native import _core
from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows, sql


def _roundtrip(path, payload):
    record = persist_result(path, payload)
    return read_result(path, record)


def test_fixed_sql_binds_input_json_and_symbol_text_separately(native_database):
    record = persist_result(
        native_database,
        {"symbol_results": {"A": {"algorithm": "twap"}}},
        raw_input={"algorithm": "twap"},
    )
    with closing(sqlite3.connect(native_database)) as connection:
        stored = connection.execute(
            "SELECT i.algorithm,s.algorithm,i.extra,i.channel_type IS NULL "
            "FROM executerecord e JOIN execution_input i ON i.id=e.input_id "
            "JOIN execution_symbol_result s ON s.record_id=e.id WHERE e.id=?",
            (record["id"],),
        ).fetchone()
    assert stored == ('"twap"', "twap", "null", 1)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbol_results": None, "first_ticks": [], "target_volume": "invalid"},
        {"symbol_results": {}, "first_ticks": {}, "target_volume": {}},
        {"symbol_results": {"": None, "alias/~XAU": [], "A": {}, "B": {"first_tick": None, "sizing": None}}},
        {
            "symbol_results": {
                "map/~B": {
                    "symbol": "native.B",
                    "status": "PARTIAL",
                    "outcome": "not_reached",
                    "target_volume": 0,
                    "final_volume": None,
                    "error": "",
                    "success": False,
                    "memory": {"channel": {"flags": []}},
                    "sizing": {
                        "symbol": "sizing.B",
                        "sizing_mode": "lots",
                        "status": "UNAVAILABLE",
                        "reason_code": "CTP.MISSING_MULTIPLIER",
                        "account_weight": 0.0,
                        "equity": None,
                        "unit_multiplier": None,
                        "target_quantity": -2,
                        "reference_price": "not-numeric",
                        "value_conversion": {
                            "price_currency": "HKD",
                            "value_currency": "CNY",
                            "rate": 0.92,
                            "reference": {"observed_at": "2026-10-19T21:00:00+08:00"},
                        },
                    },
                    "first_tick": {
                        "symbol": "tick.B",
                        "last_price": 0,
                        "bid_price": None,
                        "ask_price": 0.0,
                        "bid_price_5": 1.5,
                        "ask_volume_5": 0,
                        "timestamp": 0,
                        "update_time": "",
                        "timestamp_datetime": "1970-01-01T08:00:00",
                        "book_valid": False,
                        "extra": {"trading_day": "20261020", "event_date": "20261019", "volume_multiple": 10},
                    },
                    "unknown_symbol_metadata": {"enabled": False},
                },
                "A": {"target_volume": "2", "status": 0, "first_tick": {}, "sizing": {"value_conversion": []}},
            },
            "first_ticks": {"map/~B": {"last_price": 20, "timestamp": 42}, "A": None},
            "target_volume": {"map/~B": 17.0, "A": False, "": None, "bad": []},
        },
    ],
)
def test_symbol_roundtrip_preserves_sparse_and_malformed_evidence(native_database, payload):
    actual = _roundtrip(native_database, payload)
    assert business_evidence(actual) == business_evidence(payload)
    if isinstance(payload.get("symbol_results"), Mapping):
        assert business_evidence(list(actual["symbol_results"])) == business_evidence(list(payload["symbol_results"]))
    if "target_volume" in payload and isinstance(payload["target_volume"], Mapping):
        for key, value in payload["target_volume"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert actual["target_volume"][key] == value
            else:
                assert actual["target_volume"][key] is None


def test_explicit_null_symbol_fields_keep_presence_without_inventing_missing_fields(native_database):
    payload = {
        "symbol_results": {
            "null": {"error": None, "sizing": {"reason_code": None}, "first_tick": {"bid_price": None}},
            "missing": {"sizing": {}, "first_tick": {}},
        }
    }
    actual = _roundtrip(native_database, payload)["symbol_results"]
    for table, nested, field in (
        ("execution_symbol_result", None, "error"),
        ("execution_symbol_sizing", "sizing", "reason_code"),
        ("execution_symbol_tick", "first_tick", "bid_price"),
    ):
        null = actual["null"] if nested is None else actual["null"][nested]
        missing = actual["missing"] if nested is None else actual["missing"][nested]
        assert null[field] is None
        assert field not in missing
        stored = {row["map_key"]: row["audit_evidence"] for row in rows(native_database, table)}
        assert stored["null"]["explicit_null_fields"] == [field]
        assert "explicit_null_fields" not in stored["missing"]


def test_symbol_encoder_excludes_delegated_arrays_and_keeps_native_identity(native_database):
    payload = {
        "symbol_results": {
            "alias/~B": {
                "symbol": "native.B",
                "orders": [{"order_id": "same"}],
                "trades": [{"trade_id": "same"}],
                "account_assets": {"positions": [{"symbol": "native.B", "volume": 2}]},
                "first_tick": {"last_price": 1},
                "sizing": {"target_quantity": 2},
            }
        }
    }
    record = persist_result(native_database, payload)
    row = rows(native_database, "execution_symbol_result")[0]
    assert business_evidence(row["path"]) == business_evidence("/symbol_results/alias~1~0B")
    assert business_evidence(row["map_key"]) == business_evidence("alias/~B")
    assert business_evidence(row["symbol"]) == business_evidence("native.B")
    assert "orders" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert "trades" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert "account_assets" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert business_evidence(rows(native_database, "execution_symbol_tick")[0]["last_price"]) == business_evidence(1)
    assert business_evidence(
        rows(native_database, "execution_symbol_sizing")[0]["target_quantity"]
    ) == business_evidence(2)
    assert business_evidence(read_result(native_database, record)) == business_evidence(payload)


def test_roundtrip_keeps_every_sizing_and_five_level_tick_field(native_database):
    fixture = Path(__file__).parents[3] / "rust/storage/src/business_facts_ops/testdata/head_symbols_inputs_v0037.json"
    case = next(
        case for case in json.loads(fixture.read_text())["cases"] if case["name"] == "all-sizing-five-level-tick"
    )
    native = case["payload"]["symbol_results"]["Z/~"]
    payload = {"symbol_results": {"A": {"sizing": native["sizing"], "first_tick": native["first_tick"]}}}

    assert business_evidence(_roundtrip(native_database, payload)) == business_evidence(payload)
    assert set(native["first_tick"]) <= rows(native_database, "execution_symbol_tick")[0].keys()
    assert (
        set(native["sizing"]).difference({"value_conversion"})
        <= rows(native_database, "execution_symbol_sizing")[0].keys()
    )


def test_delegated_fragments_merge_without_field_order_metadata(native_database):
    source = {"symbol_results": {"A": {"status": "NOOP", "orders": [], "trades": []}}}
    restored = _roundtrip(native_database, source)
    assert business_evidence(restored) == business_evidence(source)
    assert rows(native_database, "execution_symbol_result")[0]["audit_evidence"] == {}


@pytest.mark.parametrize(
    "missing_table", ["execution_symbol_result", "execution_symbol_sizing", "execution_symbol_tick"]
)
def test_missing_typed_facts_raise_instead_of_silently_omitting_evidence(native_database, missing_table):
    record = persist_result(native_database, {"symbol_results": {"A": {"sizing": {}, "first_tick": {}}}})
    sql(native_database, f'DELETE FROM "{missing_table}" WHERE record_id=?', (record["id"],))
    with pytest.raises(_core.NativeError, match="missing|incomplete"):
        read_result(native_database, record)


def test_sql_roundtrip_and_narrow_readers_preserve_order_and_do_not_load_arrays(native_database):
    payload = {
        "symbol_results": {
            "Z": {"status": "PARTIAL", "first_tick": {"timestamp": 0}, "sizing": {"target_quantity": 0}},
            "alias/A": {
                "outcome": "blocked",
                "target_volume": -2,
                "memory": {"large": "opaque", "unknown": float("inf")},
            },
            "malformed": None,
            "completed": {"status": "NOOP"},
            "explicit": {"status": "PARTIAL", "outcome": "completed"},
        },
        "target_volume": {"Z": 1, "alias/A": 1.0},
    }
    record = persist_result(native_database, payload, execution_id="narrow-read")
    assert business_evidence(read_result(native_database, record)) == business_evidence(payload)
    status = native_store_call(native_database, "execution_read", {"op": "status", "execution_id": "narrow-read"})
    assert status["outcome_symbols"] == ["Z", "alias/A"]
    assert status["record_id"] == record["id"]
    sizing = native_store_call(
        native_database, "execution_read", {"op": "target_sizing", "account_id": 1, "execution_id": "narrow-read"}
    )
    assert sizing["sizings"] == {"Z": {"target_quantity": 0}}
    with closing(sqlite3.connect(native_database)) as connection:
        typed = connection.execute(
            "SELECT map_key,target_volume,target_volume_state FROM execution_symbol_result "
            "WHERE record_id=? AND node_kind='object' ORDER BY ordinal",
            (record["id"],),
        ).fetchall()
    assert typed == [
        ("Z", None, "absent"),
        ("alias/A", -2, "value"),
        ("completed", None, "absent"),
        ("explicit", None, "absent"),
    ]
    assert native_store_call(native_database, "execution_read", {"op": "status", "execution_id": "missing"}) is None
    with pytest.raises(_core.NativeError, match="未知执行读取阶段"):
        native_store_call(native_database, "execution_read", {"op": "orders", "record_id": record["id"]})
