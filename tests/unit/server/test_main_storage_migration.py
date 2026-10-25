"""实际 main 的 0033 数据库是最终存储迁移的兼容基线。"""

import json
import sqlite3
from pathlib import Path

import pytest

from axile.server._native import _core
from tests.conftest import native_store_call


@pytest.fixture
def main_database(tmp_path):
    path = tmp_path / "main.db"
    source = Path(__file__).parents[2] / "fixtures/rust_store/main_0033.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(source.read_text())
    return path


def _dump(path):
    _core.close_database(str(path))
    with sqlite3.connect(path) as connection:
        return list(connection.iterdump())


def test_main_upgrade_downgrade_repeat_preserves_facts_and_complete_artifacts(main_database):
    with sqlite3.connect(main_database) as connection:
        artifacts = connection.execute("SELECT content FROM execution_artifact ORDER BY id").fetchall()
    for _ in range(2):
        assert native_store_call(main_database, "migration_upgrade", {})["public"] == "0041"
        detail = native_store_call(main_database, "read_execution_detail", {"account_id": 1, "record_id": 1})
        assert detail["input"]["extra"]["sentinel"] == 9007199254740993
        assert detail["result"]["account_assets"]["total_asset"] == 101.0
        with sqlite3.connect(main_database) as connection:
            assert connection.execute("SELECT content FROM execution_artifact ORDER BY id").fetchall() == artifacts
            assert not connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'analysis_source_%' "
                "OR name LIKE 'replay_observation%' OR name IN ('performance_cost_range','performance_execution_summary')"
            ).fetchall()
        assert native_store_call(main_database, "migration_downgrade", {"public": "0033"})["public"] == "0033"
        with sqlite3.connect(main_database) as connection:
            rows = connection.execute("SELECT raw_input,raw_result FROM executerecord ORDER BY id").fetchall()
            assert len(rows) == 3
            assert json.loads(rows[0][0])["extra"]["sentinel"] == 9007199254740993
            assert json.loads(rows[0][1])["extra"]["audit"] == "main-body"
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_main_conversion_failure_rolls_back_schema_rows_and_revision(main_database):
    with sqlite3.connect(main_database) as connection:
        connection.execute("UPDATE executerecord SET raw_input='invalid JSON' WHERE id=2")
    before = _dump(main_database)
    with pytest.raises(_core.NativeError):
        native_store_call(main_database, "migration_upgrade", {})
    assert _dump(main_database) == before


def test_old_cache_is_requeued_and_its_original_body_survives_downgrade(main_database):
    with sqlite3.connect(main_database) as connection:
        connection.execute("UPDATE performance_snapshot SET ranges='invalid JSON'")
    native_store_call(main_database, "migration_upgrade", {})
    result = json.loads(_core.snapshot(_core.Store(str(main_database)), 1, "all", "18", "equity-ledger-1"))
    assert result["status"] == "pending"
    assert result["result"] is None
    with sqlite3.connect(main_database) as connection:
        assert connection.execute(
            "SELECT requested,current_snapshot FROM account_analysis WHERE account_id=1"
        ).fetchone() == (1, None)
    native_store_call(main_database, "migration_downgrade", {"public": "0033"})
    with sqlite3.connect(main_database) as connection:
        assert connection.execute("SELECT ranges FROM performance_snapshot").fetchone() == ("invalid JSON",)


def test_new_publication_restores_main_readable_cache_on_downgrade(main_database):
    native_store_call(main_database, "migration_upgrade", {})
    claim = _core.queue_claim(str(main_database), 1792890000.0, "main-publish")
    assert claim is not None
    receipt = native_store_call(
        main_database,
        "publish_store",
        {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 1,
            "expected_source_version": claim["source_version"],
            "snapshot_id": "main-publish",
            "claim_token": "main-publish",
            "logic_version": "18",
            "engine_version": "equity-ledger-1",
            "computed_at": "2026-10-25T10:00:00",
            "trade_channel": "tq",
            "include_backtest": False,
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0},
        },
    )
    assert receipt["published"] is True
    native_store_call(main_database, "migration_downgrade", {"public": "0033"})
    with sqlite3.connect(main_database) as connection:
        ranges = json.loads(
            connection.execute("SELECT ranges FROM performance_snapshot WHERE id='main-publish'").fetchone()[0]
        )
        assert ranges["all"]["performance"]["points"][-1]["account_equity"] == 103.0
        assert len(ranges["all"]["performance"]["executions"]) == 3


@pytest.mark.parametrize("revision", ["0034", "0035", "0036", "0037", "0038", "0039", "0040"])
def test_unpublished_revisions_are_rejected_without_creating_a_database(tmp_path, revision):
    path = tmp_path / "missing.db"
    with pytest.raises(_core.NativeError):
        native_store_call(path, "migration_upgrade", {"public": revision})
    assert not path.exists()
