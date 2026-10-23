"""旧平仓规则的无静默冲突迁移与账户设置持久化。"""

import json

import pytest
import sqlalchemy as sa

from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.server.api.routes.account_crud import _account_public
from axile.server.db.models import Account, AccountCreate
from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.unit.server.test_analysis_structured_migration import _connect, _downgrade, _upgrade


def _account_data():
    return {
        "id": 1,
        "name": "账户",
        "market": "期货",
        "trade_channel": "ctp",
        "brokerage": "test",
        "remark": None,
        "account_config": {"nested": {"secret": "test-only"}},
        "account_control_preset": "default",
        "is_started": True,
        "cron_expr": "0 9 * * *",
        "weight_precision": 0.01,
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "feishu_key": "test-key",
        "execution_notification_code": "def notify(context):\n    pass",
        "trade_rules": None,
        "forbidden_symbols": None,
        "risk_symbols": None,
    }


def _legacy_database(tmp_path, executions, *, channel="ctp"):
    database = tmp_path / "futures-close.db"
    initialize_native_database(database, revision="0028")
    with _connect(database) as connection:
        for account_id, execution in executions.items():
            connection.execute(
                sa.text(
                    "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
                    "VALUES(:id,'synthetic','FUTURES',:channel,'','2026-01-01','2026-01-01')"
                ),
                {"id": account_id, "channel": channel},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) "
                    "VALUES(:id,'{}',:execution,'{}','{}','{}')"
                ),
                {"id": account_id, "execution": json.dumps(execution)},
            )
    return database


def test_migration_removes_legacy_fields_and_preserves_explicit_global_choice(tmp_path):
    old = {
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "今昨", "max_wait_seconds": 60}},
        "trade_rules": {
            "ag2611": {"offset_priority": "昨今", "sizing_mode": "lots"},
            "ag2612": {"offset_priority": "昨今"},
        },
    }
    database = _legacy_database(tmp_path, {1: old})
    _upgrade(database, "0029")
    with _connect(database) as connection:
        result = json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings WHERE account_id=1")))
    assert result["futures_close_policy"]["overrides"]["AG"] == "yesterday_first"
    assert result["futures_close_policy"]["overrides"]["RB"] == "today_first"
    assert result["futures_close_policy"]["fallback"] == "today_first"
    assert result["algorithm"]["params"] == {"max_wait_seconds": 60}
    assert result["trade_rules"]["ag2611"] == {"sizing_mode": "lots"}
    assert "offset_priority" in str(old)
    _upgrade(database, "0029")
    with _connect(database) as connection:
        assert (
            json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings WHERE account_id=1")))
            == result
        )


def test_migration_checks_all_accounts_before_mutating_database(tmp_path):
    old = {"trade_rules": {"ag2611": {"offset_priority": "昨今"}, "ag2612": {"offset_priority": "今昨"}}}
    database = _legacy_database(tmp_path, {1: {}, 2: old})
    with pytest.raises(require_native_store("migration_upgrade").NativeError, match="不同月份"):
        _upgrade(database, "0029")
    with _connect(database) as connection:
        assert connection.scalar(sa.text("SELECT execution FROM account_settings WHERE account_id=1")) == "{}"
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0028"
        assert "_futures_close_policy_backup" not in sa.inspect(connection).get_table_names()
        connection.execute(sa.text("DELETE FROM account_settings WHERE account_id=2"))
    _upgrade(database, "0029")
    with _connect(database) as connection:
        assert (
            json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings")))["futures_close_policy"][
                "mode"
            ]
            == "rules"
        )


def test_account_policy_survives_json_settings_public_and_worker_snapshot(native_database):
    policy = FuturesClosePolicy(overrides={"ag": "today_first"})
    data = _account_data() | {"futures_close_policy": policy}
    data.pop("id")
    created = native_store_call(
        native_database,
        "account_create",
        {
            "account": AccountCreate(**data).model_dump(mode="json"),
            "now": "2026-01-01T09:00:00",
        },
    )
    stored = native_store_call(native_database, "account_get", {"account_id": created["account"]["id"]})
    account = Account.model_validate(stored)
    assert account.futures_close_policy.overrides == {"AG": "today_first"}
    assert account.snapshot().futures_close_policy == policy
    assert _account_public(account).futures_close_policy == policy


def test_downgrade_restores_snapshot_but_rejects_changed_policy_before_writes(tmp_path):
    old = {"algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "昨今"}}}
    database = _legacy_database(tmp_path, {1: old})
    _upgrade(database, "0029")
    with _connect(database) as connection:
        migrated = connection.scalar(sa.text("SELECT execution FROM account_settings"))
        changed = json.loads(migrated)
        changed["futures_close_policy"]["overrides"] = {"AG": "today_first"}
        connection.execute(sa.text("UPDATE account_settings SET execution=:data"), {"data": json.dumps(changed)})
    with pytest.raises(require_native_store("migration_downgrade").NativeError, match="不能无损降级"):
        _downgrade(database, "0028")
    with _connect(database) as connection:
        assert "_futures_close_policy_backup" in sa.inspect(connection).get_table_names()
        assert json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings"))) == changed
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0029"
        connection.execute(sa.text("UPDATE account_settings SET execution=:data"), {"data": migrated})
    _downgrade(database, "0028")
    with _connect(database) as connection:
        assert json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings"))) == old
        assert "_futures_close_policy_backup" not in sa.inspect(connection).get_table_names()


def test_public_capability_keeps_modes_and_supplied_fee_columns():
    from axile.server.api.routes.capabilities import list_channel_capabilities

    channels = {row.channel: row.model_dump(mode="json") for row in list_channel_capabilities()}
    assert channels["ctp"]["futures_close"]["modes"] == ["rules", "fee"]
    assert channels["tq"]["futures_close"]["modes"] == ["rules"]
    ag = next(p for p in channels["tq"]["futures_close"]["presets"] if p["product"] == "AG")
    assert (
        ag["yesterday_fee"] == ag["today_fee"] == "万分之0.5（AG2610—2612、2706）；万分之0.1（AG2701—2705、2707—2709）"
    )
    zn = next(p for p in channels["ctp"]["futures_close"]["presets"] if p["product"] == "ZN")
    assert zn["priority"] == "today_first"
    assert zn["yesterday_fee"] == "3元/手" and zn["today_fee"] == "免收"


def test_real_account_patch_rejects_tq_automatic_before_any_commit(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from fastapi import HTTPException, Response

    from axile.server.api.routes import account_crud
    from axile.server.db.models import AccountUpdate

    account = Account(
        **(
            _account_data()
            | {
                "trade_channel": "tq",
                "account_config": {"account_mode": "sim", "tq_username": "fixture", "tq_password": "fixture"},
            }
        )
    )

    async def get_account(*args):
        return account

    monkeypatch.setattr(account_crud, "_get_account_or_404", get_account)
    persist = AsyncMock()
    monkeypatch.setattr(account_crud, "_account_operation", persist)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            account_crud._update_account(None, 1, AccountUpdate(futures_close_policy={"mode": "fee"}), Response())
        )
    assert raised.value.status_code == 422
    assert "不支持低手续费优先" in str(raised.value.detail)
    persist.assert_not_called()
    assert account.futures_close_policy.mode == "rules"


def test_migration_does_not_remove_private_algorithm_parameters(tmp_path):
    raw = {"algorithm": {"method": "PRIVATE", "params": {"offset_priority": "custom"}}}
    database = _legacy_database(tmp_path, {1: raw}, channel="external")
    _upgrade(database, "0029")
    with _connect(database) as connection:
        result = json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings")))
    assert result["algorithm"] == raw["algorithm"]
