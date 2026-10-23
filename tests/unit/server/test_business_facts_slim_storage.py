"""以真实原生业务事实验证精简存储，不要求恢复旧 JSON 表示。"""

import json
import sqlite3
from contextlib import closing

import pytest

from axile.server._native import _core
from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_result,
    rows,
    seed_account,
    sql,
)


def _input(path, input_id="input-1"):
    return native_store_call(path, "read_execution_input", {"input_id": input_id})


def test_columns_store_facts_without_a_second_json_description(native_database):
    first = persist_result(native_database, {"orders": [{"price": 2, "volume": 0.0}]})
    other = persist_result(native_database, {"orders": [{"volume": 0, "price": 2.0}]})
    stored = rows(native_database, "execution_order")
    assert [(row["price"], row["volume"]) for row in stored] == [(2, 0), (2, 0)]
    assert all(row["audit_evidence"] == {} for row in stored)
    for record in (first, other):
        restored = read_result(native_database, record)["orders"][0]
        assert {key: restored[key] for key in ("price", "volume")} == {"price": 2, "volume": 0}


@pytest.mark.parametrize("bad", ["bad", float("nan"), float("inf"), 2**1000 + 1])
def test_invalid_amount_is_auditable_and_never_becomes_zero(native_database, bad):
    record = persist_result(native_database, {"account_assets": {"available_cash": bad}})
    row = rows(native_database, "business_asset_snapshot")[0]
    assert row["available_cash"] is None
    assert "available_cash" in row["audit_evidence"]["invalid_fields"]
    json.dumps(row["audit_evidence"], allow_nan=False)
    restored = read_result(native_database, record)["account_assets"]
    assert "available_cash" not in restored
    assert restored["availability"]["cash"] == "invalid"


def test_target_aliases_do_not_create_duplicate_facts(native_database):
    freeze_input(
        native_database,
        {
            "curr_target": {"A": 0},
            "account_target": {"A": 0.0},
            "normalized_weights": {"A": 0},
            "strategy_target": {"A": 0.3},
            "raw_weights": {"A": 0.3},
            "last_target": {"A": -0.2},
        },
    )
    targets = rows(native_database, "execution_input_target")
    assert len(targets) == 3
    assert {(row["role"], row["map_key"], row["value"]) for row in targets} == {
        ("curr_target", "A", 0),
        ("strategy_target", "A", 0.3),
        ("last_target", "A", -0.2),
    }
    with closing(sqlite3.connect(native_database)) as connection:
        columns = connection.execute("PRAGMA table_info(execution_input_target)").fetchall()
        indexes = connection.execute("PRAGMA index_list(execution_input_target)").fetchall()
    assert "ordinal" not in {row[1] for row in columns}
    assert not [index for index in indexes if index[3] != "pk"]
    assert not [index for index in indexes if index[3] == "u"]


@pytest.mark.parametrize(
    "target,state,present",
    [("missing", "absent", False), (None, "null", True), ({}, "value", True), ({"A": 0}, "value", True)],
)
def test_target_missing_null_empty_and_zero_have_distinct_meaning(native_database, target, state, present):
    payload = {} if target == "missing" else {"curr_target": target}
    freeze_input(native_database, payload)
    assert rows(native_database, "execution_input")[0]["curr_target_state"] == state
    view = _input(native_database)
    assert ("curr_target" in view) is present
    if present:
        assert view["curr_target"] == target


def test_missing_target_children_raise_instead_of_becoming_empty_target(native_database):
    freeze_input(native_database, {"curr_target": {"A": 1}})
    sql(native_database, "DELETE FROM execution_input_target")
    with pytest.raises(_core.NativeError, match="(?i)incomplete frozen target"):
        _input(native_database)


def test_quantity_and_contract_units_are_separate_frozen_business_facts(native_database):
    freeze_input(native_database, {"target_quantities": {"A": 0, "B": None}, "target_units": {"A": 2}})
    view = _input(native_database)
    assert view["plan"]["quantities"] == {"A": 0, "B": None}
    assert view["plan"]["units"] == {"A": 2}
    assert view["plan"]["availability"]["B"] == {"quantity": "null", "units": "absent"}
    assert len(rows(native_database, "execution_input_plan")) == 2
    sql(native_database, "DELETE FROM execution_input_plan")
    with pytest.raises(_core.NativeError, match="(?i)incomplete frozen quantity plan"):
        _input(native_database)


@pytest.mark.parametrize("native_id", [0, "0", "", None])
def test_native_order_identity_is_not_coerced(native_database, native_id):
    record = persist_result(native_database, {"orders": [{"order_id": native_id}]})
    restored = read_result(native_database, record)["orders"][0]
    assert restored["order_id"] == native_id
    assert type(restored["order_id"]) is type(native_id)


def test_missing_positions_and_cancellation_children_expose_incomplete_facts(native_database):
    record = persist_result(native_database, {"account_assets": {"positions": [{"symbol": "A", "volume": 0}]}})
    sql(native_database, "DELETE FROM business_asset_position")
    with pytest.raises(_core.NativeError, match="(?i)incomplete position facts"):
        read_result(native_database, record)
    record = persist_result(native_database, {"termination": {"cancel_failed_order_ids": [0, None]}})
    sql(native_database, "DELETE FROM execution_termination_cancel_failure")
    with pytest.raises(_core.NativeError, match="(?i)incomplete collection facts"):
        read_result(native_database, record)


