"""账户拆分的持久化、迁移与通知成功摘要契约。"""

import json
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from axile.server.core import db
from axile.server.db.models import Account, AccountNotificationState, AccountSnapshot, AccountUpdate
from axile.server.db.models.account_settings import ExecutionSettings
from axile.server.execution import notification_state
from tests.conftest import initialize_native_database, native_store_call
from tests.unit.server._execution_test_support import insert_account


def account_data() -> dict:
    return dict(
        id=1,
        name="账户",
        market="期货",
        trade_channel="ctp",
        brokerage="test",
        remark=None,
        account_config={"nested": {"secret": "test-only"}},
        account_control_preset="default",
        is_started=True,
        cron_expr="0 9 * * *",
        weight_precision=0.01,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        feishu_key="test-key",
        execution_notification_code="def notify(context):\n    pass",
        trade_rules=None,
        forbidden_symbols=None,
        risk_symbols=None,
    )


def stored_account(path):
    return Account.model_validate(native_store_call(path, "account_get", {"account_id": 1}))


def test_settings_persist_patch_and_snapshot_are_independent(native_database):
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**account_data()))
    loaded = stored_account(native_database)
    assert loaded.settings.account_id == 1
    snapshot = loaded.snapshot()
    assert isinstance(snapshot, AccountSnapshot)
    snapshot.account_config["nested"]["secret"] = "changed"
    assert loaded.account_config["nested"]["secret"] == "test-only"
    patch = {"long_leverage": 3, "execution_notification_code": None}
    loaded.settings.apply_patch(patch)
    assert loaded.is_started
    assert loaded.feishu_key == "test-key"
    assert loaded.execution_notification_code is None
    with pytest.raises(ValidationError):
        loaded.settings.apply_patch({"weight_precision": -1, "cron_expr": "bad cron"})
    with pytest.raises(ValidationError):
        AccountUpdate(name="invalid", weight_precision=-1, cron_expr="bad cron")
    assert loaded.name == "账户"
    assert loaded.weight_precision == 0.01
    native_store_call(native_database, "account_update", {"account_id": 1, "patch": patch, "now": "updated"})
    account = stored_account(native_database)
    assert account.long_leverage == 3
    assert account.execution_notification_code is None
    with closing(sqlite3.connect(native_database)) as connection:
        assert "algorithm" not in {row[1] for row in connection.execute("PRAGMA table_info(account)")}
    assert isinstance(account.settings.execution, ExecutionSettings)


def test_migration_preserves_all_settings_and_binding_rows(tmp_path):
    path = tmp_path / "settings-migration.db"
    initialize_native_database(path, revision="0019")
    values = Account(**account_data()).model_dump(mode="json")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(account)")}
        json_fields = {name for name, kind in columns.items() if kind == "JSON"}
        values = {
            key: json.dumps(value) if key in json_fields else value for key, value in values.items() if key in columns
        }
        connection.execute(
            f"INSERT INTO account ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())
        )
        connection.execute(
            "INSERT INTO portfolioaccount (account_id, portfolio_id, created_at) VALUES (1, NULL, '2026-09-30')"
        )
        before = dict(connection.execute("SELECT * FROM account").fetchone())
    initialize_native_database(path, revision="0020")
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        stored = connection.execute("SELECT * FROM account_settings").fetchone()
        assert json.loads(stored["connection"]) == json.loads(before["account_config"])
        assert (
            json.loads(stored["notification"])["execution_notification_code"] == before["execution_notification_code"]
        )
        assert connection.execute("SELECT count(*) FROM portfolioaccount").fetchone()[0] == 1
        assert "algorithm" not in {row[1] for row in connection.execute("PRAGMA table_info(account)")}
    native_store_call(path, "migration_downgrade", {"public": "0019"})
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        restored = dict(connection.execute("SELECT * FROM account").fetchone())
        for row in (before, restored):
            for field in json_fields & row.keys():
                row[field] = json.loads(row[field]) if row[field] is not None else None
        assert restored == before
        assert connection.execute("SELECT count(*) FROM portfolioaccount").fetchone()[0] == 1


