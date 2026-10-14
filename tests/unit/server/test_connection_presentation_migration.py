"""迁移保留 0025 旧故障，新字段可空且可独立回滚。"""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_upgrade_old_connection_record_and_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    previous = import_module("axile.server.alembic.versions.0025_account_connection_state")
    revision = import_module("axile.server.alembic.versions.0026_connection_presentation")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            previous.upgrade()
            connection.execute(
                text(
                    "INSERT INTO account_connection_state (account_id, config_key, instance_id, sequence, status, message, error) VALUES (1, 'config', 'instance', 1, 'error', '旧故障原文', '{}')"
                )
            )
            revision.upgrade()
            row = connection.execute(text("SELECT * FROM account_connection_state")).mappings().one()
            assert row["label"] is None and row["presentation"] is None
            assert row["message"] == "旧故障原文"
            connection.execute(
                text("UPDATE account_connection_state SET label='自定义标签', presentation=:presentation"),
                {"presentation": '{"title":"动态详情"}'},
            )
            assert connection.execute(text("SELECT label FROM account_connection_state")).scalar_one() == "自定义标签"
            revision.downgrade()
            names = {column["name"] for column in inspect(connection).get_columns("account_connection_state")}
            assert "label" not in names and "presentation" not in names
            assert connection.execute(text("SELECT message FROM account_connection_state")).scalar_one() == "旧故障原文"
    engine.dispose()
