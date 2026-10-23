"""Historical CTP valuation migration integration tests."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import initialize_native_database, native_store_call, require_native_store


def test_repairs_joined_ctp_observations_and_restores_exact_originals(tmp_path: Path) -> None:
    path = tmp_path / "ctp-market-value.db"
    initialize_native_database(path, revision="0013")
    assets = {
        "market_value": 33,
        "total_asset": 500,
        "positions": [
            {"symbol": "rb", "volume": 2, "market_value": 33},
            {"symbol": "ag", "volume": 1, "market_value": 0},
        ],
    }
    result = {
        "account_assets": assets,
        "symbol_results": {
            "rb": {"first_tick": {"last_price": 100, "timestamp": 1780000000000}, "sizing": {"unit_multiplier": 10}}
        },
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO account (id, name, market, trade_channel, account_control_preset, account_config, "
            "is_started, cron_expr, brokerage, weight_precision, updated_at, created_at) "
            "VALUES (?, 'account', 'future', ?, 'default', '{}', 0, '', '', 0.001, 'now', 'now')",
            ((1, "ctp"), (2, "gm")),
        )
        connection.executemany(
            "INSERT INTO executerecord (id, account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (?, ?, ?, '{}', ?, 1, 'now')",
            ((1, 1, "ctp-1", json.dumps(result)), (2, 2, "gm-1", json.dumps(result))),
        )
        connection.executemany(
            "INSERT INTO account_asset_snapshot (id, account_id, execution_id, assets, source, created_at) "
            "VALUES (?, ?, ?, ?, 'execution', 'now')",
            ((1, 1, "ctp-1", json.dumps(assets)), (2, 2, "gm-1", json.dumps(assets))),
        )
        connection.executemany(
            "INSERT INTO execution_artifact (id, execution_id, artifact_type, schema_version, content, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'now')",
            (
                (1, "ctp-1", "ACCOUNT_SNAPSHOT", json.dumps({"account_assets": assets})),
                (3, "ctp-1", "ACCOUNT_SNAPSHOT_BEFORE", json.dumps({"account_assets": assets})),
                (2, "gm-1", "ACCOUNT_SNAPSHOT", json.dumps({"account_assets": assets})),
            ),
        )
    initialize_native_database(path, revision="0014")
    with closing(sqlite3.connect(path)) as connection:
        repaired = json.loads(
            connection.execute("SELECT assets FROM account_asset_snapshot WHERE id = 1").fetchone()[0]
        )
        assert repaired["positions"][0]["market_value"] == 2000
        assert repaired["positions"][1]["market_value"] is None
        assert repaired["market_value"] is None
        assert repaired["positions"][0]["extra"] == {
            "position_cost": 33,
            "market_value_provenance": {
                "source": "ctp_first_tick_last_price_0014",
                "quote_timestamp": 1780000000000,
            },
        }
        assert (
            json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id = 1").fetchone()[0])[
                "account_assets"
            ]
            == repaired
        )
        assert (
            json.loads(connection.execute("SELECT content FROM execution_artifact WHERE id = 1").fetchone()[0])[
                "account_assets"
            ]
            == repaired
        )
        assert (
            json.loads(connection.execute("SELECT content FROM execution_artifact WHERE id = 3").fetchone()[0])[
                "account_assets"
            ]
            == repaired
        )
        assert (
            json.loads(connection.execute("SELECT assets FROM account_asset_snapshot WHERE id = 2").fetchone()[0])
            == assets
        )
        assert (
            json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id = 2").fetchone()[0]) == result
        )
    native_store_call(path, "migration_downgrade", {"public": "0013"})
    with closing(sqlite3.connect(path)) as connection:
        assert (
            json.loads(connection.execute("SELECT assets FROM account_asset_snapshot WHERE id = 1").fetchone()[0])
            == assets
        )
        assert (
            json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id = 1").fetchone()[0]) == result
        )
        assert json.loads(connection.execute("SELECT content FROM execution_artifact WHERE id = 1").fetchone()[0]) == {
            "account_assets": assets
        }


def test_downgrade_refuses_external_edits(tmp_path: Path) -> None:
    core = require_native_store("migration_downgrade")
    path = tmp_path / "ctp-conflict.db"
    initialize_native_database(path, revision="0013")
    result = {
        "account_assets": {"positions": [{"symbol": "rb", "volume": 1, "market_value": 8}], "market_value": 8},
        "symbol_results": {
            "rb": {"first_tick": {"last_price": 100, "timestamp": 1780000000000}, "sizing": {"unit_multiplier": 10}}
        },
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, account_control_preset, account_config, "
            "is_started, cron_expr, brokerage, weight_precision, updated_at, created_at) "
            "VALUES (1, 'account', 'future', 'ctp', 'default', '{}', 0, '', '', 0.001, 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO executerecord (id, account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (1, 1, 'one', '{}', ?, 1, 'now')",
            (json.dumps(result),),
        )
    initialize_native_database(path, revision="0014")
    with closing(sqlite3.connect(path)) as connection, connection:
        changed = json.loads(connection.execute("SELECT raw_result FROM executerecord").fetchone()[0])
        changed["account_assets"]["total_asset"] = 999
        connection.execute("UPDATE executerecord SET raw_result = ?", (json.dumps(changed),))
    with pytest.raises(core.NativeError, match="restore conflict"):
        native_store_call(path, "migration_downgrade", {"public": "0013"})
    with closing(sqlite3.connect(path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "axile_0014_market_value_backup" in tables
        assert json.loads(connection.execute("SELECT raw_result FROM executerecord").fetchone()[0]) == changed
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0014"
