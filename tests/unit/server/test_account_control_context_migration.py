"""上下文迁移识别历史操作且不改写原始审计、计数和唯一身份。"""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_context_migration_classifies_known_ids_preserves_unknowns_and_roundtrips(tmp_path):
    revision = import_module("axile.server.alembic.versions.0027_account_control_context")
    engine = create_engine(f"sqlite:///{tmp_path / 'old-control.db'}")
    identities = [
        ("orders-" + "a" * 32, "account_operation", "current_orders"),
        ("catalog-" + "b" * 32, "account_operation", "instrument_catalog"),
        ("orders-" + "c" * 32, "execution", "unknown"),
        ("catalog-" + "d" * 32, "execution", "unknown"),
        ("orders-unrecognized", "unknown", "unknown"),
        ("Orders-" + "e" * 32, "unknown", "unknown"),
        ("unclassified", "unknown", "unknown"),
    ]
    with engine.begin() as connection:
        for table in ("execution_intent", "executerecord", "execution_event"):
            connection.execute(text(f"CREATE TABLE {table} (account_id INTEGER, execution_id TEXT)"))
        connection.execute(text("CREATE TABLE execution_artifact (execution_id TEXT)"))
        connection.execute(text("INSERT INTO execution_intent VALUES (1, :key)"), {"key": identities[2][0]})
        connection.execute(text("INSERT INTO execution_artifact VALUES (:key)"), {"key": identities[3][0]})
        for table in revision._TABLES:
            connection.execute(
                text(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, account_id INTEGER, execution_id TEXT UNIQUE, amount INTEGER)"
                )
            )
            connection.execute(
                text(f"INSERT INTO {table} (account_id, execution_id, amount) VALUES (1, :key, 7)"),
                [{"key": key} for key, _, _ in identities],
            )
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            for table in revision._TABLES:
                rows = connection.execute(
                    text(f"SELECT execution_id, context_kind, source FROM {table} ORDER BY id")
                ).all()
                assert rows == identities
                assert connection.execute(text(f"SELECT SUM(amount) FROM {table}")).scalar_one() == 7 * len(identities)
            revision.downgrade()
            for table in revision._TABLES:
                assert {row["name"] for row in inspect(connection).get_columns(table)} == {
                    "id",
                    "account_id",
                    "execution_id",
                    "amount",
                }
                assert connection.execute(text(f"SELECT execution_id FROM {table} ORDER BY id")).scalars().all() == [
                    key for key, _, _ in identities
                ]
                assert inspect(connection).get_unique_constraints(table)[0]["column_names"] == ["execution_id"]
            revision.upgrade()
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM account_control_event WHERE context_kind = 'account_operation'")
                ).scalar_one()
                == 2
            )
    engine.dispose()
