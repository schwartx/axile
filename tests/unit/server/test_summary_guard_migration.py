"""Revision 0038 replaces only summary guards in a real SQLite transaction."""

import json

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database
from tests.unit.server.test_analysis_structured_migration import SOURCE_FACT_TABLES, _connect, _upgrade

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
    initialize_native_database(database, revision="0033")
    with _connect(database) as connection:
        _seed_legacy_evidence(connection)
    _upgrade(database, "0041")
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
