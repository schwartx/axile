"""旧平仓规则的无静默冲突迁移与账户设置持久化。"""

import json
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import Session, SQLModel

from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.server.api.routes.account_crud import _account_public
from axile.server.db.models import Account
from tests.unit.server.test_account_settings import account_data

revision = import_module("axile.server.alembic.versions.0029_futures_close_policy")


def test_migration_removes_legacy_fields_and_preserves_explicit_global_choice():
    old = {
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "今昨", "max_wait_seconds": 60}},
        "trade_rules": {
            "ag2611": {"offset_priority": "昨今", "sizing_mode": "lots"},
            "ag2612": {"offset_priority": "昨今"},
        },
    }
    result = revision.migrate_execution(old, 1)
    assert result["futures_close_policy"]["overrides"]["AG"] == "yesterday_first"
    assert result["futures_close_policy"]["overrides"]["RB"] == "today_first"
    assert result["futures_close_policy"]["fallback"] == "today_first"
    assert result["algorithm"]["params"] == {"max_wait_seconds": 60}
    assert result["trade_rules"]["ag2611"] == {"sizing_mode": "lots"}
    assert "offset_priority" in str(old)
    assert revision.migrate_execution(result, 1) == result


def test_migration_checks_all_accounts_before_mutating_database():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE account (id INTEGER PRIMARY KEY, trade_channel TEXT)")
        connection.exec_driver_sql("INSERT INTO account VALUES (1, 'ctp'), (2, 'ctp')")
        connection.exec_driver_sql("CREATE TABLE account_settings (account_id INTEGER PRIMARY KEY, execution JSON)")
        old = {"trade_rules": {"ag2611": {"offset_priority": "昨今"}, "ag2612": {"offset_priority": "今昨"}}}
        for account_id, data in ((1, {}), (2, old)):
            connection.execute(
                sa.text("INSERT INTO account_settings VALUES (:id, :data)"),
                {"id": account_id, "data": json.dumps(data)},
            )
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(ValueError, match="不同月份"):
            revision.upgrade()
        assert connection.scalar(sa.text("SELECT execution FROM account_settings WHERE account_id=1")) == "{}"
        connection.execute(sa.text("DELETE FROM account_settings WHERE account_id=2"))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        assert (
            json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings")))["futures_close_policy"][
                "mode"
            ]
            == "rules"
        )
    engine.dispose()


def test_account_policy_survives_json_settings_public_and_worker_snapshot():
    engine = sa.create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    policy = FuturesClosePolicy(overrides={"ag": "today_first"})
    with Session(engine) as session:
        account = Account(**(account_data() | {"futures_close_policy": policy}))
        session.add(account)
        session.commit()
        session.refresh(account)
        assert account.futures_close_policy.overrides == {"AG": "today_first"}
        assert account.snapshot().futures_close_policy == policy
        assert _account_public(account).futures_close_policy == policy
    engine.dispose()


def test_downgrade_restores_snapshot_but_rejects_changed_policy_before_writes():
    engine = sa.create_engine("sqlite://")
    old = {"algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "昨今"}}}
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE account (id INTEGER PRIMARY KEY, trade_channel TEXT)")
        connection.exec_driver_sql("INSERT INTO account VALUES (1, 'ctp'), (2, 'ctp')")
        connection.exec_driver_sql("CREATE TABLE account_settings (account_id INTEGER PRIMARY KEY, execution JSON)")
        connection.execute(sa.text("INSERT INTO account_settings VALUES (1, :data)"), {"data": json.dumps(old)})
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        migrated = connection.scalar(sa.text("SELECT execution FROM account_settings"))
        changed = json.loads(migrated)
        changed["futures_close_policy"]["overrides"] = {"AG": "today_first"}
        connection.execute(sa.text("UPDATE account_settings SET execution=:data"), {"data": json.dumps(changed)})
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(ValueError, match="不能无损降级"),
        ):
            revision.downgrade()
        assert "_futures_close_policy_backup" in sa.inspect(connection).get_table_names()
        assert json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings"))) == changed
        connection.execute(sa.text("UPDATE account_settings SET execution=:data"), {"data": migrated})
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
        assert json.loads(connection.scalar(sa.text("SELECT execution FROM account_settings"))) == old
    engine.dispose()


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
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException, Response

    from axile.server.api.routes import account_crud
    from axile.server.db.models import AccountUpdate

    account = Account(
        **(
            account_data()
            | {
                "trade_channel": "tq",
                "account_config": {"account_mode": "sim", "tq_username": "fixture", "tq_password": "fixture"},
            }
        )
    )

    async def get_account(*args):
        return account

    monkeypatch.setattr(account_crud, "_get_account_or_404", get_account)
    session = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            account_crud._update_account(
                session, None, 1, AccountUpdate(futures_close_policy={"mode": "fee"}), Response()
            )
        )
    assert raised.value.status_code == 422
    assert "不支持低手续费优先" in str(raised.value.detail)
    session.commit.assert_not_called()
    assert account.futures_close_policy.mode == "rules"


def test_migration_does_not_remove_private_algorithm_parameters():
    raw = {"algorithm": {"method": "PRIVATE", "params": {"offset_priority": "custom"}}}
    assert revision.migrate_execution(raw, 1, "external")["algorithm"] == raw["algorithm"]
