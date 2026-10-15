"""连接历史迁移保留旧故障、主动断开意愿，并支持往返降级。"""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_connection_history_migration_preserves_evidence_without_inventing_access_success():
    revision = import_module("axile.server.alembic.versions.0028_connection_history")
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as db:
            db.execute(
                text("""CREATE TABLE account_connection_state (
                account_id INTEGER PRIMARY KEY, status TEXT, observed_at TEXT, message TEXT,
                label TEXT, error JSON, presentation JSON)""")
            )
            db.execute(
                text("""INSERT INTO account_connection_state VALUES
                (1, 'error', 'same-time', 'original failure', 'custom label', '{"technical_detail":"native error"}', NULL),
                (2, 'ok', 'same-time', NULL, NULL, NULL, NULL),
                (3, 'disconnected', 'old-time', NULL, NULL, NULL, NULL)""")
            )
            with Operations.context(MigrationContext.configure(db)):
                revision.upgrade()
            rows = db.execute(text("SELECT * FROM account_connection_state ORDER BY account_id")).mappings().all()
            import json

            assert json.loads(rows[0]["last_failure"])["error"]["technical_detail"] == "native error"
            assert rows[1]["last_failure"] is None
            assert all(row["last_success_at"] is None for row in rows)
            assert [row["manually_disconnected"] for row in rows] == [0, 0, 1]
            with Operations.context(MigrationContext.configure(db)):
                revision.downgrade()
            assert "last_failure" not in {col["name"] for col in inspect(db).get_columns("account_connection_state")}
    finally:
        engine.dispose()
