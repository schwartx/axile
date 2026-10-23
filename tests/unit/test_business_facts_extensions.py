"""动态扩展只拥有自身证据，异常历史根值具有显式状态。"""

import json

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_result,
    rows,
    seed_account,
    sql,
)

_DOMAIN_FIELDS = (
    "status",
    "symbol",
    "technical_detail",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
    "channel_type",
    "execution_time",
    "success",
    "termination",
    "inputs",
    "account_assets",
    "symbol_results",
    "orders",
    "trades",
    "first_ticks",
    "target_volume",
)


def test_result_extensions_exclude_all_owned_business_domains(native_database):
    payload = {name: {"must_not_duplicate": name} for name in _DOMAIN_FIELDS}
    payload.update({"memory": {"diagnostic": [1, None]}, "extra": {"channel": "custom"}, "unknown": False})
    record = persist_result(native_database, payload)
    row = rows(native_database, "execution_extension")[0]

    assert row["root_kind"] == "object"
    assert row["root_value"] is None
    assert "must_not_duplicate" not in json.dumps(row)
    assert row["audit_evidence"]["unrecognized_fields"] == {"unknown": False}
    restored = read_result(native_database, record)
    assert restored["memory"] == {"diagnostic": [1, None]}
    assert restored["extra"] == {"channel": "custom"}
    assert restored["audit_evidence"]["unrecognized_fields"]["unknown"] is False


@pytest.mark.parametrize(
    ("payload", "kind"),
    [(None, "null"), ([], "empty_list"), (0, "scalar"), (False, "scalar"), ("", "scalar"), (2.5, "scalar")],
)
def test_historical_non_object_root_does_not_become_empty_object(native_database, payload, kind):
    record = persist_result(native_database, payload)
    row = rows(native_database, "execution_extension")[0]
    assert row["root_kind"] == kind
    assert read_result(native_database, record) == payload
    assert row["memory"] is None and row["extra"] is None and row["reconciliation"] is None
    assert row["audit_evidence"] == {}


def test_historical_scalar_nonfinite_root_is_json_safe(tmp_path):
    path = tmp_path / "legacy-scalar.db"
    initialize_native_database(path, revision="0036")
    seed_account(path)
    sql(
        path,
        "INSERT INTO executerecord (account_id,is_success,created_at,raw_input,raw_result) VALUES (1,0,'2026-10-20','null','NaN')",
    )

    initialize_native_database(path)

    row = rows(path, "execution_extension")[0]
    json.dumps(row, allow_nan=False)
    assert row["root_kind"] == "scalar"
    assert row["root_value"] == {"invalid_number": "nan"}
    assert read_result(path, {"id": row["record_id"]}) == {"invalid_number": "nan"}


def test_literal_invalid_number_mapping_remains_an_object_in_every_scope(native_database):
    literal = {"invalid_number": "nan"}
    persist_result(
        native_database,
        {
            **literal,
            "orders": [literal, {"value_conversion": literal}],
            "trades": [literal, {"value_conversion": literal}],
        },
    )
    freeze_input(native_database, literal)

    assert rows(native_database, "execution_extension")[0]["root_kind"] == "object"
    assert rows(native_database, "execution_input")[0]["root_kind"] == "object"
    orders = rows(native_database, "execution_order")
    assert orders[0]["kind"] == "object"
    assert orders[0]["audit_evidence"]["unrecognized_fields"] == literal
    assert orders[1]["conversion_kind"] == "object"
    assert rows(native_database, "business_trade")[0]["state"] == "object"
    assert rows(native_database, "business_trade_conversion")[0]["state"] == "object"


@pytest.mark.parametrize("payload", [[{"orders": []}], [1], object()])
def test_unmappable_root_fails_before_hiding_a_whole_document(native_database, payload):
    expected = TypeError if type(payload) is object else _core.NativeError
    match = "not JSON serializable" if expected is TypeError else "Unsupported historical result root"
    with pytest.raises(expected, match=match):
        persist_result(native_database, payload)
    assert not rows(native_database, "executerecord")
    assert not rows(native_database, "execution_extension")


def test_missing_new_root_row_is_visible_corruption(native_database):
    record = persist_result(native_database, {})
    sql(native_database, "DELETE FROM execution_extension WHERE record_id=?", (record["id"],))
    with pytest.raises(_core.NativeError, match="exactly one fact"):
        read_result(native_database, record)


def test_empty_mapping_and_no_extensions_are_still_object(native_database):
    record = persist_result(native_database, {})
    row = rows(native_database, "execution_extension")[0]
    assert (row["root_kind"], row["root_value"]) == ("object", None)
    assert read_result(native_database, record) == {"task_status": "FAILED"}
    assert row["audit_evidence"] == {}


@pytest.mark.parametrize(("kind", "value"), [("null", 4), ("empty_list", [1]), ("scalar", {})])
def test_root_state_cannot_silently_disagree_with_value(native_database, kind, value):
    record = persist_result(native_database, value if kind != "empty_list" else [])
    sql(
        native_database,
        "UPDATE execution_extension SET root_kind=?,root_value=?,audit_evidence='{}' WHERE record_id=?",
        (kind, json.dumps(value), record["id"]),
    )
    with pytest.raises(_core.NativeError, match="disagrees with value"):
        read_result(native_database, record)
