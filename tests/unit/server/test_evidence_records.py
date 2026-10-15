"""证据结果读取边界：合约上下文、原生身份范围及快照完整性。"""

from copy import deepcopy

import pytest

from axile.server.evidence.records import normalize_result_records


@pytest.mark.parametrize("identity,field", [("order_id", "orders"), ("trade_id", "trades")])
@pytest.mark.parametrize("symbol", [None, "", "  "])
def test_group_contract_and_json_pointer_are_independent_of_layout(identity, field, symbol):
    nested = {identity: "native-1", "symbol": symbol, "extra": {"order_sys_id": "native-order"}}
    top = {**nested, "symbol": "SHFE.rb2610"}
    result = {
        "symbol_results": {"alias/~rb": {"symbol": "SHFE.rb2610", field: [nested]}},
        field: [top],
    }
    before = deepcopy(result)
    orders, trades = normalize_result_records(result, "record.json#/raw_result")
    records = orders if field == "orders" else trades
    assert records.count == 1
    assert len(records.rows) == 1
    assert records.rows[0]["symbol"] == "SHFE.rb2610"
    assert records.rows[0]["source_path"] == f"record.json#/raw_result/symbol_results/alias~1~0rb/{field}/0"
    assert result == before


@pytest.mark.parametrize("identity,field", [("order_id", "orders"), ("trade_id", "trades")])
def test_record_contract_overrides_conflicting_group_names(identity, field):
    raw = {identity: "native-1", "symbol": "rb2610"}
    result = {
        "symbol_results": {
            "wrong-contract-1": {"symbol": "wrong-contract-2", field: [dict(raw)]},
            "wrong-contract-3": {field: [dict(raw)]},
        },
        field: [dict(raw)],
    }
    orders, trades = normalize_result_records(result, "record.json#/raw_result")
    records = orders if field == "orders" else trades
    assert records.count == len(records.rows) == 1
    assert records.rows[0]["symbol"] == "rb2610"


@pytest.mark.parametrize("identity,field", [("order_id", "orders"), ("trade_id", "trades")])
@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "hc2610"},
        {"extra": {"exchange_id": "DCE", "trading_day": "20261015"}},
        {"extra": {"exchange_id": "SHFE", "trading_day": "20261016"}},
    ],
    ids=["different-contract", "different-exchange", "different-trading-day"],
)
def test_native_ids_do_not_merge_across_record_scopes(identity, field, change):
    raw = {identity: "native-1", "symbol": "rb2610", "extra": {"exchange_id": "SHFE", "trading_day": "20261015"}}
    orders, trades = normalize_result_records({field: [raw, {**raw, **change}]}, "record.json#/raw_result")
    records = orders if field == "orders" else trades
    assert records.count == len(records.rows) == 2
    assert records.variants == []


def test_different_snapshots_keep_native_fields_and_ignore_export_metadata():
    rows = [
        {"trade_id": "t1", "symbol": "rb2610", "trade_price": 3500, "extra": {"order_sys_id": "native-order"}},
        {"trade_id": "t1", "symbol": "rb2610", "trade_price": 3501, "extra": {"order_sys_id": "native-order"}},
        {"trade_id": "t1", "symbol": "rb2610", "trade_price": 3502, "extra": {"order_sys_id": "native-order"}},
    ]
    duplicates = [
        {**row, "source_path": f"other-source/{index}", "source_record_id": "derived-id"}
        for index, row in enumerate(reversed(rows))
    ]
    _, trades = normalize_result_records({"trades": [*rows, *duplicates]}, "record.json#/raw_result")
    assert trades.count == 1
    assert [row["trade_price"] for row in trades.rows] == [3500, 3501, 3502]
    assert all(row["extra"]["order_sys_id"] == "native-order" for row in trades.rows)
    assert trades.variants[0]["snapshots"] == 3
    assert [row["source_path"] for row in trades.rows] == [f"record.json#/raw_result/trades/{i}" for i in range(3)]


@pytest.mark.parametrize("identity", [None, "", " ", False, [], {}])
def test_unusable_identity_does_not_discard_identical_observations(identity):
    raw = {"trade_id": identity, "symbol": "rb2610", "trade_price": 3500}
    _, trades = normalize_result_records({"trades": [dict(raw), dict(raw)]}, "record.json#/raw_result")
    assert trades.count == trades.unidentified == len(trades.rows) == 2
    assert trades.variants == []


def test_missing_contract_cannot_be_inferred_from_matching_native_id():
    raw = {"trade_id": "t1", "trade_price": 3500}
    result = {"symbol_results": {"rb2610": {"trades": [dict(raw)]}}, "trades": [dict(raw), dict(raw)]}
    _, trades = normalize_result_records(result, "record.json#/raw_result")
    assert trades.count == len(trades.rows) == 3
    assert trades.unidentified == 2
    assert [row["symbol"] for row in trades.rows] == ["rb2610", "", ""]


@pytest.mark.parametrize("native_id", [0, 17, "0"])
def test_numeric_native_ids_remain_native_source_record_ids(native_id):
    first = {"trade_id": native_id, "symbol": "rb2610", "trade_price": 3500}
    _, trades = normalize_result_records({"trades": [first, {**first, "trade_price": 3501}]}, "record.json#/raw_result")
    assert trades.count == 1
    assert len(trades.rows) == 2
    assert [row["source_record_id"] for row in trades.rows] == [native_id, native_id]


def test_order_and_trade_ids_have_separate_namespaces():
    orders, trades = normalize_result_records(
        {"symbol": "rb2610", "orders": [{"order_id": "1"}], "trades": [{"order_id": "1", "trade_id": "1"}]},
        "record.json#/raw_result",
    )
    assert orders.count == trades.count == 1
    assert orders.rows[0]["source_record_id"] == trades.rows[0]["source_record_id"] == "1"


def test_invalid_groups_and_entries_do_not_hide_valid_top_level_records():
    result = {
        "symbol_results": {"rb2610": None, "hc2610": {"orders": {}, "trades": "invalid"}},
        "orders": [None, "invalid", {"order_id": "o1", "symbol": "rb2610"}],
        "trades": [None, {"trade_id": "t1", "symbol": "rb2610"}],
    }
    orders, trades = normalize_result_records(result, "record.json#/raw_result")
    assert orders.count == trades.count == 1
    assert orders.rows[0]["source_path"].endswith("/orders/2")
    assert trades.rows[0]["source_path"].endswith("/trades/1")
