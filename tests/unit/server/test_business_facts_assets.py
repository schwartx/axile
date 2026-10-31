"""资产事实的身份、空值、可信度及选择性读取回归。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest

from axile.server._native import _core
from axile.server.core import db
from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_before_assets,
    read_result,
    rows,
    sql,
)
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._runtime_db_support import to_us, utc_time


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    for account_id in (1, 2):
        receipt = native_store_call(
            native_database,
            "account_create",
            {"now": "2026-10-20", "account": build_account(id=account_id, portfolio_id=None).model_dump(mode="json")},
        )
        assert receipt["account"]["id"] == account_id
    return native_database


def _asset_view(result):
    view = {"account_assets": result["account_assets"]} if "account_assets" in result else {}
    if "symbol_results" in result:
        view["symbol_results"] = {
            key: {"account_assets": item["account_assets"]}
            for key, item in result["symbol_results"].items()
            if isinstance(item, dict) and "account_assets" in item
        }
    return view


def _roundtrip(database, payload):
    record = persist_result(database, payload)
    loaded = {
        name: [row for row in rows(database, name) if row.get("record_id") == record["id"]]
        for name in ("business_asset_snapshot", "business_asset_binding")
    }
    identities = {row["asset_id"] for row in loaded["business_asset_snapshot"]}
    loaded["business_asset_position"] = [
        row for row in rows(database, "business_asset_position") if row["asset_id"] in identities
    ]
    return _asset_view(read_result(database, record)), loaded


def _after(database, payload, *, execution_id="execution-1"):
    return native_store_call(
        database,
        "freeze_execution_after_assets",
        {"account_id": 1, "execution_id": execution_id, "payload": payload},
    )


def _manual(database, assets):
    return SimpleNamespace(
        **native_store_call(
            database, "asset_snapshot_write", {"account_id": 1, "created_at": "2026-10-20", "account_assets": assets}
        )
    )


def _corrupt(database, statement, parameters=()):
    # 历史损坏夹具跳过当前写入触发器，读取仍经过真实原生完整性检查。
    with closing(oracle_connect(database)) as connection, connection:
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
        connection.execute(statement, parameters)


def _totals(database):
    page = json.loads(_core.journal(_core.Store(str(database)), 1, 0, 100))
    return {row["record"]["id"]: row["record"]["total_asset"] for row in page["data"] if row["kind"] == "execution"}


@pytest.mark.parametrize(
    "value", [None, 0, "missing", [], {}, {"positions": None}, {"positions": {}}, {"positions": []}]
)
def test_asset_container_and_positions_presence_roundtrip(database, value):
    payload = {"account_assets": value}
    restored, _ = _roundtrip(database, payload)
    assert business_evidence(restored) == business_evidence(payload)


def test_position_identity_and_native_channel_fields_are_not_merged(database):
    payload = {
        "account_assets": {
            "positions": [
                {
                    "symbol": "SHFE.rb2610",
                    "direction": "多头",
                    "volume": 2,
                    "available_volume": 0.0,
                    "market_value": None,
                    "avg_price": None,
                    "extra": {"position_attributes": {"today_volume": 2, "unknown_native": {"v": 0}}},
                },
                {"symbol": "SHFE.rb2610", "direction": "空头", "volume": 1.0},
                None,
                "legacy-position-error",
            ],
            "available_cash": 0,
            "market_value": None,
            "total_asset": 0,
            "source": "ctp-native",
            "update_time": "2026-10-19T21:00:00.123456789+08:00",
        }
    }
    restored, loaded = _roundtrip(database, payload)
    assert business_evidence(restored) == business_evidence(payload)
    assert restored["account_assets"]["positions"][0]["volume"] == payload["account_assets"]["positions"][0]["volume"]
    assert type(restored["account_assets"]["positions"][1]["volume"]) is float
    assert business_evidence([row["ordinal"] for row in loaded["business_asset_position"]]) == business_evidence(
        [0, 1, 2, 3]
    )
    assert loaded["business_asset_snapshot"][0]["observation_valid"]
    assert "realized_profit" not in restored["account_assets"]["positions"][0]["extra"]["position_attributes"]


@pytest.mark.parametrize(
    "source,valid", [("real", True), ("gm", True), ("assumed", False), ("error", False), ("unavailable", False)]
)
def test_real_zero_is_distinct_from_unavailable(database, source, valid):
    restored, loaded = _roundtrip(database, {"account_assets": {"total_asset": 0, "source": source}})
    assert business_evidence(restored["account_assets"]["total_asset"]) == business_evidence(0)
    assert loaded["business_asset_snapshot"][0]["observation_valid"] is valid


def test_exact_legacy_placeholder_is_frozen_without_dropping_evidence(database):
    payload = {
        "account_assets": {
            "available_cash": 0,
            "total_asset": 0,
            "market_value": 0,
            "positions": [],
            "extra": {},
            "source": "real",
        },
        "status": "FAILED",
        "extra": {"worker_error": {"type": "ValueError"}},
        "inputs": None,
        "execution_time": 0,
        "symbol_results": {},
    }
    restored, loaded = _roundtrip(database, payload)
    assert loaded["business_asset_snapshot"][0]["observation_valid"] is False
    assert business_evidence(restored["account_assets"]) == business_evidence(payload["account_assets"])
    changed = deepcopy(payload)
    changed["extra"] = {}
    _, real = _roundtrip(database, changed)
    assert real["business_asset_snapshot"][0]["observation_valid"] is True


def test_symbol_map_key_path_and_embedded_position_symbol_are_separate(database):
    payload = {
        "account_assets": {"total_asset": 10},
        "symbol_results": {
            "XAU/USD~hedge": {
                "account_assets": {"total_asset": 9, "positions": [{"symbol": "native-symbol", "volume": 1}]}
            },
            "": {"account_assets": None},
        },
    }
    restored, loaded = _roundtrip(database, payload)
    assert business_evidence(restored) == business_evidence(payload)
    paths = [row["path"] for row in loaded["business_asset_snapshot"]]
    assert business_evidence(paths) == business_evidence(
        [
            "/account_assets",
            "/symbol_results/XAU~1USD~0hedge/account_assets",
            "/symbol_results//account_assets",
        ]
    )
    assert business_evidence(
        len(set(row["asset_id"] for row in loaded["business_asset_snapshot"]))
    ) == business_evidence(3)


def test_worker_after_observation_binds_without_duplicate_snapshot(database):
    before = {"account_assets": {"total_asset": 100, "update_time": "before", "positions": []}}
    after = {"account_assets": {"total_asset": 0, "update_time": "after", "positions": [{"symbol": "A", "volume": 0}]}}
    freeze_input(database, {}, input_id="pre-order-input", before_assets=before["account_assets"])
    receipt = _after(database, after)
    assert business_evidence(read_before_assets(database, "pre-order-input")) == business_evidence(
        before["account_assets"]
    )
    observations = rows(database, "business_asset_snapshot")
    assert {row["path"] for row in observations} == {"/before_account_assets", "/after_account_assets"}
    assert {row["input_id"] for row in observations} == {"pre-order-input"}
    linked_id = receipt["asset_id"]
    assert linked_id == "input:pre-order-input:/after_account_assets"
    record = persist_result(database, after, execution_id="execution-1", created_at="2026-10-20T09:00:00")
    assert rows(database, "business_asset_binding")[0]["asset_id"] == linked_id
    assert business_evidence(_asset_view(read_result(database, record))) == business_evidence(after)
    assert business_evidence(_totals(database)) == business_evidence({record["id"]: 0})
    assert len(rows(database, "business_asset_snapshot")) == 2
    assert len(rows(database, "business_asset_position")) == 1
    with pytest.raises(_core.NativeError, match="不一致"):
        _after(database, before)
    changed_representation = deepcopy(after)
    changed_representation["account_assets"]["total_asset"] = 0.0
    assert _after(database, changed_representation)["asset_id"] == linked_id
    _corrupt(database, "UPDATE business_asset_snapshot SET audit_evidence='invalid-json'")
    assert _totals(database) == {record["id"]: 0}


@pytest.mark.parametrize("mode", ["other_account", "other_execution", "before"])
def test_result_binding_and_scalar_read_reject_foreign_observation(database, mode):
    payload = {"account_assets": {"total_asset": 42}}
    freeze_input(database, {}, input_id="local", execution_id="exec-local", before_assets=payload["account_assets"])
    if mode == "before":
        actual_id = "input:local:/before_account_assets"
    else:
        account_id = 2 if mode == "other_account" else 1
        freeze_input(database, {}, input_id="foreign", execution_id="exec-foreign", account_id=account_id)
        receipt = native_store_call(
            database,
            "freeze_execution_after_assets",
            {"account_id": account_id, "execution_id": "exec-foreign", "payload": payload},
        )
        actual_id = receipt["asset_id"]
    record = persist_result(database, payload, execution_id="exec-local", created_at="2026-10-20T09:00:00")
    _corrupt(
        database,
        "UPDATE business_asset_binding SET asset_id=? WHERE record_id=? AND path='/account_assets'",
        (actual_id, record["id"]),
    )
    with pytest.raises(_core.NativeError, match="资产|[Aa]sset"):
        read_result(database, record)
    with pytest.raises(_core.NativeError, match="资产|[Aa]sset"):
        _totals(database)


def test_account_snapshot_reference_rejects_another_manual_snapshot(database):
    first = _manual(database, {"total_asset": 1})
    other = _manual(database, {"total_asset": 99})
    _corrupt(database, "UPDATE account_asset_snapshot SET asset_fact_id=? WHERE id=?", (other.asset_fact_id, first.id))
    with pytest.raises(_core.NativeError):
        _core.asset_snapshots(_core.Store(str(database)), 1, 0, 100)


def test_after_snapshot_matching_preserves_nonfinite_diagnostic_and_negative_zero(database):
    freeze_input(database, {}, input_id="input-nonfinite")
    after = {"account_assets": {"total_asset": 0, "extra": {"diagnostic": float("nan"), "native": -0.0}}}
    assert _after(database, after)["asset_id"] == "input:input-nonfinite:/after_account_assets"
    assert _after(database, deepcopy(after))["asset_id"] == "input:input-nonfinite:/after_account_assets"
    changed = deepcopy(after)
    changed["account_assets"]["extra"]["native"] = 0.0
    assert _after(database, changed)["asset_id"] == "input:input-nonfinite:/after_account_assets"
    changed["account_assets"]["extra"]["native"] = 1
    with pytest.raises(_core.NativeError, match="不一致"):
        _after(database, changed)


def test_manual_snapshot_page_preserves_facts_and_rejects_incomplete_positions(database):
    snapshots = [
        _manual(database, {"total_asset": key, "positions": [{"symbol": "A", "volume": key}]}) for key in (5, 6)
    ]
    payloads = {
        snapshot.id: {"total_asset": key, "positions": [{"symbol": "A", "volume": key}]}
        for snapshot, key in zip(snapshots, (5, 6), strict=True)
    }
    page = json.loads(_core.asset_snapshots(_core.Store(str(database)), 1, 0, 100))
    assert business_evidence({row["id"]: row["assets"] for row in page["data"]}) == business_evidence(payloads)
    _corrupt(database, "DELETE FROM business_asset_position")
    with pytest.raises(_core.NativeError, match="[Ii]ncomplete position facts"):
        _core.asset_snapshots(_core.Store(str(database)), 1, 0, 100)


@pytest.mark.parametrize("input_owned", [False, True])
@pytest.mark.parametrize("execution_id", [None, "execution-history-17"])
@pytest.mark.parametrize("source", ["real", "unavailable"])
def test_execution_asset_history_reuses_valid_canonical_root(database, input_owned, execution_id, source):
    payload = {
        "account_assets": {
            "available_cash": 0,
            "total_asset": 0,
            "market_value": None,
            "source": source,
            "positions": [],
        }
    }
    created_at = "2026-10-20T01:02:03.123456+08:00"
    terminal_execution_id = execution_id if execution_id is not None else "history-owner" if input_owned else None
    if input_owned:
        freeze_input(database, {}, input_id="history-input", execution_id=terminal_execution_id)
        _after(database, payload, execution_id=terminal_execution_id)
    failure_point = (
        "AFTER INSERT ON account_asset_snapshot"
        if source == "real"
        else "BEFORE UPDATE OF facts_complete ON executerecord WHEN NEW.facts_complete=1"
    )
    sql(database, f"CREATE TRIGGER fail_terminal {failure_point} BEGIN SELECT RAISE(ABORT, 'history rollback'); END")
    with pytest.raises(_core.NativeError, match="history rollback"):
        persist_result(database, payload, execution_id=terminal_execution_id, created_at=created_at)
    assert rows(database, "account_asset_snapshot") == []
    assert rows(database, "executerecord") == []
    sql(database, "DROP TRIGGER fail_terminal")
    record = persist_result(database, payload, execution_id=terminal_execution_id, created_at=created_at)
    reference = "input:history-input:/after_account_assets" if input_owned else f"record:{record['id']}:/account_assets"
    if input_owned and execution_id is None:
        # 旧记录可无执行标识；事实仍由真实冻结和终态阶段生成。
        _corrupt(database, "UPDATE executerecord SET execution_id=NULL WHERE id=?", (record["id"],))
        _corrupt(
            database, "UPDATE execution_input SET execution_id=NULL,owner_execution_id=NULL WHERE id='history-input'"
        )
        _corrupt(database, "UPDATE account_asset_snapshot SET execution_id=NULL WHERE account_id=1")
    first = rows(database, "account_asset_snapshot")
    if execution_id is not None:
        assert persist_result(database, payload, execution_id=execution_id, created_at=created_at)["id"] == record["id"]
    second = rows(database, "account_asset_snapshot")
    assert first == second
    if source == "unavailable":
        assert first == []
        expected = 0
    else:
        assert len(first) == 1
        snapshot = SimpleNamespace(**first[0])
        assert snapshot.asset_fact_id == reference
        assert snapshot.source == "execution"
        assert snapshot.execution_id == execution_id
        assert snapshot.created_at_us == to_us(created_at)
        assert isinstance(snapshot.created_at_us, int)
        page = json.loads(_core.asset_snapshots(_core.Store(str(database)), 1, 0, 100))
        assert page["data"][0]["created_at"] == utc_time(created_at)
        assert business_evidence({row["id"]: row["assets"] for row in page["data"]}) == business_evidence(
            {snapshot.id: payload["account_assets"]}
        )
        expected = 1
    assert len(rows(database, "account_asset_snapshot")) == expected
    assert len(rows(database, "business_asset_snapshot")) == 1
