"""账户拆分的持久化、迁移与通知成功摘要契约。"""

import json
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from axile.server.core import db
from axile.server.db.models import Account, AccountNotificationState, AccountSnapshot, AccountUpdate
from axile.server.db.models.account import AccountCreate
from axile.server.execution import notification_state
from tests.conftest import native_store_call
from tests.sqlite_oracle import utc_time
from tests.unit.server._execution_test_support import insert_account
from tests.unit.server._native_read_views import account_view


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


def test_replay_recording_models_default_false_and_patch_preserves_omission():
    account = Account(**account_data())
    assert account.replay_recording_enabled is False
    assert AccountSnapshot.model_validate(account.model_dump()).replay_recording_enabled is False
    assert account_view(account)["replay_recording_enabled"] is False
    create_data = account_data()
    create_data.pop("id")
    assert AccountCreate(**create_data).replay_recording_enabled is False
    assert AccountUpdate().model_dump(exclude_unset=True) == {}
    assert AccountUpdate(replay_recording_enabled=False).model_dump(exclude_unset=True) == {
        "replay_recording_enabled": False
    }
    assert AccountUpdate(replay_recording_enabled=True).replay_recording_enabled is True
    assert account.replay_config_version == 0
    snapshot = AccountSnapshot.model_validate(account_data() | {"runtime_sync": {"revision": 7}})
    assert snapshot.model_dump()["replay_config_version"] == 7
    snapshot = AccountSnapshot.model_validate(
        account_data() | {"runtime_sync": {"revision": 7}, "replay_config_version": 8}
    )
    assert snapshot.replay_config_version == 8
    assert "replay_config_version" not in account_view(account)
    assert "replay_config_version" not in AccountCreate.model_fields


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_replay_recording_models_reject_non_boolean(value):
    account = Account(**account_data()).model_dump()
    for model in (Account, AccountSnapshot):
        with pytest.raises(ValidationError, match="replay_recording_enabled"):
            model.model_validate(account | {"replay_recording_enabled": value})
    with pytest.raises(ValidationError, match="replay_recording_enabled"):
        AccountUpdate(replay_recording_enabled=value)


def test_replay_recording_native_group_patch_and_legacy_default(native_database):
    saved = native_store_call(
        native_database, "account_create", {"account": account_data(), "now": "2026-09-30T10:00:00"}
    )["account"]
    identity = saved["id"]
    assert saved["replay_recording_enabled"] is False
    result = native_store_call(
        native_database,
        "account_update",
        {"account_id": identity, "patch": {"replay_recording_enabled": True}, "now": "2026-09-30T10:01:00"},
    )
    assert result["runtime_changed"] is False
    assert result["account"]["replay_recording_enabled"] is True
    assert result["account"]["algorithm"] == account_data()["algorithm"]
    with closing(sqlite3.connect(native_database)) as connection, connection:
        assert "replay_recording_enabled" not in {row[1] for row in connection.execute("PRAGMA table_info(account)")}
        assert connection.execute(
            "SELECT json_extract(execution,'$.replay_recording_enabled') FROM account_settings WHERE account_id=?",
            (identity,),
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE account_settings SET execution=json_remove(execution,'$.replay_recording_enabled') WHERE account_id=?",
            (identity,),
        )
    assert (
        native_store_call(native_database, "account_get", {"account_id": identity})["replay_recording_enabled"] is False
    )


def test_replay_recording_copy_defaults_off(native_database):
    saved = native_store_call(
        native_database,
        "account_create",
        {
            "account": account_data() | {"is_started": False, "replay_recording_enabled": True},
            "now": "2026-09-30T10:00:00",
        },
    )["account"]
    copied = native_store_call(
        native_database,
        "account_copy",
        {"account_id": saved["id"], "name": "复制账户", "now": "2026-09-30T10:01:00"},
    )
    assert copied["replay_recording_enabled"] is False
    assert (
        native_store_call(native_database, "account_get", {"account_id": saved["id"]})["replay_recording_enabled"]
        is True
    )


def test_settings_persist_patch_and_snapshot_are_independent(native_database):
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**account_data()))
    loaded = stored_account(native_database)
    snapshot = AccountSnapshot.model_validate(loaded.model_dump())
    assert isinstance(snapshot, AccountSnapshot)
    snapshot.account_config["nested"]["secret"] = "changed"
    assert loaded.account_config["nested"]["secret"] == "test-only"
    patch = {"long_leverage": 3, "execution_notification_code": None}
    loaded = Account.model_validate(loaded.model_dump() | patch)
    assert loaded.is_started
    assert loaded.feishu_key == "test-key"
    assert loaded.execution_notification_code is None
    with pytest.raises(ValidationError):
        Account.model_validate(loaded.model_dump() | {"weight_precision": -1, "cron_expr": "bad cron"})
    with pytest.raises(ValidationError):
        AccountUpdate(name="invalid", weight_precision=-1, cron_expr="bad cron")
    assert loaded.name == "账户"
    assert loaded.weight_precision == 0.01
    native_store_call(
        native_database, "account_update", {"account_id": 1, "patch": patch, "now": "2026-09-30T12:00:00"}
    )
    account = stored_account(native_database)
    assert account.long_leverage == 3
    assert account.execution_notification_code is None
    with closing(sqlite3.connect(native_database)) as connection:
        assert "algorithm" not in {row[1] for row in connection.execute("PRAGMA table_info(account)")}
    assert account.model_dump()["algorithm"] == account_data()["algorithm"]


