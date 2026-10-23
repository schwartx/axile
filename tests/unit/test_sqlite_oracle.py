"""测试侧跨进程 SQL 保留事务、原始字段类型和异常。"""

import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from tests import sqlite_oracle as oracle_transport
from tests.sqlite_oracle import connect, sqlite_oracle


@sqlite_oracle
def _insert(path, duplicate=False):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, value BLOB)")
        connection.execute("INSERT INTO facts VALUES(?,?)", (2 if duplicate else 1, b"\x00\xff"))
        if duplicate:
            connection.execute("INSERT INTO facts VALUES(1,NULL)")
    return os.getpid()


@sqlite_oracle
async def _read(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT id,value FROM facts ORDER BY id").fetchall()


def test_isolated_sql_preserves_rows_and_rolls_back_integrity_failures(tmp_path):
    path = tmp_path / "oracle.db"
    assert _insert(path) != os.getpid()
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(path, duplicate=True)
    rows = asyncio.run(_read(path))
    assert len(rows) == 1
    assert isinstance(rows[0], sqlite3.Row)
    assert rows[0][0] == rows[0]["id"] == 1
    assert rows[0]["value"] == b"\x00\xff"


def test_explicit_oracle_connection_keeps_transaction_and_cursor_lifetimes(tmp_path):
    with closing(connect(tmp_path / "connection.db")) as connection:
        connection.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
        with connection:
            connection.executemany("INSERT INTO facts VALUES(?)", [(1,), (2,)])
            assert connection.in_transaction
        assert not connection.in_transaction
        connection.row_factory = sqlite3.Row
        cursor = connection.execute("SELECT id FROM facts ORDER BY id")
        assert cursor.description[0][0] == "id"
        assert cursor.fetchone()["id"] == 1
        assert [row[0] for row in cursor.fetchall()] == [2]
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute("INSERT INTO facts VALUES(3)")
                connection.execute("INSERT INTO facts VALUES(1)")
        with pytest.raises(ValueError, match="abort context"):
            with connection:
                connection.execute("INSERT INTO facts VALUES(3)")
                raise ValueError("abort context")
        assert [row[0] for row in connection.execute("SELECT id FROM facts ORDER BY id")] == [1, 2]
        connection.executescript(
            "CREATE TABLE audit(id INTEGER);"
            "CREATE TRIGGER record_fact AFTER INSERT ON facts BEGIN INSERT INTO audit VALUES(NEW.id); END;"
        )
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
        with connection:
            connection.execute("INSERT INTO facts VALUES(3)")
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, True)
        with connection:
            connection.execute("INSERT INTO facts VALUES(4)")
        assert [row[0] for row in connection.execute("SELECT id FROM audit")] == [4]


@sqlite_oracle
def _typed_rows(path):
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{path}")
    table = sa.Table(
        "typed",
        sa.MetaData(),
        sa.Column("payload", sa.JSON),
        sa.Column("created_at", sa.DateTime),
        sa.Column("enabled", sa.Boolean),
    )
    try:
        table.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(payload={"zero": 0, "null": None}, created_at=datetime(2026, 1, 2), enabled=False)
            )
            return [dict(row) for row in connection.execute(sa.select(table)).mappings()]
    finally:
        engine.dispose()


def test_sqlalchemy_types_survive_transport_and_child_is_terminated(tmp_path):
    assert _typed_rows(tmp_path / "typed.db") == [
        {"payload": {"zero": 0, "null": None}, "created_at": datetime(2026, 1, 2), "enabled": False}
    ]
    process = oracle_transport._worker
    oracle_transport._close_worker()
    assert process.poll() is not None
    assert oracle_transport._worker is None
