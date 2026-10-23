"""TQ historical market value repair tests."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import initialize_native_database, native_store_call, require_native_store


def test_repair_same_observation_quotes_and_guarded_restore(tmp_path: Path) -> None:
    core = require_native_store("migration_downgrade")
    path = tmp_path / "tq-market-value.db"
    initialize_native_database(path, revision="0014")
    assets = {
        "update_time": "2026-09-22T15:00:00",
        "total_asset": 500,
        "available_cash": 400,
        "market_value": 30,
        "positions": [
            {"symbol": "rb", "volume": 2, "market_value": 20, "extra": {"tq_symbol": "SHFE.rb"}},
            {"symbol": "ag", "volume": 1, "market_value": 10, "extra": {"tq_symbol": "SHFE.ag"}},
        ],
        "extra": {
            "quotes": {
                "SHFE.rb": {
                    "tq_symbol": "SHFE.rb",
                    "last_price": 100,
                    "volume_multiple": 10,
                    "datetime": "2026-09-22T14:59:00",
                },
                "SHFE.ag": {
                    "tq_symbol": "SHFE.ag",
                    "last_price": 200,
                    "volume_multiple": 10,
                    "datetime": "2026-09-22T14:50:00",
                },
            }
        },
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, account_control_preset, account_config, "
            "is_started, cron_expr, brokerage, weight_precision, updated_at, created_at) "
            "VALUES (1, 'account', 'future', 'tq', 'default', '{}', 0, '', '', 0.001, 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO executerecord (id, account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (1, 1, 'e1', '{}', ?, 1, ?)",
            (json.dumps({"account_assets": assets}), assets["update_time"]),
        )
        connection.execute(
            "INSERT INTO account_asset_snapshot (id, account_id, execution_id, assets, source, created_at) "
            "VALUES (1, 1, 'e1', ?, 'execution', ?)",
            (json.dumps(assets), assets["update_time"]),
        )
        connection.execute(
            "INSERT INTO execution_artifact (id, execution_id, artifact_type, schema_version, content, created_at) "
            "VALUES (1, 'e1', 'ACCOUNT_SNAPSHOT', 1, ?, ?)",
            (json.dumps({"account_assets": assets}), assets["update_time"]),
        )
    initialize_native_database(path, revision="0015")
    with closing(sqlite3.connect(path)) as connection, connection:
        repaired = json.loads(connection.execute("SELECT assets FROM account_asset_snapshot").fetchone()[0])
        assert repaired["positions"][0]["market_value"] == 2000
        assert repaired["positions"][1]["market_value"] is None
        assert repaired["market_value"] is None
        assert repaired["total_asset"] == 500
        assert repaired["available_cash"] == 400
        assert repaired["positions"][1]["extra"]["position_cost"] == 10
        assert (
            json.loads(connection.execute("SELECT raw_result FROM executerecord").fetchone()[0])["account_assets"]
            == repaired
        )
        assert (
            json.loads(connection.execute("SELECT content FROM execution_artifact").fetchone()[0])["account_assets"]
            == repaired
        )
        changed = {**repaired, "total_asset": 999}
        connection.execute("UPDATE account_asset_snapshot SET assets = ?", (json.dumps(changed),))
    with pytest.raises(core.NativeError, match="restore conflict"):
        native_store_call(path, "migration_downgrade", {"public": "0014"})
    with closing(sqlite3.connect(path)) as connection, connection:
        assert json.loads(connection.execute("SELECT assets FROM account_asset_snapshot").fetchone()[0]) == changed
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0015"
        connection.execute("UPDATE account_asset_snapshot SET assets = ?", (json.dumps(repaired),))
    native_store_call(path, "migration_downgrade", {"public": "0014"})
    with closing(sqlite3.connect(path)) as connection:
        assert json.loads(connection.execute("SELECT assets FROM account_asset_snapshot").fetchone()[0]) == assets


def test_repair_skips_new_provenance(tmp_path: Path) -> None:
    path = tmp_path / "tq-provenance.db"
    initialize_native_database(path, revision="0014")
    assets = {
        "positions": [
            {"volume": 1, "market_value": 100, "extra": {"market_value_provenance": {"source": "tq_last_price"}}}
        ]
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, account_control_preset, account_config, "
            "is_started, cron_expr, brokerage, weight_precision, updated_at, created_at) "
            "VALUES (1, 'account', 'future', 'tq', 'default', '{}', 0, '', '', 0.001, 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO executerecord (id, account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (1, 1, 'new-provenance', '{}', ?, 1, '2026-09-22T15:00:00')",
            (json.dumps({"account_assets": assets}),),
        )
    initialize_native_database(path, revision="0015")
    with closing(sqlite3.connect(path)) as connection:
        assert json.loads(connection.execute("SELECT raw_result FROM executerecord").fetchone()[0]) == {
            "account_assets": assets
        }
        assert connection.execute("SELECT COUNT(*) FROM axile_0015_tq_market_value_backup").fetchone()[0] == 0
