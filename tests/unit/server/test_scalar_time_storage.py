"""0034 时间字段的真实迁移契约；JSON 保持原始存储。"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_naive_historical_scalars_use_migration_host_timezone(tmp_path, host_timezone):
    source = Path(__file__).parents[2] / "fixtures/rust_store/v0.23.1.sql"
    database = tmp_path / "local-time.db"
    program = r"""
import json, sqlite3, sys, time
from axile.server.native_store import native_operation_sync
from tests.fixtures.rust_store.schema_contract import load_schema
if hasattr(time, "tzset"):
    time.tzset()
with sqlite3.connect(sys.argv[1]) as connection:
    load_schema(connection, open(sys.argv[2]).read())
    connection.execute("INSERT INTO alembic_version VALUES ('0033')")
    connection.execute("INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(1,'time','FUTURES','ctp','','2026-07-01T09:00:00.123456','2026-07-01T09:00:00.123456')")
    connection.execute("INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(2,'negative','FUTURES','ctp','','1969-12-31T23:59:59.999999','1969-12-31T23:59:59.999999')")
    connection.execute("CREATE TABLE user_opaque(id INTEGER PRIMARY KEY,payload JSON)")
    connection.execute("INSERT INTO user_opaque VALUES(1,?)", ('{ "created_at" : "not-a-date", "nested":{"trade_time":"2026-07-01T09:00:00"}}',))
receipt = native_operation_sync("migration_upgrade", {}, database=sys.argv[1])
with sqlite3.connect(sys.argv[1]) as connection:
    instant, kind = connection.execute("SELECT created_at_us,typeof(created_at_us) FROM account WHERE id=1").fetchone()
    negative = connection.execute("SELECT created_at_us FROM account WHERE id=2").fetchone()[0]
    opaque = connection.execute("SELECT payload FROM user_opaque").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
print(json.dumps({"instant":instant,"kind":kind,"negative":negative,"opaque":opaque,"integrity":integrity,"revision":receipt["public"]}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(database), str(source)],
        env={**os.environ, "TZ": host_timezone},
        cwd=Path(__file__).parents[3],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    instant = datetime(2026, 7, 1, 9, 0, 0, 123456, tzinfo=ZoneInfo(host_timezone)).astimezone(UTC)
    delta = instant - datetime(1970, 1, 1, tzinfo=UTC)
    assert observed["instant"] == (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    previous = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=ZoneInfo(host_timezone)).astimezone(UTC)
    previous_delta = previous - datetime(1970, 1, 1, tzinfo=UTC)
    assert (
        observed["negative"]
        == (previous_delta.days * 86400 + previous_delta.seconds) * 1_000_000 + previous_delta.microseconds
    )
    assert observed["kind"] == "integer"
    assert observed["opaque"] == '{ "created_at" : "not-a-date", "nested":{"trade_time":"2026-07-01T09:00:00"}}'
    assert observed["revision"] == "0034"
    assert observed["integrity"] == "ok"


@pytest.fixture
def published_database(tmp_path):
    database = tmp_path / "published-0033.db"
    initialize_native_database(database, revision="0033")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
            "VALUES(1,'time','FUTURES','ctp','','2026-07-01T09:00:00.123456Z','2026-07-01T09:00:00.123456Z')"
        )
    yield database
    _core.close_database(str(database))


def _database_dump(database):
    _core.close_database(str(database))
    with closing(sqlite3.connect(database)) as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize(
    ("definition", "reason"),
    [
        ("CREATE VIEW custom_view AS SELECT /* readable comment */ * FROM account", "time column"),
        ("CREATE VIEW custom_view AS SELECT -- readable comment\n * FROM account", "time column"),
        (
            'CREATE VIEW custom_view AS SELECT DISTINCT "a/*""--".* FROM account AS "a/*""--"',
            "time column",
        ),
        (
            "CREATE TRIGGER custom_audit AFTER UPDATE ON account "
            "BEGIN INSERT INTO account_copy SELECT * FROM account WHERE id=NEW.id; END",
            "time column",
        ),
        pytest.param(
            "CREATE VIEW custom_view AS SELECT * FROM 'account'",
            "time column",
            id="single_quoted_table",
        ),
        pytest.param(
            "CREATE TRIGGER custom_audit AFTER UPDATE ON 'account' "
            "WHEN NEW.'created_at'>'2025-01-01' BEGIN SELECT 1; END",
            "time column",
            id="single_quoted_column",
        ),
        pytest.param(
            'CREATE VIEW custom_view AS SELECT "created_at" FROM "account"',
            "time column",
            id="double_quoted_column",
        ),
        pytest.param(
            "CREATE VIEW custom_view AS SELECT `created_at` FROM `account`",
            "time column",
            id="backtick_quoted_column",
        ),
        pytest.param(
            "CREATE VIEW custom_view AS SELECT [created_at] FROM [account]",
            "time column",
            id="bracket_quoted_column",
        ),
        pytest.param(
            "CREATE TRIGGER custom_audit AFTER UPDATE OF remark ON account "
            "BEGIN UPDATE account SET created_at='2026-07-01T09:00:00Z' WHERE id=NEW.id; END",
            "time column",
            id="trigger_writes_time_literal",
        ),
        (
            "CREATE TRIGGER business_facts_0034_execution_input_insert AFTER UPDATE ON account "
            "WHEN NEW.created_at>'2025-01-01' BEGIN SELECT 1; END",
            "reserved trigger name",
        ),
        (
            "CREATE TRIGGER business_facts_0034_execution_input_insert AFTER UPDATE ON account BEGIN SELECT 1; END",
            "reserved trigger name",
        ),
        (
            "CREATE TRIGGER Business_Facts_0034_Execution_Input_Insert AFTER UPDATE ON account BEGIN SELECT 1; END",
            "reserved trigger name",
        ),
        (
            "CREATE TRIGGER business_facts_0034_custom_delete AFTER DELETE ON executerecord BEGIN SELECT 1; END",
            "reserved trigger name",
        ),
        pytest.param(
            "CREATE INDEX ix_executerecord_account_time_id ON account(id) WHERE created_at>'2025-01-01'",
            "reserved index name",
            id="reserved_time_index",
        ),
    ],
)
def test_custom_time_dependencies_and_reserved_names_leave_0033_unchanged(published_database, definition, reason):
    with closing(sqlite3.connect(published_database)) as connection, connection:
        connection.execute("CREATE TABLE account_copy AS SELECT * FROM account WHERE 0")
        connection.execute(definition)
    before = _database_dump(published_database)
    with pytest.raises(_core.NativeError, match=reason):
        native_store_call(published_database, "migration_upgrade", {})
    assert _database_dump(published_database) == before


