"""Revision 0038 replaces only summary guards in a real SQLite transaction."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database, require_native_store
from tests.unit.server.test_analysis_structured_migration import SOURCE_FACT_TABLES, _connect, _downgrade, _upgrade

_PREFIX = "business_facts_0037_"
_SUMMARY_SUFFIXES = ("_summary_insert", "_summary_update", "_summary_delete")
_INPUT_TABLES = (*SOURCE_FACT_TABLES, "replay_observation_symbol", "replay_observation")
_OUTPUT_TABLES = ("performance_cost_range", "performance_execution_summary")


def _is_summary_guard(name):
    return name.startswith(_PREFIX) and name.endswith(_SUMMARY_SUFFIXES)


def _schema(connection, *, include_summary=True):
    rows = connection.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).all()
    return [row for row in rows if include_summary or row.type != "trigger" or not _is_summary_guard(row.name)]


def _summary_guards(connection):
    return {row.name: row.sql for row in _schema(connection) if row.type == "trigger" and _is_summary_guard(row.name)}


def _business_rows(connection):
    result = {}
    for name in sa.inspect(connection).get_table_names():
        if name == "alembic_version":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = connection.exec_driver_sql(f"SELECT * FROM {quoted}").all()
    return result


def _assert_summary_protected(connection):
    with pytest.raises(sa.exc.IntegrityError, match="summary must be materialized"):
        connection.exec_driver_sql("UPDATE execution_order SET price=2 WHERE record_id=1")
    assert connection.exec_driver_sql("SELECT price FROM execution_order WHERE record_id=1").scalar_one() == 1


def _seed_legacy_evidence(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["account", "executerecord", "execution_artifact"])
    created_at = "2026-01-02T09:30:00+08:00"
    connection.execute(
        metadata.tables["account"].insert(),
        {
            "id": 1,
            "name": "summary migration synthetic",
            "market": "FUTURES",
            "trade_channel": "ctp",
            "brokerage": "",
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    inputs = {"curr_target": {"A": 1}}
    summary = {
        "status": "SUCCEEDED",
        "symbol_results": {
            "A": {
                "status": "SUCCEEDED",
                "orders": [{"order_id": "one", "symbol": "A", "price": 1, "volume": 1}],
                "trades": [],
            }
        },
    }
    connection.execute(
        metadata.tables["executerecord"].insert(),
        {
            "id": 1,
            "account_id": 1,
            "execution_id": "summary-migration-exec",
            "created_at": created_at,
            "is_success": 1,
            "raw_input": inputs,
            "raw_result": {"inputs": inputs, **summary},
        },
    )
    connection.execute(
        metadata.tables["execution_artifact"].insert(),
        {
            "execution_id": "summary-migration-exec",
            "artifact_type": "EXECUTION_SUMMARY",
            "schema_version": 2,
            "created_at": created_at,
            "content": summary,
        },
    )


@pytest.fixture
def historical_database(tmp_path):
    database = tmp_path / "summary-guard-migration.sqlite"
    initialize_native_database(database, revision="0036")
    with _connect(database) as connection:
        _seed_legacy_evidence(connection)
    _upgrade(database, "0037")
    with _connect(database) as connection:
        content = json.loads(connection.exec_driver_sql("SELECT content FROM execution_artifact").scalar_one())
        assert content["business_facts_ref"]["view"] == "result"
        connection.exec_driver_sql("CREATE TABLE unrelated_evidence(id INTEGER PRIMARY KEY,value TEXT)")
        connection.exec_driver_sql("CREATE INDEX unrelated_evidence_value ON unrelated_evidence(value)")
        connection.exec_driver_sql("INSERT INTO unrelated_evidence VALUES(1,'preserve exactly')")
        for name in (
            "external_summary_update",
            "business_facts_0037_custom_summary_update_extra",
            "businessXfacts_0037_custom_summary_delete",
        ):
            connection.exec_driver_sql(
                f'CREATE TRIGGER "{name}" AFTER UPDATE ON unrelated_evidence BEGIN SELECT 1; END'
            )
    return database


def _fail_version_stamp(connection, target, condition):
    """在 DDL 已生效时拒绝版本写入，验证整个原生事务回滚。"""
    connection.exec_driver_sql(
        f"CREATE TRIGGER injected_stamp_failure BEFORE UPDATE ON alembic_version "
        f"WHEN NEW.version_num='{target}' AND ({condition}) "
        "BEGIN SELECT RAISE(ABORT,'injected migration stamp failure'); END"
    )


def test_0039_merges_only_frozen_values_and_restores_outputs(historical_database):
    database = historical_database
    _upgrade(database, "0038")
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "INSERT INTO performance_cost_execution(snapshot_id,record_id,time,success,noop,symbol_count,payload) VALUES('frozen',1,1000,1,0,2,'{}'),('frozen',2,1100,1,1,0,'{}')"
        )
        connection.exec_driver_sql(
            "INSERT INTO performance_execution_summary(snapshot_id,record_id,time,value,cost,loss_bp,coverage,covered,count,fee_covered,amount_complete,covered_value,estimated,symbol_count,has_transactions) VALUES('frozen',1,1000,NULL,-2,NULL,NULL,0,3,1,0,0,2,2,1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO performance_result_range(snapshot_id,range_key,backtest_included,engine_version,baseline,end,record_count,observation_count,used_record_count,target_used_record_count,invalid_asset_count,calendar_status,event_count) VALUES('frozen','all',1,'engine','2026-01-01T09:00:00.000003','2026-01-01T01:00:00.000003Z',0,0,0,0,0,'available',0)"
        )
        bound = datetime(2026, 1, 1, 9, microsecond=3, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000.0
        connection.exec_driver_sql("INSERT INTO performance_cost_range VALUES('frozen','all',?,?)", (bound, bound))
        before = {name: connection.exec_driver_sql(f"SELECT * FROM {name}").all() for name in _OUTPUT_TABLES}
    _upgrade(database, "0039")
    with _connect(database) as connection:
        assert not set(_OUTPUT_TABLES) & set(sa.inspect(connection).get_table_names())
        assert connection.exec_driver_sql(
            "SELECT value,cost,count,amount_complete,estimated FROM performance_cost_execution WHERE record_id=1"
        ).one() == (None, -2, 3, 0, 2)
        assert (
            connection.exec_driver_sql(
                "SELECT amount_complete FROM performance_cost_execution WHERE record_id=2"
            ).scalar_one()
            is None
        )
    _downgrade(database, "0038")
    with _connect(database) as connection:
        for name, rows in before.items():
            assert connection.exec_driver_sql(f"SELECT * FROM {name}").all() == rows


def test_0039_removes_only_inputs_and_restores_old_guards(historical_database):
    database = historical_database
    _upgrade(database, "0038")
    with _connect(database) as connection:
        guards = _summary_guards(connection)
        original_schema = _schema(connection)
        version = connection.exec_driver_sql(
            "SELECT source_version FROM account_analysis WHERE account_id=1"
        ).scalar_one()
    _upgrade(database, "0039")
    with _connect(database) as connection:
        assert not set(_INPUT_TABLES) & set(sa.inspect(connection).get_table_names())
        assert _summary_guards(connection) == guards
        assert (
            connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
            == version
        )
        _assert_summary_protected(connection)
        for row in _schema(connection):
            if row.type == "trigger":
                assert not any(table in row.sql for table in _INPUT_TABLES)
    _downgrade(database, "0038")
    with _connect(database) as connection:
        assert set(_INPUT_TABLES) <= set(sa.inspect(connection).get_table_names())
        assert _schema(connection) == original_schema
        assert _summary_guards(connection) == guards
        _assert_summary_protected(connection)
    _upgrade(database, "0039")
    with _connect(database) as connection:
        assert not set(_INPUT_TABLES) & set(sa.inspect(connection).get_table_names())


def test_0039_retains_old_display_and_refuses_lossy_memory_downgrade(historical_database):
    database = historical_database
    _upgrade(database, "0038")
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "INSERT INTO performance_cost_execution_symbol(snapshot_id,record_id,symbol,present,error) VALUES('frozen',1,'A',2,'旧展示')"
        )
    _upgrade(database, "0039")
    with _connect(database) as connection:
        assert connection.exec_driver_sql(
            "SELECT error,memory_message,memory_reason_code FROM performance_cost_execution_symbol"
        ).one() == ("旧展示", None, None)
        connection.exec_driver_sql("UPDATE performance_cost_execution_symbol SET memory_message='未达到目标'")
        before = _schema(connection)
    with pytest.raises(require_native_store("migration_downgrade").NativeError, match="frozen symbol memory evidence"):
        _downgrade(database, "0038")
    with _connect(database) as connection:
        assert _schema(connection) == before
        assert (
            connection.exec_driver_sql("SELECT memory_message FROM performance_cost_execution_symbol").scalar_one()
            == "未达到目标"
        )


def test_0037_to_0038_roundtrip_preserves_everything_except_summary_guards(historical_database):
    database = historical_database
    with _connect(database) as connection:
        original = _summary_guards(connection)
        original_schema = _schema(connection, include_summary=False)
        original_rows = _business_rows(connection)
        empty_name = _PREFIX + "execution_input_summary_insert"
        assert empty_name in original
        assert all("GLOB" not in sql for sql in original.values())
        _assert_summary_protected(connection)
    _upgrade(database, "0038")
    with _connect(database) as connection:
        optimized = _summary_guards(connection)
        assert optimized.keys() == original.keys()
        assert empty_name in optimized
        for name, sql in optimized.items():
            if name.endswith("_summary_insert"):
                assert "GLOB" in sql
            else:
                assert sql == original[name]
        assert all(" BEFORE " in sql for sql in optimized.values())
        assert _schema(connection, include_summary=False) == original_schema
        assert _business_rows(connection) == original_rows
        _assert_summary_protected(connection)
    _upgrade(database, "0038")
    with _connect(database) as connection:
        assert _summary_guards(connection) == optimized
    _downgrade(database, "0037")
    with _connect(database) as connection:
        assert _summary_guards(connection) == original
        assert _schema(connection, include_summary=False) == original_schema
        assert _business_rows(connection) == original_rows
        _assert_summary_protected(connection)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0039_ddl_failure_restores_schema_and_rows(historical_database, direction):
    database = historical_database
    _upgrade(database, "0038")
    if direction == "downgrade":
        _upgrade(database, "0039")
    target = "0039" if direction == "upgrade" else "0038"
    with _connect(database) as connection:
        condition = "EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name GLOB 'replay_obser*')"
        _fail_version_stamp(connection, target, "NOT " + condition if direction == "upgrade" else condition)
        schema = _schema(connection)
        rows = _business_rows(connection)
    operation = _upgrade if direction == "upgrade" else _downgrade
    with pytest.raises(
        require_native_store("migration_" + direction).NativeError, match="injected migration stamp failure"
    ):
        operation(database, target)
    with _connect(database) as connection:
        assert _schema(connection) == schema
        assert _business_rows(connection) == rows
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == (
            "0038" if direction == "upgrade" else "0039"
        )
        _assert_summary_protected(connection)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_summary_guard_ddl_failure_rolls_back_original_definitions(historical_database, direction):
    database = historical_database
    if direction == "downgrade":
        _upgrade(database, "0038")
    target = "0038" if direction == "upgrade" else "0037"
    with _connect(database) as connection:
        glob = "EXISTS(SELECT 1 FROM sqlite_master WHERE name='business_facts_0037_execution_input_summary_insert' AND sql LIKE '%GLOB%')"
        _fail_version_stamp(connection, target, glob if direction == "upgrade" else "NOT " + glob)
        original_schema = _schema(connection)
        original_rows = _business_rows(connection)
        original_guards = _summary_guards(connection)
    operation = _upgrade if direction == "upgrade" else _downgrade
    with pytest.raises(
        require_native_store("migration_" + direction).NativeError, match="injected migration stamp failure"
    ):
        operation(database, target)
    with _connect(database) as connection:
        assert _schema(connection) == original_schema
        assert _business_rows(connection) == original_rows
        assert _summary_guards(connection) == original_guards
        _assert_summary_protected(connection)
        connection.exec_driver_sql("DROP TRIGGER injected_stamp_failure")
        original_schema = _schema(connection)
    operation(database, target)
    with _connect(database) as connection:
        assert _summary_guards(connection) != original_guards
        assert _schema(connection, include_summary=False) == [
            row for row in original_schema if row.type != "trigger" or not _is_summary_guard(row.name)
        ]
        assert _business_rows(connection) == original_rows
        _assert_summary_protected(connection)