@pytest.mark.parametrize(
    "payload,state",
    [
        ({}, "absent"),
        ({"available_cash": None}, "null"),
        ({"available_cash": 0}, "value"),
        ({"available_cash": "bad"}, "invalid"),
    ],
)
def test_cash_observation_preserves_missing_null_zero_and_invalid_evidence(native_database, payload, state):
    record = persist_result(native_database, {"account_assets": payload})
    row = rows(native_database, "business_asset_snapshot")[0]
    view = read_result(native_database, record)["account_assets"]
    assert row["cash_state"] == view["availability"]["cash"] == state
    if state == "value":
        assert view["available_cash"] == 0
    elif state == "null":
        assert view["available_cash"] is None
    elif state == "invalid":
        assert "available_cash" not in view
        assert view["audit_evidence"]["invalid_fields"]["available_cash"] == "bad"
    else:
        assert "available_cash" not in view


@pytest.mark.parametrize(
    "extra,state,fee",
    [
        ({}, "absent", None),
        ({"commission": None}, "null", None),
        ({"commission": 0}, "value", 0),
        ({"commission": -1}, "value", -1),
    ],
)
def test_fee_availability_and_actual_zero_are_not_guessed(native_database, extra, state, fee):
    persist_result(native_database, {"trades": [{"trade_id": "0", "extra": extra}]})
    row = rows(native_database, "business_trade")[0]
    assert row["fee_state"] == state
    assert row["fee"] == fee
    assert row["extra"] == extra


def test_summary_reference_preserves_distinct_evidence_without_field_projection_metadata(native_database):
    fact = {"status": "SUCCEEDED", "orders": [{"order_id": "0", "price": 2}], "sizing": {"target_quantity": 0}}
    result = {"symbol_results": {"A": fact}}
    content = {
        "symbol_results": {"A": {**fact, "sizing": {"target_quantity": 3}}},
        "independent": {"null": None, "zero": 0},
    }
    execution_id = "slim-summary"
    native_store_call(
        native_database,
        "append_execution_artifact",
        {
            "execution_id": execution_id,
            "artifact_type": "execution_summary",
            "schema_version": 1,
            "content": content,
            "created_at": "2026-10-20",
        },
    )
    record = persist_result(native_database, result, execution_id=execution_id)
    compact = rows(native_database, "execution_artifact")[0]["content"]
    assert compact["business_facts_ref"] == {
        "version": 1,
        "view": "result",
        "record_id": record["id"],
        "projection_version": 2,
        "legacy_schema_version": 1,
    }
    assert "orders" not in compact["symbol_results"]["A"]
    assert compact["symbol_results"]["A"]["sizing"] == {"target_quantity": 3}
    hydrated = native_store_call(native_database, "list_execution_artifacts", {"execution_id": execution_id})["data"][
        0
    ]["content"]
    hydrated.pop("business_facts_ref")
    assert business_evidence(hydrated) == business_evidence(content)


@pytest.mark.parametrize("projection", [None, 1, True, 2.0, "2"])
def test_summary_reference_rejects_invalid_projection_version(native_database, projection):
    execution_id = "invalid-summary"
    record = persist_result(native_database, {"symbol_results": {}}, execution_id=execution_id)
    native_store_call(
        native_database,
        "append_execution_artifact",
        {
            "execution_id": execution_id,
            "artifact_type": "execution_summary",
            "schema_version": 1,
            "created_at": "2026-10-20",
            "content": {
                "symbol_results": {},
                "business_facts_ref": {
                    "version": 1,
                    "view": "result",
                    "record_id": record["id"],
                    "projection_version": projection,
                },
            },
        },
    )
    with pytest.raises(_core.NativeError, match="摘要业务视图版本无效") as error:
        native_store_call(native_database, "list_execution_artifacts", {"execution_id": execution_id})
    assert error.value.args[0] == "BUSINESS_FACTS_REFERENCE"


def test_all_business_tables_have_no_generic_field_metadata(native_database):
    with closing(sqlite3.connect(native_database)) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        facts = [
            name
            for name in names
            if name.startswith(
                ("execution_input", "execution_symbol", "execution_order", "execution_extension", "business_")
            )
            or name in {"executerecord", "execution_termination_cancel_failure"}
        ]
        assert facts
        for name in facts:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
            assert "field_metadata" not in columns
            assert "numeric_kinds" not in columns


def test_equal_audit_targets_share_one_frozen_input(monkeypatch, native_database):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    payload = {"curr_target": {"A": 0}, "last_target": {}, "strategy_target": {"A": 0.3}, "trade_rules": {}}
    receipt = freeze_input(
        native_database,
        payload,
        execution_id="one",
        audit_payload={**payload, "account_target": {"A": 0.0}},
        audit_input_id="audit-one",
    )
    assert receipt["input_id"] == receipt["audit_id"]
    assert len(rows(native_database, "execution_input")) == 1
    assert len(rows(native_database, "execution_input_target")) == 2


def test_explicit_invalid_input_state_survives_business_view_reencoding(native_database):
    freeze_input(native_database, {"curr_target": ["invalid"]})
    first = _input(native_database)
    assert first["curr_target"] is None
    freeze_input(native_database, first, input_id="second", execution_id="second")
    row = next(row for row in rows(native_database, "execution_input") if row["id"] == "second")
    assert row["curr_target_state"] == "invalid"
    assert row["audit_evidence"]["invalid_collections"]["curr_target"] == ["invalid"]


def test_invalid_cash_state_survives_adoption_roundtrip(native_database):
    first = persist_result(native_database, {"account_assets": {"available_cash": "not-observed"}})
    result = read_result(native_database, first)
    second = read_result(native_database, persist_result(native_database, result))
    assert second["account_assets"]["availability"]["cash"] == "invalid"
    assert second["account_assets"]["audit_evidence"]["invalid_fields"]["available_cash"] == "not-observed"
