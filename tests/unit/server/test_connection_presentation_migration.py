"""迁移保留 0025 旧故障，新字段可空且可独立回滚。"""

import sqlite3
from contextlib import closing
from pathlib import Path

from tests.conftest import initialize_native_database, native_store_call


def test_upgrade_old_connection_record_and_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    initialize_native_database(path, revision="0025")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, brokerage, updated_at, created_at) "
            "VALUES (1, 'account', 'future', 'ctp', '', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO account_connection_state "
            "(account_id, config_key, instance_id, sequence, status, message, error) "
            "VALUES (1, 'config', 'instance', 1, 'error', '旧故障原文', '{}')"
        )
    initialize_native_database(path, revision="0026")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM account_connection_state").fetchone()
        assert row["label"] is None and row["presentation"] is None
        assert row["message"] == "旧故障原文"
        connection.execute(
            "UPDATE account_connection_state SET label = '自定义标签', presentation = ?",
            ('{"title":"动态详情"}',),
        )
        assert connection.execute("SELECT label FROM account_connection_state").fetchone()[0] == "自定义标签"
    native_store_call(path, "migration_downgrade", {"public": "0025"})
    with closing(sqlite3.connect(path)) as connection:
        names = {column[1] for column in connection.execute("PRAGMA table_info(account_connection_state)")}
        assert "label" not in names and "presentation" not in names
        assert connection.execute("SELECT message FROM account_connection_state").fetchone()[0] == "旧故障原文"
