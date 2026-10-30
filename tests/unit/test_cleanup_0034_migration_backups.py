"""独立清理脚本只删除指定版本的迁移备份，并保证原子性。"""

import runpy
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/cleanup_0034_migration_backups.py"
MODULE = runpy.run_path(str(SCRIPT))


@pytest.mark.parametrize("revision", ["0033", "0034"])
def test_cleanup_preserves_business_data_and_version(tmp_path, revision):
    database = tmp_path / "history.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.execute("CREATE TABLE business(value TEXT)")
        connection.execute("INSERT INTO business VALUES ('preserved')")
        for table in MODULE["BACKUP_TABLES"]:
            connection.execute(f'CREATE TABLE "{table}"(value TEXT)')
            connection.execute(f"INSERT INTO \"{table}\" VALUES ('historical')")
    if revision == "0033":
        before = database.read_bytes()
        with pytest.raises(ValueError, match="仅支持 0034"):
            MODULE["cleanup"](database)
        assert database.read_bytes() == before
    else:
        MODULE["cleanup"](database)
        MODULE["cleanup"](database)
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT * FROM business").fetchall() == [("preserved",)]
            assert connection.execute("SELECT * FROM alembic_version").fetchall() == [(revision,)]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            assert not tables.intersection(MODULE["BACKUP_TABLES"])


def test_missing_database_is_not_created(tmp_path):
    database = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        MODULE["cleanup"](database)
    assert not database.exists()


def test_drop_failure_rolls_back_all_tables(tmp_path):
    database = tmp_path / "dependent.db"
    first, second = MODULE["BACKUP_TABLES"][:2]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('0034')")
        connection.execute(f'CREATE TABLE "{first}"(id INTEGER PRIMARY KEY)')
        connection.execute(f'CREATE TABLE "{second}"(id INTEGER PRIMARY KEY)')
        connection.execute(f'CREATE TABLE dependent(id INTEGER REFERENCES "{second}"(id))')
        connection.execute(f'INSERT INTO "{second}" VALUES (1)')
        connection.execute("INSERT INTO dependent VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError):
        MODULE["cleanup"](database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(f'SELECT * FROM "{first}"').fetchall() == []
        assert connection.execute(f'SELECT * FROM "{second}"').fetchall() == [(1,)]
