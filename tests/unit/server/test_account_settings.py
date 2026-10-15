"""账户拆分的持久化、迁移与通知成功摘要契约。"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, select

from axile.server.db.models import Account, AccountNotificationState, AccountSettings, AccountSnapshot
from axile.server.db.models.account_settings import ExecutionSettings
from axile.server.execution import notification_state

MIGRATIONS = Path(__file__).parents[3] / "axile/server/alembic/versions"


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


def load_migration(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_persist_patch_and_snapshot_are_independent():
    engine = sa.create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        account = Account(**account_data())
        session.add(account)
        session.commit()
        session.expire_all()
        loaded = session.get(Account, 1)
        assert loaded.settings.account_id == 1
        snapshot = loaded.snapshot()
        assert isinstance(snapshot, AccountSnapshot)
        snapshot.account_config["nested"]["secret"] = "changed"
        assert loaded.account_config["nested"]["secret"] == "test-only"
        loaded.sqlmodel_update({"long_leverage": 3, "execution_notification_code": None})
        assert loaded.is_started
        assert loaded.feishu_key == "test-key"
        assert loaded.execution_notification_code is None
        with pytest.raises(ValidationError):
            loaded.sqlmodel_update({"name": "invalid", "weight_precision": -1, "cron_expr": "bad cron"})
        assert loaded.name == "账户"
        assert loaded.weight_precision == 0.01
        session.commit()
    with Session(engine) as session:
        account = session.get(Account, 1)
        assert account.long_leverage == 3
        assert account.execution_notification_code is None
        assert "algorithm" not in Account.__table__.columns
        assert isinstance(account.settings.execution, ExecutionSettings)
    engine.dispose()


def test_migration_preserves_all_settings_and_binding_rows():
    engine = sa.create_engine("sqlite://")
    migrations = [load_migration(path) for path in sorted(MIGRATIONS.glob("[0-9]*.py")) if path.name[:4] <= "0020"]
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for migration in migrations[:-1]:
                migration.upgrade()
            metadata = sa.MetaData()
            account = sa.Table("account", metadata, autoload_with=connection)
            values = Account(**account_data()).model_dump()
            values = {key: value for key, value in values.items() if key in account.c}
            values["trade_channel"] = "ctp"
            connection.execute(account.insert().values(**values))
            connection.execute(
                sa.text(
                    "INSERT INTO portfolioaccount (account_id, portfolio_id, created_at) VALUES (1, NULL, '2026-09-30')"
                )
            )
            before = dict(connection.execute(sa.select(account)).mappings().one())
            migrations[-1].upgrade()
            settings = sa.Table("account_settings", sa.MetaData(), autoload_with=connection)
            stored = connection.execute(sa.select(settings)).mappings().one()
            assert stored["connection"] == before["account_config"]
            assert stored["notification"]["execution_notification_code"] == before["execution_notification_code"]
            assert connection.scalar(sa.text("SELECT count(*) FROM portfolioaccount")) == 1
            assert "algorithm" not in {c["name"] for c in sa.inspect(connection).get_columns("account")}
            migrations[-1].downgrade()
            restored = sa.Table("account", sa.MetaData(), autoload_with=connection)
            assert dict(connection.execute(sa.select(restored)).mappings().one()) == before
            assert connection.scalar(sa.text("SELECT count(*) FROM portfolioaccount")) == 1
    engine.dispose()


def test_success_state_only_advances_and_deleted_account_is_ignored(monkeypatch):
    engine = sa.create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(notification_state, "_notification_engine", lambda: engine)
    with Session(engine) as session:
        session.add(Account(**account_data()))
        session.commit()
    notification_state.record_notification_result(1, "exec-new", "2026-09-30T10:01:00", True, None)
    notification_state.record_notification_result(1, "exec-old", "2026-09-30T10:00:00", True, None)
    with Session(engine) as session:
        account = session.get(Account, 1)
        assert account.notification_state.execution_id == "exec-new"
        assert account.notification_state.last_success_at == "2026-09-30T10:01:00"
        session.delete(account)
        session.commit()
    notification_state.record_notification_result(1, "exec-deleted", "2026-09-30T10:02:00", True, None)
    with Session(engine) as session:
        assert session.exec(select(AccountNotificationState)).all() == []
        assert session.exec(select(AccountSettings)).all() == []
    engine.dispose()


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
        account.sqlmodel_update({"long_leverage": 3, "account_config": None})
    assert account.settings.execution is before


def test_notification_result_preserves_success_and_ignores_older_results(monkeypatch):
    engine = sa.create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(notification_state, "_notification_engine", lambda: engine)
    with Session(engine) as session:
        session.add(Account(**account_data()))
        session.commit()
    notification_state.record_notification_result(1, "first-fail", "2026-09-30T09:00:00", False, "timeout")
    with Session(engine) as session:
        state = session.get(AccountNotificationState, 1)
        assert state.last_success_at is None
        assert state.last_attempt_ok is False
    notification_state.record_notification_result(1, "success", "2026-09-30T10:00:00", True, None)
    notification_state.record_notification_result(1, "failure", "2026-09-30T11:00:00", False, "timeout")
    notification_state.record_notification_result(1, "old", "2026-09-30T09:30:00", True, None)
    with Session(engine) as session:
        state = session.get(AccountNotificationState, 1)
        assert state.execution_id == "success"
        assert state.last_attempt_execution_id == "failure"
        assert state.last_attempt_ok is False
        assert state.last_attempt_error == "timeout"
    notification_state.record_notification_result(999, "missing", "2026-09-30T12:00:00", True, None)
    engine.dispose()


def test_notification_result_migration_preserves_legacy_success():
    engine = sa.create_engine("sqlite://")
    migrations = [load_migration(path) for path in sorted(MIGRATIONS.glob("[0-9]*.py")) if path.name[:4] <= "0021"]
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for migration in migrations[:-1]:
                migration.upgrade()
            connection.execute(
                sa.text(
                    "INSERT INTO account (id, name, market, trade_channel, brokerage, created_at, updated_at) VALUES (1, 'test', '期货', 'ctp', 0, 'now', 'now')"
                )
            )
            connection.execute(
                sa.text("INSERT INTO account_notification_state VALUES (1, '2026-09-30T10:00:00', 'old')")
            )
            migrations[-1].upgrade()
            connection.execute(
                sa.text(
                    "INSERT INTO account (id, name, market, trade_channel, brokerage, created_at, updated_at) VALUES (2, 'test2', '期货', 'ctp', 0, 'now', 'now')"
                )
            )
            connection.execute(
                sa.text("INSERT INTO account_notification_state (account_id, last_attempt_ok) VALUES (2, 0)")
            )
            assert (
                connection.scalar(sa.text("SELECT execution_id FROM account_notification_state WHERE account_id = 1"))
                == "old"
            )
            migrations[-1].downgrade()
            assert connection.scalar(sa.text("SELECT count(*) FROM account_notification_state")) == 1
    engine.dispose()