def test_success_state_only_advances_and_deleted_account_is_ignored(monkeypatch, native_database):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**account_data()))
    notification_state.record_notification_result(1, "exec-new", "2026-09-30T10:01:00", True, None)
    notification_state.record_notification_result(1, "exec-old", "2026-09-30T10:00:00", True, None)
    account = stored_account(native_database)
    assert account.notification_state.execution_id == "exec-new"
    assert account.notification_state.last_success_at == "2026-09-30T10:01:00"
    native_store_call(native_database, "account_delete", {"account_id": 1})
    notification_state.record_notification_result(1, "exec-deleted", "2026-09-30T10:02:00", True, None)
    with closing(sqlite3.connect(native_database)) as connection:
        assert connection.execute("SELECT * FROM account_notification_state").fetchall() == []
        assert connection.execute("SELECT * FROM account_settings").fetchall() == []


def test_public_response_exposes_success_summary_without_credentials():
    from axile.server.api.routes.account_crud import _account_public

    account = Account(**account_data())
    account.notification_state = AccountNotificationState(
        account_id=1, execution_id="exec-1", last_success_at="2026-09-30T10:00:00"
    )
    public = _account_public(account).model_dump(mode="json")
    assert public["notification_state"]["execution_id"] == "exec-1"
    assert public["notification_state"]["last_success_at"] == "2026-09-30T10:00:00"
    assert public["notification_state"]["last_attempt_ok"] is None
    assert "account_config" not in public
    assert "feishu_key" not in public
    assert "test-only" not in str(public)
    assert "test-key" not in str(public)
    assert account.model_dump(include={"name", "feishu_key"}) == {"name": "账户", "feishu_key": "test-key"}
    assert "remark" not in account.model_dump(exclude_none=True)
    before = account.settings.execution
    with pytest.raises(ValueError):
        account.settings.apply_patch({"long_leverage": 3, "account_config": None})
    assert account.settings.execution is before


def test_notification_result_preserves_success_and_ignores_older_results(monkeypatch, native_database):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**account_data()))
    notification_state.record_notification_result(1, "first-fail", "2026-09-30T09:00:00", False, "timeout")
    state = stored_account(native_database).notification_state
    assert state.last_success_at is None
    assert state.last_attempt_ok is False
    notification_state.record_notification_result(1, "success", "2026-09-30T10:00:00", True, None)
    notification_state.record_notification_result(1, "failure", "2026-09-30T11:00:00", False, "timeout")
    notification_state.record_notification_result(1, "old", "2026-09-30T09:30:00", True, None)
    state = stored_account(native_database).notification_state
    assert state.execution_id == "success"
    assert state.last_attempt_execution_id == "failure"
    assert state.last_attempt_ok is False
    assert state.last_attempt_error == "timeout"
    notification_state.record_notification_result(999, "missing", "2026-09-30T12:00:00", True, None)


def test_notification_result_migration_preserves_legacy_success(tmp_path):
    path = tmp_path / "notification-migration.db"
    initialize_native_database(path, revision="0020")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, brokerage, created_at, updated_at) VALUES (1, 'test', '期货', 'ctp', 0, 'now', 'now')"
        )
        connection.execute("INSERT INTO account_notification_state VALUES (1, '2026-09-30T10:00:00', 'old')")
    initialize_native_database(path, revision="0021")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, brokerage, created_at, updated_at) VALUES (2, 'test2', '期货', 'ctp', 0, 'now', 'now')"
        )
        connection.execute("INSERT INTO account_notification_state (account_id, last_attempt_ok) VALUES (2, 0)")
        assert (
            connection.execute("SELECT execution_id FROM account_notification_state WHERE account_id = 1").fetchone()[0]
            == "old"
        )
    native_store_call(path, "migration_downgrade", {"public": "0020"})
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM account_notification_state").fetchone()[0] == 1
