"""连接历史迁移保留旧故障、主动断开意愿，并支持往返降级。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.conftest import initialize_native_database, native_store_call


def test_connection_history_migration_preserves_evidence_without_inventing_access_success(tmp_path: Path) -> None:
    path = tmp_path / "connection-history.db"
    initialize_native_database(path, revision="0027")
    with closing(sqlite3.connect(path)) as db, db:
        db.executemany(
            "INSERT INTO account (id, name, market, trade_channel, brokerage, updated_at, created_at) "
            "VALUES (?, 'account', 'future', 'ctp', '', 'now', 'now')",
            ((1,), (2,), (3,)),
        )
        db.execute(
            """INSERT INTO account_connection_state
            (account_id, config_key, instance_id, sequence, status, observed_at, message, label, error, presentation)
            VALUES
            (1, 'config', 'instance', 1, 'error', 'same-time', 'original failure', 'custom label', '{"technical_detail":"native error"}', NULL),
            (2, 'config', 'instance', 1, 'ok', 'same-time', NULL, NULL, NULL, NULL),
            (3, 'config', 'instance', 1, 'disconnected', 'old-time', NULL, NULL, NULL, NULL)"""
        )
    initialize_native_database(path, revision="0028")
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM account_connection_state ORDER BY account_id").fetchall()
        assert json.loads(rows[0]["last_failure"])["error"]["technical_detail"] == "native error"
        assert rows[1]["last_failure"] is None
        assert all(row["last_success_at"] is None for row in rows)
        assert [row["manually_disconnected"] for row in rows] == [0, 0, 1]
    native_store_call(path, "migration_downgrade", {"public": "0027"})
    with closing(sqlite3.connect(path)) as db:
        assert "last_failure" not in {column[1] for column in db.execute("PRAGMA table_info(account_connection_state)")}