def test_success_state_only_advances_and_deleted_account_is_ignored(monkeypatch, native_database):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**account_data()))
    notification_state.record_notification_result(1, "exec-new", "2026-09-30T10:01:00", True, None)
    notification_state.record_notification_result(1, "exec-old", "2026-09-30T10:00:00", True, None)
    account = stored_account(native_database)
    assert account.notification_state.execution_id == "exec-new"
    assert account.notification_state.last_success_at == utc_time("2026-09-30T10:01:00")
    native_store_call(native_database, "account_delete", {"account_id": 1})
    replacement = native_store_call(
        native_database, "account_create", {"account": account_data(), "now": "2026-09-30T10:01:30"}
    )["account"]
    assert replacement["id"] > 1
    notification_state.record_notification_result(1, "exec-deleted", "2026-09-30T10:02:00", True, None)
    with closing(sqlite3.connect(native_database)) as connection:
        assert connection.execute("SELECT * FROM account_notification_state").fetchall() == []
        assert connection.execute("SELECT account_id FROM account_settings").fetchall() == [(replacement["id"],)]


def test_public_response_exposes_success_summary_without_credentials():
    account = Account(**account_data())
    account.notification_state = AccountNotificationState(
        account_id=1, execution_id="exec-1", last_success_at="2026-09-30T10:00:00"
    )
    public = account_view(account)
    assert public["notification_state"]["execution_id"] == "exec-1"
    assert public["notification_state"]["last_success_at"] == "2026-09-30T10:00:00"
    assert public["notification_state"]["last_attempt_ok"] is None
    assert "account_config" not in public
    assert "feishu_key" not in public
    assert "test-only" not in str(public)
    assert "test-key" not in str(public)
    assert account.model_dump(include={"name", "feishu_key"}) == {"name": "账户", "feishu_key": "test-key"}
    assert "remark" not in account.model_dump(exclude_none=True)
    before = account.model_dump()
    with pytest.raises(ValueError):
        Account.model_validate(before | {"long_leverage": 3, "account_config": None})
    assert account.model_dump() == before


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


def test_gm_sessions_persist_and_roundtrip_in_public_snapshot(native_database):
    from axile.channels.gm_sessions import GMSessionSettings

    sessions = GMSessionSettings(opening_cancelable={"enabled": True, "time": "09:15"})
    data = account_data() | {"trade_channel": "gm", "gm_sessions": sessions.model_dump()}
    created = native_store_call(
        native_database,
        "account_create",
        {"account": Account(**data).model_dump(mode="json"), "now": "2026-09-01T09:00:00+08:00"},
    )
    account_id = created["account"]["id"]
    loaded = Account.model_validate(native_store_call(native_database, "account_get", {"account_id": account_id}))
    assert loaded.gm_sessions == sessions
    assert loaded.channel_options == {"gm": sessions.model_dump()}
    assert AccountSnapshot.model_validate(loaded.model_dump()).gm_sessions == sessions
    scheduled = native_store_call(native_database, "runtime_sync_scheduler_account", {"account_id": account_id})
    assert scheduled["gm_sessions"] == sessions.model_dump()
    native_store_call(
        native_database,
        "account_update",
        {"account_id": account_id, "patch": {"gm_sessions": None}, "now": "2026-09-01T09:01:00+08:00"},
    )
    assert native_store_call(native_database, "account_get", {"account_id": account_id})["gm_sessions"] is None


def test_legacy_schedule_json_is_read_and_rewritten_as_channel_options(native_database):
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, Account(**(account_data() | {"trade_channel": "gm"})))
        connection.execute(
            "UPDATE account_settings SET schedule=?",
            (json.dumps({"gm_sessions": {"opening_locked": {"enabled": True, "time": "09:24"}}}),),
        )
    account = stored_account(native_database)
    assert account.gm_sessions.opening_locked.time == "09:24"
    native_store_call(
        native_database,
        "account_update",
        {"account_id": 1, "patch": {"cron_expr": "30 9 * * *"}, "now": "2026-09-30T12:00:00"},
    )
    with closing(sqlite3.connect(native_database)) as connection:
        stored = json.loads(connection.execute("SELECT schedule FROM account_settings").fetchone()[0])
    assert "gm_sessions" not in stored
    assert stored["channel_options"]["gm"]["opening_locked"] == {"enabled": True, "time": "09:24"}


@pytest.mark.parametrize(
    "options",
    [
        {"gm": {"unknown": True}},
        {"gm": {"opening_locked": {"time": "09:25"}}},
        {"gm": {"after_hours": {"enabled": True, "time": "15:05"}}},
        {"ctp": {}},
        {"missing-channel": {}},
        {" GM ": {}},
        {"GM": {}, "gm": {}},
        None,
        [],
        "bad",
        1,
    ],
)
def test_snapshot_channel_options_do_not_bypass_plugin_validation(options):
    with pytest.raises(ValidationError):
        AccountSnapshot.model_validate(account_data() | {"channel_options": options})


def test_snapshot_channel_options_restore_legacy_model_and_reject_conflicts():
    snapshot = AccountSnapshot.model_validate(
        account_data() | {"channel_options": {"gm": {"opening_locked": {"enabled": True, "time": "09:24"}}}}
    )
    assert snapshot.gm_sessions.opening_locked.time == "09:24"
    assert snapshot.channel_options["gm"]["opening_locked"] == {"enabled": True, "time": "09:24"}
    with pytest.raises(ValidationError, match="冲突"):
        AccountSnapshot.model_validate(
            account_data() | {"channel_options": {"gm": {}}, "gm_sessions": {"opening_locked": {"time": "09:24"}}}
        )
