"""原生资产事务及 Python 阶段边界的定向回归。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from axile.server._native import _core
from axile.server.business_facts import assets
from axile.server.db.models.account_asset import AccountAssetSnapshot

_SCHEMA = """
CREATE TABLE account(id INTEGER PRIMARY KEY);
INSERT INTO account VALUES(1),(2);
CREATE TABLE execution_input(id TEXT PRIMARY KEY,account_id INTEGER,execution_id TEXT,owner_execution_id TEXT);
CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,input_id TEXT,
    result_input_id TEXT,created_at TEXT);
CREATE TABLE account_asset_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,asset_fact_id TEXT,
    source TEXT NOT NULL,execution_id TEXT UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE business_asset_snapshot(asset_id TEXT PRIMARY KEY,record_id INTEGER,input_id TEXT,
    account_snapshot_id INTEGER,path TEXT NOT NULL,scope TEXT NOT NULL,ordinal INTEGER NOT NULL,map_key TEXT,
    payload_kind TEXT NOT NULL,payload_value JSON,audit_evidence JSON NOT NULL,cash_state TEXT NOT NULL,
    equity_state TEXT NOT NULL,market_state TEXT NOT NULL,positions_kind TEXT NOT NULL,positions_count INTEGER,
    positions_value JSON,observation_valid INTEGER NOT NULL,available_cash REAL,total_asset REAL,
    market_value REAL,currency TEXT,update_time TEXT,source TEXT,extra JSON,
    CHECK ((record_id IS NOT NULL)+(input_id IS NOT NULL)+(account_snapshot_id IS NOT NULL)=1));
CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER,payload_kind TEXT NOT NULL,
    payload_value JSON,audit_evidence JSON NOT NULL,volume_state TEXT NOT NULL,
    available_volume_state TEXT NOT NULL,market_value_state TEXT NOT NULL,avg_price_state TEXT NOT NULL,
    symbol TEXT,volume REAL,available_volume REAL,market_value REAL,direction TEXT,avg_price REAL,extra JSON,
    PRIMARY KEY(asset_id,ordinal));
CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT NOT NULL,scope TEXT NOT NULL,
    ordinal INTEGER NOT NULL,map_key TEXT,PRIMARY KEY(record_id,path));
