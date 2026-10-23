"""Dashboard native inputs preserve business facts, ordering and account isolation."""

import sqlite3
from contextlib import closing

import pytest

from axile.server._native import _core
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result
from tests.unit.server._execution_test_support import build_account, insert_account


def _snapshot(path, account_id, assets, *, created_at="2026-10-23T00:00:00"):
    return native_store_call(
        path,
        "asset_snapshot_write",
        {"account_id": account_id, "account_assets": assets, "created_at": created_at},
    )


def _target(path, portfolio_id, payload):
    return native_store_call(
        path,
        "target_operation",
        {
            "op": "append",
            "account_id": 1,
            "portfolio_id": portfolio_id,
            "source": "manual",
            "calculated_at": "2026-10-20",
            **payload,
        },
    )


@pytest.fixture
def dashboard_database(native_database):
    path = native_database
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at,updated_at) "
            "VALUES (?,'dashboard','期货','def calculate_portfolio(context): return {}','2026-10-20','2026-10-20')",
            [(7,), (8,)],
        )
        for account_id in (1, 2, 3):
            insert_account(connection, build_account(id=account_id))
        connection.executemany(
            "INSERT INTO portfolioaccount(id,account_id,portfolio_id,created_at) VALUES (?,?,?,'2026-10-20')",
            [(1, 1, 7), (2, 2, 7), (3, 2, None)],
        )
    snapshots = []
    for snapshot_id, account_id, assets in [
        (1, 1, {"total_asset": 100, "positions": []}),
        (
            2,
            1,
            {
                "total_asset": 0,
                "available_cash": None,
                "positions": [
                    {"symbol": "Z", "volume": 1, "market_value": "bad"},
                    {"symbol": "A", "volume": None, "market_value": -5},
                ],
            },
        ),
        (3, 1, {"total_asset": "invalid", "positions": []}),
        (4, 2, {"total_asset": 22, "positions": []}),
    ]:
        saved = _snapshot(path, account_id, assets, created_at=f"2026-10-{20 - snapshot_id:02d}T00:00:00")
        assert saved["id"] == snapshot_id
        if snapshot_id in (2, 4):
            snapshots.append(saved)
    expected = {
        row["id"]: row["assets"]
        for row in native_store_call(path, "asset_snapshots_read", {"snapshots": snapshots, "include_positions": True})
    }
    for portfolio_id, payload in [
        (7, {"normalized_weights": {"old": 1}}),
        (7, {"normalized_weights": {}}),
        (7, {"normalized_weights": None}),
        (7, {}),
        (8, {"normalized_weights": {"wrong": 1}}),
    ]:
        _target(path, portfolio_id, payload)
    for identity in (1, 2):
        saved = persist_result(
            path,
            {"status": "SUCCEEDED"},
            raw_input={},
            execution_id=f"e{identity}",
            is_success=1,
            created_at=f"2026-10-{20 - identity:02d}T00:00:00",
        )
        assert saved["id"] == identity
    return path, expected


def inputs(path, ids=(1, 2, 3)):
    return _core.dashboard_inputs(_core.Store(str(path)), list(ids), LOGIC_VERSION, ENGINE_VERSION)


def test_dashboard_inputs_selection_and_asset_decoder_parity(dashboard_database):
    path, expected = dashboard_database
    assert inputs(path, ()) == {}
    result = inputs(path)
    assert set(result) == {"1", "2", "3"}
    assert result["1"]["observation"]["assets"] == expected[2]
    assert result["2"]["observation"]["assets"] == expected[4]
    assert expected[2]["availability"] == {"cash": "null", "equity": "value", "market_value": "absent"}
    assert [position["symbol"] for position in expected[2]["positions"]] == ["Z", "A"]
    assert expected[2]["positions"][0]["audit_evidence"]["invalid_fields"] == {"market_value": "bad"}
    assert result["1"]["latest"]["created_at"] == "2026-10-18T00:00:00"
    assert result["1"]["target_weights"] == {}
    assert result["1"]["portfolio_id"] == 7
    assert result["2"]["portfolio_id"] is None
    assert result["2"]["target_weights"] is None
    assert result["3"]["observation"] is None
    assert set(inputs(path, (2,))) == {"2"}


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE account_asset_snapshot SET asset_fact_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=4) WHERE id=2",
        "DELETE FROM business_asset_position WHERE asset_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=2) AND ordinal=0",
        "UPDATE business_asset_position SET ordinal=5 WHERE asset_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=2) AND ordinal=0",
    ],
)
def test_dashboard_rejects_corrupt_asset_facts(dashboard_database, mutation):
    path, _ = dashboard_database
    with closing(sqlite3.connect(path)) as connection, connection:
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='business_facts_0037_snapshot_asset_owner_update'"
        ).fetchone()[0]
        # 历史损坏绕过一次写入守卫，恢复守卫后验证原生读取仍拒绝越权。
        connection.execute("DROP TRIGGER business_facts_0037_snapshot_asset_owner_update")
        connection.execute(mutation)
        connection.execute(guard)
    with pytest.raises(_core.NativeError, match="INVALID_DATA"):
        inputs(path)


@pytest.mark.parametrize("positions", [None, [], "invalid", [None, 7, {"volume": "bad", "market_value": None}]])
def test_dashboard_position_container_parity(dashboard_database, positions):
    path, _ = dashboard_database
    saved = _snapshot(path, 1, {"total_asset": 0, "positions": positions})
    expected = native_store_call(path, "asset_snapshots_read", {"snapshots": [saved], "include_positions": True})[0][
        "assets"
    ]
    assert inputs(path)["1"]["observation"]["assets"] == expected
    if not isinstance(positions, list):
        assert expected["positions"] == positions
    elif positions:
        assert expected["positions"][:2] == [None, 7]
        assert expected["positions"][2]["market_value"] is None
        assert expected["positions"][2]["audit_evidence"]["invalid_fields"] == {"volume": "bad"}
    else:
        assert expected["positions"] == []


@pytest.mark.parametrize("value", [[], "invalid", 0, {"X": "bad"}])
def test_dashboard_invalid_target_recovery(dashboard_database, value):
    path, _ = dashboard_database
    _target(path, 7, {"normalized_weights": value})
    assert inputs(path)["1"]["target_weights"] == ({"X": None} if isinstance(value, dict) else value)
