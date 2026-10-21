"""Revision 0038 replaces only summary guards in a real SQLite transaction."""

import json

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration

_PREFIX = "business_facts_0037_"
_SUMMARY_SUFFIXES = ("_summary_insert", "_summary_update", "_summary_delete")


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    """Use only historical migration DDL, not the shared runtime ORM database."""
    yield


def _revision(connection, filename):
    migration = _load_migration(_MIGRATIONS_DIR / filename)
    migration.op = Operations(MigrationContext.configure(connection))
    return migration


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
            "account_control_preset": "default",
            "account_config": {},
            "is_started": False,
            "cron_expr": "0 0 0 * * *",
            "brokerage": "",
            "weight_precision": 0,
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
def historical_connection(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'summary-guard-migration.sqlite'}")
    try:
        with engine.connect() as connection:
            # SQLAlchemy's logical begin alone does not start SQLite DDL transactions.
            connection.exec_driver_sql("BEGIN")
            assert connection.connection.driver_connection.in_transaction
            for path in sorted(_MIGRATIONS_DIR.glob("*.py")):
                if path.stem[:4].isdigit() and int(path.stem[:4]) <= 36:
                    _revision(connection, path.name).upgrade()
            _seed_legacy_evidence(connection)
            _revision(connection, "0037_business_execution_facts.py").upgrade()
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
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def test_0037_to_0038_roundtrip_preserves_everything_except_summary_guards(historical_connection):
    connection = historical_connection
    migration = _revision(connection, "0038_summary_guard_prefilter.py")
    assert migration.revision == "0038"
    assert migration.down_revision == "0037"
    original = _summary_guards(connection)
    original_schema = _schema(connection, include_summary=False)
    original_rows = _business_rows(connection)
    empty_name = _PREFIX + "execution_input_summary_insert"
    assert empty_name in original
    assert all("GLOB" not in sql for sql in original.values())
    _assert_summary_protected(connection)

    migration.upgrade()
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

    migration.upgrade()
    assert _summary_guards(connection) == optimized
    migration.downgrade()
    assert _summary_guards(connection) == original
    assert _schema(connection, include_summary=False) == original_schema
    assert _business_rows(connection) == original_rows
    _assert_summary_protected(connection)
    assert connection.connection.driver_connection.in_transaction


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_summary_guard_ddl_failure_rolls_back_original_definitions(historical_connection, direction):
    connection = historical_connection
    migration = _revision(connection, "0038_summary_guard_prefilter.py")
    if direction == "downgrade":
        migration.upgrade()
    original_schema = _schema(connection)
    original_rows = _business_rows(connection)
    original_guards = _summary_guards(connection)
    created = []

    def inject_invalid_ddl(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("CREATE TRIGGER") and _PREFIX in statement and "_summary_" in statement:
            created.append(statement)
            if len(created) == 2:
                # At least one new definition already exists after all old guards were dropped.
                assert len(_summary_guards(connection)) == 1
                return "CREATE TRIGGER", parameters
        return statement, parameters

    sa.event.listen(connection, "before_cursor_execute", inject_invalid_ddl, retval=True)
    try:
        with pytest.raises(sa.exc.OperationalError):
            getattr(migration, direction)()
    finally:
        sa.event.remove(connection, "before_cursor_execute", inject_invalid_ddl)

    assert len(created) == 2
    assert connection.connection.driver_connection.in_transaction
    assert _schema(connection) == original_schema
    assert _business_rows(connection) == original_rows
    assert _summary_guards(connection) == original_guards
    _assert_summary_protected(connection)
    getattr(migration, direction)()
    assert _summary_guards(connection) != original_guards
    assert _schema(connection, include_summary=False) == [
        row for row in original_schema if row.type != "trigger" or not _is_summary_guard(row.name)
    ]
    assert _business_rows(connection) == original_rows
    _assert_summary_protected(connection)