"""


@pytest.fixture
def asset_database(tmp_path):
    path = tmp_path / "assets.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_SCHEMA)
    return path


def _call(path, operation, request):
    return getattr(_core, operation)(str(path), json.dumps(request, ensure_ascii=False, allow_nan=False))


def _save(path, value, *, account_id=1, created_at="observed-time"):
    return _call(
        path,
        "asset_snapshot_write",
        {"account_id": account_id, "created_at": created_at, "account_assets": value},
    )


def test_manual_write_is_atomic_when_a_later_position_fails(asset_database):
    first = _save(asset_database, {"total_asset": 50, "positions": []})
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        connection.executescript(
            "CREATE TRIGGER position_failure BEFORE INSERT ON business_asset_position "
            "WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT,'position failed'); END;"
        )
    with pytest.raises(_core.NativeError):
        _save(asset_database, {"total_asset": 0, "positions": [{"symbol": "A"}, {"symbol": "B"}]})
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        for table in ("account_asset_snapshot", "business_asset_snapshot"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM business_asset_position").fetchone()[0] == 0
    latest = _call(asset_database, "asset_snapshot_latest", {"account_id": 1})
    assert latest["id"] == first["id"]
    assert latest["assets"]["total_asset"] == 50


def test_latest_trusted_filters_before_selection_and_preserves_evidence(asset_database):
    assert _call(asset_database, "asset_snapshot_latest", {"account_id": 1}) is None
    _save(asset_database, {"total_asset": 100}, created_at="newer-clock")
    zero = _save(
        asset_database,
        {
            "total_asset": 0,
            "available_cash": None,
            "market_value": "bad",
            "positions": [{"symbol": "A", "volume": 0, "available_volume": None, "avg_price": "bad"}],
        },
        created_at="older-clock",
    )
    for value in ({}, {"total_asset": None}, {"total_asset": True}, {"total_asset": "bad"}):
        _save(asset_database, value)
    _save(asset_database, {"total_asset": 1000, "source": "unavailable"})
    _save(asset_database, {"total_asset": 500}, account_id=2)
    latest = _call(asset_database, "asset_snapshot_latest", {"account_id": 1})
    assert latest["id"] == zero["id"]
    assert latest["created_at"] == "older-clock"
    assert latest["assets"]["total_asset"] == 0
    assert latest["assets"]["available_cash"] is None
    assert "market_value" not in latest["assets"]
    assert latest["assets"]["availability"] == {"cash": "null", "equity": "value", "market_value": "invalid"}
    position = latest["assets"]["positions"][0]
    assert position["available_volume"] is None
    assert position["audit_evidence"]["invalid_fields"]["avg_price"] == "bad"


def test_snapshot_read_rejects_foreign_manual_reference_and_incomplete_positions(asset_database):
    first = _save(asset_database, {"total_asset": 0, "positions": [{"symbol": "A", "volume": 1}]})
    other = _save(asset_database, {"total_asset": 100}, account_id=2)
    request = {"snapshots": [first], "include_positions": True}
    assert _call(asset_database, "asset_snapshots_read", request)[0]["assets"]["positions"][0]["volume"] == 1
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        connection.execute("DELETE FROM business_asset_position WHERE asset_id=?", (first["asset_fact_id"],))
    with pytest.raises(_core.NativeError):
        _call(asset_database, "asset_snapshots_read", request)
    request["include_positions"] = False
    assert "positions" not in _call(asset_database, "asset_snapshots_read", request)[0]["assets"]
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        connection.execute(
            "UPDATE account_asset_snapshot SET asset_fact_id=? WHERE id=?", (other["asset_fact_id"], first["id"])
        )
    first["asset_fact_id"] = other["asset_fact_id"]
    with pytest.raises(_core.NativeError):
        _call(asset_database, "asset_snapshots_read", request)


def test_input_observations_survive_terminal_delete(asset_database):
    before = _save(asset_database, {"total_asset": 100, "positions": None})
    after = _save(asset_database, {"total_asset": 0, "positions": [None, "bad"]})
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        connection.execute("INSERT INTO execution_input VALUES('i1',1,'e1','e1')")
        connection.execute("INSERT INTO executerecord VALUES(17,1,'e1','i1',NULL,'t')")
        for snapshot, path, scope in (
            (before, "/before_account_assets", "before"),
            (after, "/after_account_assets", "after_pending"),
        ):
            connection.execute(
                "UPDATE business_asset_snapshot SET account_snapshot_id=NULL,input_id='i1',path=?,scope=? "
                "WHERE asset_id=?",
                (path, scope, snapshot["asset_fact_id"]),
            )
        connection.execute("DELETE FROM account_asset_snapshot")
        connection.execute("DELETE FROM executerecord")
    observations = _call(asset_database, "asset_input_observations", {"input_id": "i1"})
    assert [row["scope"] for row in observations] == ["after_pending", "before"]
    assert observations[0]["account_assets"]["total_asset"] == 0
    assert observations[0]["account_assets"]["positions"] == [None, "bad"]
    assert observations[1]["account_assets"]["positions"] is None


def test_manual_facade_crosses_once_with_clock_and_invalid_number_evidence(monkeypatch):
    import axile.server.native_store as native_store

    requests = []

    async def operation(name, request):
        requests.append((name, request))
        return {
            "id": 7,
            "account_id": 1,
            "asset_fact_id": "snapshot:7:/account_assets",
            "source": "manual",
            "execution_id": None,
            "created_at": request["created_at"],
        }

    monkeypatch.setattr(native_store, "native_operation", operation)
    raw_assets = {"total_asset": float("inf")}
    raw_result = {"account_assets": raw_assets}
    saved = asyncio.run(assets.save_manual_asset_snapshot(1, raw_assets, created_at="clock-time", result=raw_result))
    assert isinstance(saved, AccountAssetSnapshot)
    assert saved.id == 7
    assert requests == [
        (
            "asset_snapshot_write",
            {
                "account_id": 1,
                "created_at": "clock-time",
                "account_assets": raw_assets,
                "result": raw_result,
            },
        )
    ]
    assert requests[0][1]["account_assets"] is raw_assets
    assert requests[0][1]["result"] is raw_result


def test_snapshot_facade_batches_identity_and_propagates_read_failure(monkeypatch):
    import axile.server.native_store as native_store

    requests = []

    async def operation(name, request):
        requests.append((name, request))
        return [{"id": 7, "assets": {"total_asset": 0}}]

    monkeypatch.setattr(native_store, "native_operation", operation)
    snapshot = SimpleNamespace(id=7, account_id=1, asset_fact_id="snapshot:7:/account_assets", execution_id=None)
    assert asyncio.run(assets.read_snapshots_assets(None, [snapshot], include_positions=False)) == {
        7: {"total_asset": 0}
    }
    assert requests == [
        (
            "asset_snapshots_read",
            {
                "snapshots": [
                    {"id": 7, "account_id": 1, "asset_fact_id": "snapshot:7:/account_assets", "execution_id": None}
                ],
                "include_positions": False,
            },
        )
    ]
    assert asyncio.run(assets.read_snapshots_assets(None, [])) == {}
    assert len(requests) == 1

    async def failure(*_args):
        raise ValueError("wrong owner")

    monkeypatch.setattr(native_store, "native_operation", failure)
    with pytest.raises(ValueError, match="wrong owner"):
        asyncio.run(assets.read_snapshot_assets(None, snapshot))


def test_asset_facade_preserves_value_error_for_corrupt_facts(monkeypatch):
    import axile.server.native_store as native_store

    async def failure(*_args):
        raise _core.NativeError("BUSINESS_FACTS", "wrong owner")

    monkeypatch.setattr(native_store, "native_operation", failure)
    with pytest.raises(ValueError, match="wrong owner"):
        asyncio.run(assets.latest_asset_snapshot(1))
