"""已发布 0033 直接迁到当前 0038，失败不破坏业务事实或旧缓存。"""

import json
from contextlib import closing
from pathlib import Path

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.fixtures.rust_store.schema_contract import load_schema
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._native_read_views import read_view

_FIXTURE = Path(__file__).parents[2] / "fixtures/rust_store/v0.22.0.sql"
_INPUT = {"curr_target": {"A": 0.5}, "extra": {"zero": 0, "flag": False, "missing": None}}
_RESULT = {"status": "SUCCEEDED", "success": True, "symbol_results": {}, "extra": {"evidence": [0, None, False]}}


@pytest.fixture
def old_database(tmp_path):
    path = tmp_path / "released-0033.db"
    with closing(oracle_connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0").fetchone()
        with connection:
            load_schema(connection, _FIXTURE.read_text())
            connection.execute("INSERT INTO alembic_version VALUES('0033')")

            connection.execute(
                "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(1,'synthetic','FUTURES','ctp','','now','now')"
            )
            connection.execute(
                "INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at,raw_input,raw_result) VALUES(7,1,'original',1,'2026-10-01',?,?)",
                (json.dumps(_INPUT), json.dumps(_RESULT)),
            )
            connection.execute(
                "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES('original','EXECUTION_SUMMARY',2,?,'now')",
                (json.dumps(_RESULT),),
            )
            connection.execute(
                "INSERT INTO performance_snapshot(id,account_id,source_version,logic_version,engine_version,computed_at,settings,ranges) VALUES('old',1,3,'old','old','then','{}','malformed historical cache')"
            )
            connection.execute(
                "UPDATE account_analysis SET source_version=3,published_version=3,current_snapshot='old',running_version=3,failures=4,error='old failure' WHERE account_id=1"
            )
        assert Path(str(path) + "-wal").stat().st_size > 0
        yield path


def _state(path):
    with closing(oracle_connect(path)) as connection:
        return (
            connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
            connection.execute("SELECT * FROM executerecord").fetchall(),
            connection.execute("SELECT * FROM account_analysis").fetchall(),
            connection.execute("SELECT * FROM performance_snapshot").fetchall(),
        )


def test_direct_upgrade_preserves_business_data_backs_up_wal_and_requeues_cache(old_database):
    receipt = initialize_native_database(old_database)
    assert receipt["public"] == "0038"
    backup = Path(receipt["backup"])
    assert backup.is_file()
    with closing(oracle_connect(backup)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0033",)
        assert (
            json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id=7").fetchone()[0]) == _RESULT
        )
        assert (
            connection.execute("SELECT ranges FROM performance_snapshot").fetchone()[0] == "malformed historical cache"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    detail = read_view(old_database, "read_execution_detail", {"account_id": 1, "record_id": 7})
    assert detail["input"]["curr_target"] == _INPUT["curr_target"]
    assert detail["result"]["extra"] == _RESULT["extra"]
    with closing(oracle_connect(old_database)) as connection:
        assert connection.execute(
            "SELECT execution_id,account_id,is_success FROM executerecord WHERE id=7"
        ).fetchone() == ("original", 1, 1)
        assert connection.execute("SELECT COUNT(*) FROM execution_artifact").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM performance_snapshot").fetchone() == (0,)
        assert connection.execute(
            "SELECT current_snapshot,previous_snapshot,running_version,running_token,requested,failures,error FROM account_analysis"
        ).fetchone() == (None, None, None, None, 1, 0, None)
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'analysis_source_%' OR name LIKE 'replay_observation%'"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert "backup" not in initialize_native_database(old_database)


def test_downgrade_restores_business_documents_and_can_upgrade_again(old_database):
    initialize_native_database(old_database)
    native_store_call(old_database, "migration_downgrade", {"public": "0033"})
    with closing(oracle_connect(old_database)) as connection:
        row = connection.execute("SELECT raw_input,raw_result FROM executerecord WHERE id=7").fetchone()
        assert json.loads(row[0])["extra"] == _INPUT["extra"]
        assert json.loads(row[1]) == _RESULT | {"task_status": "SUCCEEDED"}
        assert connection.execute("SELECT COUNT(*) FROM execution_artifact").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM performance_snapshot").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert initialize_native_database(old_database)["public"] == "0038"


@pytest.mark.parametrize("failure", ["invalid_document", "stamp"])
def test_failed_upgrade_restores_schema_business_data_and_old_cache(old_database, failure):
    with closing(oracle_connect(old_database)) as connection, connection:
        if failure == "invalid_document":
            connection.execute("UPDATE executerecord SET raw_result='not-json' WHERE id=7")
        else:
            connection.execute(
                "CREATE TRIGGER reject_final_revision BEFORE UPDATE ON alembic_version WHEN NEW.version_num='0034' BEGIN SELECT RAISE(ABORT,'stamp rejected'); END"
            )
    before = _state(old_database)
    with pytest.raises(_core.NativeError):
        initialize_native_database(old_database)
    assert _state(old_database) == before
    assert native_store_call(old_database, "migration_current", {})["public"] == "0033"


def test_pending_facts_block_lossy_downgrade_before_any_change(old_database):
    initialize_native_database(old_database)
    native_store_call(
        old_database,
        "freeze_execution_input",
        {
            "account_id": 1,
            "execution_id": "pending",
            "input_id": "pending",
            "created_at": "now",
            "payload": {"curr_target": {"A": 1}},
        },
    )
    before = _state(old_database)
    with pytest.raises(_core.NativeError, match="unrepresented pending execution input"):
        native_store_call(old_database, "migration_downgrade", {"public": "0033"})
    assert _state(old_database) == before
    assert native_store_call(old_database, "migration_current", {})["public"] == "0038"


@pytest.mark.parametrize("revision", ["0039", "0040", "0041", "0042"])
def test_unreleased_intermediate_targets_are_rejected(old_database, revision):
    before = _state(old_database)
    with pytest.raises(_core.NativeError, match="supported targets"):
        native_store_call(old_database, "migration_upgrade", {"public": revision})
    assert _state(old_database) == before


def test_downgrade_preserves_unrelated_schema_objects(old_database):
    with closing(oracle_connect(old_database)) as connection, connection:
        connection.execute("CREATE TABLE unrelated_evidence(value TEXT)")
        connection.execute("CREATE INDEX unrelated_value ON unrelated_evidence(value)")
        connection.execute("INSERT INTO unrelated_evidence VALUES('keep')")
        connection.execute(
            "CREATE TRIGGER businessXfacts_0034_external AFTER UPDATE ON unrelated_evidence BEGIN SELECT 1; END"
        )
        objects = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='unrelated_evidence' ORDER BY name"
        ).fetchall()
    initialize_native_database(old_database)
    native_store_call(old_database, "migration_downgrade", {"public": "0033"})
    with closing(oracle_connect(old_database)) as connection:
        assert (
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='unrelated_evidence' ORDER BY name"
            ).fetchall()
            == objects
        )
        assert connection.execute("SELECT * FROM unrelated_evidence").fetchall() == [("keep",)]
