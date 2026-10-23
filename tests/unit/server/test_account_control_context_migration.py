"""上下文迁移识别历史操作且不改写原始审计、计数和唯一身份。"""

import sqlite3
from contextlib import closing
from pathlib import Path

from tests.conftest import initialize_native_database, native_store_call

_TABLES = ("account_control_event", "account_control_counter_delta", "account_control_session")


def test_context_migration_classifies_known_ids_preserves_unknowns_and_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "old-control.db"
    initialize_native_database(path, revision="0026")
    identities = [
        ("orders-" + "a" * 32, "account_operation", "current_orders"),
        ("catalog-" + "b" * 32, "account_operation", "instrument_catalog"),
        ("orders-" + "c" * 32, "execution", "unknown"),
        ("catalog-" + "d" * 32, "execution", "unknown"),
        ("orders-unrecognized", "unknown", "unknown"),
        ("Orders-" + "e" * 32, "unknown", "unknown"),
        ("unclassified", "unknown", "unknown"),
    ]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO account (id, name, market, trade_channel, brokerage, updated_at, created_at) "
            "VALUES (1, 'account', 'future', 'ctp', '', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO execution_intent (account_id, execution_id, kind, trigger_source, status, payload, created_at) "
            "VALUES (1, ?, 'execution', 'manual', 'completed', '{}', 'now')",
            (identities[2][0],),
        )
        connection.execute(
            "INSERT INTO execution_artifact (execution_id, artifact_type, schema_version, content, created_at) "
            "VALUES (?, 'ACCOUNT_SNAPSHOT', 1, '{}', 'now')",
            (identities[3][0],),
        )
        columns_before = {}
        for table in _TABLES:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN amount INTEGER")
            connection.execute(f"CREATE UNIQUE INDEX test_{table}_execution_id ON {table} (execution_id)")
            columns_before[table] = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for index, (key, _, _) in enumerate(identities):
            connection.execute(
                "INSERT INTO account_control_event "
                "(account_id, control_date, execution_id, seq, channel, operation, metadata, decision, "
                "counted, outcome, event_uid, created_at, occurred_at_ms, amount) "
                "VALUES (1, '2026-10-23', ?, ?, 'ctp', 'query', '{}', 'allow', 1, 'sent', ?, 'now', 0, 7)",
                (key, index, key),
            )
            connection.execute(
                "INSERT INTO account_control_counter_delta "
                "(account_id, execution_id, control_date, bucket_type, bucket_start, scope_type, "
                "operation, delta_count, delta_uid, amount) "
                "VALUES (1, ?, '2026-10-23', 'daily', 'now', 'account', 'query', 7, ?, 7)",
                (key, key),
            )
            connection.execute(
                "INSERT INTO account_control_session "
                "(instance_id, account_id, execution_id, timezone, started_at_ms, status, amount) "
                "VALUES (?, 1, ?, 'Asia/Shanghai', 0, 'completed', 7)",
                (key, key),
            )
    initialize_native_database(path, revision="0027")
    with closing(sqlite3.connect(path)) as connection:
        for table in _TABLES:
            rows = connection.execute(f"SELECT execution_id, context_kind, source FROM {table} ORDER BY id").fetchall()
            assert rows == identities
            assert connection.execute(f"SELECT SUM(amount) FROM {table}").fetchone()[0] == 7 * len(identities)
    native_store_call(path, "migration_downgrade", {"public": "0026"})
    with closing(sqlite3.connect(path)) as connection:
        for table in _TABLES:
            assert {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} == columns_before[table]
            assert [row[0] for row in connection.execute(f"SELECT execution_id FROM {table} ORDER BY id")] == [
                key for key, _, _ in identities
            ]
            unique_columns = [
                [column[2] for column in connection.execute(f"PRAGMA index_info({index[1]})")]
                for index in connection.execute(f"PRAGMA index_list({table})")
                if index[2]
            ]
            assert ["execution_id"] in unique_columns
    initialize_native_database(path, revision="0027")
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM account_control_event WHERE context_kind = 'account_operation'"
            ).fetchone()[0]
            == 2
        )
