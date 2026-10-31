"""账户身份迁移保留旧数据，并隔离删除后残留的账户编号。"""

import json
from contextlib import closing

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.sqlite_oracle import to_us
from tests.unit.server._execution_test_support import account_settings_values, build_account

_NOW = "2026-10-20T09:30:00+08:00"


def _insert_0033_account(connection):
    account = build_account(id=7, portfolio_id=None, created_at=_NOW, updated_at=_NOW)
    values = account.model_dump(mode="json")
    names = (
        "id",
        "name",
        "market",
        "trade_channel",
        "remark",
        "brokerage",
        "portfolio_id",
        "created_at",
        "updated_at",
        "copied_from_account_id",
        "copied_from_account_name",
    )
    connection.execute(
        f"INSERT INTO account ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        [values.get(name) for name in names],
    )
    settings = account_settings_values(account)
    groups = ("connection", "execution", "schedule", "notification", "backtest")
    connection.execute(
        "INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) VALUES (?,?,?,?,?,?)",
        [account.id, *[json.dumps(settings[group], ensure_ascii=False, allow_nan=False) for group in groups]],
    )


def _account_rows(connection):
    cursor = connection.execute("SELECT * FROM account")
    names = [column[0] for column in cursor.description]
    return [
        tuple(to_us(value) if name in {"created_at", "updated_at"} else value for name, value in zip(names, row))
        for row in cursor.fetchall()
    ]


def test_account_identity_upgrade_and_downgrade_preserve_schema_and_data(tmp_path):
    path = tmp_path / "identity.db"
    initialize_native_database(path, revision="0033")
    with closing(oracle_connect(path)) as connection, connection:
        _insert_0033_account(connection)
        connection.executescript(
            "CREATE INDEX account_identity_name ON account(name);"
            "CREATE TABLE account_identity_log(name TEXT);"
            "CREATE TRIGGER account_identity_insert AFTER INSERT ON account "
            "BEGIN INSERT INTO account_identity_log VALUES(NEW.name); END;"
            "CREATE VIEW account_identity_view AS SELECT id,name FROM account;"
        )
        before = _account_rows(connection)
        settings = connection.execute("SELECT * FROM account_settings").fetchall()
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name='account_identity_insert' ORDER BY name"
        ).fetchall()
    for revision, autoincrement in (("0034", True), ("0033", False), ("0034", True)):
        operation = "migration_upgrade" if autoincrement else "migration_downgrade"
        assert native_store_call(path, operation, {"public": revision})["public"] == revision
        with closing(oracle_connect(path)) as connection:
            assert _account_rows(connection) == before
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
        path, "account_create", {"account": build_account(portfolio_id=None).model_dump(mode="json"), "now": _NOW}
    )["account"]
    with closing(oracle_connect(path)) as connection:
        assert connection.execute("SELECT * FROM account_identity_log").fetchall() == [(created["name"],)]


def test_account_identity_upgrade_skips_legacy_orphan_ids(tmp_path):
    path = tmp_path / "orphan.db"
    initialize_native_database(path, revision="0033")
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO accountruntimesync(account_id,revision,status,reset_worker,create_request_key,attempts,requested_at) "
            "VALUES(91,1,'pending',0,'deleted-create',0,?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,payload,created_at) "
            "VALUES('deleted-intent',92,'REBALANCE','manual','QUEUED','{}',?)",
            (_NOW,),
        )
    initialize_native_database(path)
    request = {
        "account": build_account(portfolio_id=None).model_dump(mode="json"),
        "now": _NOW,
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
        _insert_0033_account(connection)
        connection.execute(
            "CREATE TRIGGER reject_identity_revision BEFORE UPDATE ON alembic_version "
            "WHEN NEW.version_num='0034' BEGIN SELECT RAISE(ABORT,'identity rejected'); END"
        )
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        account = connection.execute("SELECT * FROM account").fetchall()
    with pytest.raises(_core.NativeError, match="identity rejected"):
        initialize_native_database(path)
    with closing(oracle_connect(path)) as connection:
        assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
        assert connection.execute("SELECT * FROM account").fetchall() == account
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0033",)]