@pytest.mark.parametrize(
    ("table", "definition", "reason"),
    [
        pytest.param("account", "guard INTEGER CHECK(created_at<'2027-01-01')", "time column", id="check"),
        # 身份表重建可能先拒绝限定列名，失败仍须原子回滚。
        pytest.param(
            "account",
            "guard INTEGER CHECK(account.'created_at'<'2027-01-01')",
            r"time column|no such column: account\.created_at",
            id="qualified_check",
        ),
        pytest.param(
            "account",
            "guard TEXT GENERATED ALWAYS AS (substr(created_at,1,10)) VIRTUAL",
            "time column",
            id="generated_column",
        ),
        pytest.param(
            "supplement_group",
            "guard INTEGER CHECK(base_scheduled_at<'2027-01-01')",
            "time column",
            id="unique_column_check",
        ),
    ],
)
def test_custom_table_time_dependencies_leave_0033_unchanged(published_database, table, definition, reason):
    with closing(sqlite3.connect(published_database)) as connection, connection:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    before = _database_dump(published_database)
    with pytest.raises(_core.NativeError, match=reason):
        native_store_call(published_database, "migration_upgrade", {})
    assert _database_dump(published_database) == before


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW custom_view AS SELECT * FROM 'account'",
        "CREATE TRIGGER custom_audit AFTER UPDATE ON 'account' WHEN NEW.'created_at_us'>0 BEGIN SELECT 1; END",
    ],
)
def test_custom_quoted_time_dependencies_leave_0034_unchanged_on_downgrade(published_database, definition):
    assert native_store_call(published_database, "migration_upgrade", {})["public"] == "0034"
    _core.close_database(str(published_database))
    with closing(sqlite3.connect(published_database)) as connection, connection:
        connection.execute(definition)
    before = _database_dump(published_database)
    with pytest.raises(_core.NativeError, match="time column"):
        native_store_call(published_database, "migration_downgrade", {"public": "0033"})
    assert _database_dump(published_database) == before


def test_safe_custom_sql_and_opaque_json_survive_upgrade_downgrade_upgrade(published_database):
    opaque = b'{ "created_at": "not-a-date", "nested": {"trade_time": "unchanged"} }'
    with closing(sqlite3.connect(published_database)) as connection, connection:
        connection.execute("CREATE TABLE user_opaque(id INTEGER PRIMARY KEY,payload JSON)")
        connection.execute("INSERT INTO user_opaque VALUES(1,?)", (opaque,))
        connection.execute(
            'CREATE VIEW safe_view AS SELECT id AS "quoted/*""--", '
            "'/* created_at */ --' AS literal, 'created_at' AS column_literal, 'account' AS table_literal "
            "/* updated_at */ FROM account"
        )
        connection.execute("ALTER TABLE account ADD COLUMN guard INTEGER CHECK(guard>=0)")
        connection.execute("UPDATE account SET guard=2")
        connection.execute("CREATE TABLE custom_trigger_audit(account_id INTEGER)")
        connection.execute("CREATE VIEW editable_view AS SELECT id,guard FROM account")
        connection.execute(
            "CREATE TRIGGER safe_view_update INSTEAD OF UPDATE ON editable_view BEGIN "
            "INSERT INTO custom_trigger_audit VALUES(NEW.id); "
            "UPDATE account SET guard=NEW.guard WHERE id=OLD.id; END"
        )
        connection.execute("CREATE TRIGGER businessXfacts_0034_custom AFTER UPDATE ON account BEGIN SELECT 1; END")
    for operation, request, revision in [
        ("migration_upgrade", {}, "0034"),
        ("migration_downgrade", {"public": "0033"}, "0033"),
        ("migration_upgrade", {}, "0034"),
    ]:
        assert native_store_call(published_database, operation, request)["public"] == revision
        _core.close_database(str(published_database))
        with closing(sqlite3.connect(published_database)) as connection:
            assert connection.execute("SELECT * FROM safe_view").fetchall() == [
                (1, "/* created_at */ --", "created_at", "account")
            ]
            assert connection.execute("SELECT guard FROM account").fetchone() == (2,)
            assert connection.execute("SELECT count(*) FROM custom_trigger_audit").fetchone() == (0,)
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute("UPDATE account SET guard=-1")
            assert connection.execute("SELECT payload FROM user_opaque").fetchone() == (opaque,)
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='businessXfacts_0034_custom'"
            ).fetchone()
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with closing(sqlite3.connect(published_database)) as connection, connection:
        connection.execute("UPDATE editable_view SET guard=3 WHERE id=1")
        assert connection.execute("SELECT guard FROM account").fetchone() == (3,)
        assert connection.execute("SELECT account_id FROM custom_trigger_audit").fetchall() == [(1,)]
