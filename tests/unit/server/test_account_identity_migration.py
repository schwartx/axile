"""账户身份迁移保留旧数据，并隔离删除后残留的账户编号。"""

from contextlib import closing

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account, insert_account


def test_account_identity_upgrade_and_downgrade_preserve_schema_and_data(tmp_path):
    path = tmp_path / "identity.db"
    initialize_native_database(path, revision="0033")
    with closing(oracle_connect(path)) as connection, connection:
        insert_account(connection, build_account(id=7, portfolio_id=None))
        connection.executescript(
            "CREATE INDEX account_identity_name ON account(name);"
            "CREATE TABLE account_identity_log(name TEXT);"
            "CREATE TRIGGER account_identity_insert AFTER INSERT ON account "
            "BEGIN INSERT INTO account_identity_log VALUES(NEW.name); END;"
            "CREATE VIEW account_identity_view AS SELECT id,name FROM account;"
        )
        before = connection.execute("SELECT * FROM account").fetchall()
        settings = connection.execute("SELECT * FROM account_settings").fetchall()
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name='account_identity_insert' ORDER BY name"
        ).fetchall()
    for revision, autoincrement in (("0041", True), ("0033", False), ("0041", True)):
        operation = "migration_upgrade" if autoincrement else "migration_downgrade"
        assert native_store_call(path, operation, {"public": revision})["public"] == revision
        with closing(oracle_connect(path)) as connection:
            assert connection.execute("SELECT * FROM account").fetchall() == before
            assert connection.execute("SELECT * FROM account_settings").fetchall() == settings
            assert connection.execute("SELECT * FROM account_identity_view").fetchall() == [(7, before[0][0])]
            assert (
                connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name='account_identity_insert' ORDER BY name"
                ).fetchall()
                == triggers
            )
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='account_identity_name'").fetchone()
            definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='account'").fetchone()[0]
            assert ("AUTOINCREMENT" in definition) is autoincrement
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    created = native_store_call(
        path, "account_create", {"account": build_account(portfolio_id=None).model_dump(mode="json"), "now": "now"}
    )["account"]
    with closing(oracle_connect(path)) as connection:
        assert connection.execute("SELECT * FROM account_identity_log").fetchall() == [(created["name"],)]


def test_account_identity_upgrade_skips_legacy_orphan_ids(tmp_path):
    path = tmp_path / "orphan.db"
    initialize_native_database(path, revision="0033")
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO accountruntimesync(account_id,revision,status,reset_worker,create_request_key,attempts,requested_at) "
            "VALUES(91,1,'pending',0,'deleted-create',0,'now')"
        )
        connection.execute(
            "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,payload,created_at) "
            "VALUES('deleted-intent',92,'REBALANCE','manual','QUEUED','{}','now')"
        )
    initialize_native_database(path)
    request = {
        "account": build_account(portfolio_id=None).model_dump(mode="json"),
        "now": "now",
        "idempotency_key": "new-create",
    }
    created = native_store_call(path, "account_create", request)
    account_id = created["account"]["id"]
    assert account_id > 92
    assert native_store_call(path, "intent_active", {"account_id": account_id}) == {"running": None, "queued": None}
    assert native_store_call(path, "account_create", request)["account"]["id"] == account_id
    assert native_store_call(path, "account_create", request)["replayed"] is True


def test_account_identity_migration_failure_rolls_back_ddl_and_rows(tmp_path):
    path = tmp_path / "rollback.db"
    initialize_native_database(path, revision="0033")
    with closing(oracle_connect(path)) as connection, connection:
        insert_account(connection, build_account(id=7, portfolio_id=None))
        connection.execute(
            "CREATE TRIGGER reject_identity_revision BEFORE UPDATE ON alembic_version "
            "WHEN NEW.version_num='0041' BEGIN SELECT RAISE(ABORT,'identity rejected'); END"
        )
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        account = connection.execute("SELECT * FROM account").fetchall()
    with pytest.raises(_core.NativeError, match="identity rejected"):
        initialize_native_database(path)
    with closing(oracle_connect(path)) as connection:
        assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
        assert connection.execute("SELECT * FROM account").fetchall() == account
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0033",)]
