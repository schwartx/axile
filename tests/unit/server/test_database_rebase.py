"""最终0034仅承接真实0033；拒绝行为不得先修改数据库。"""

import hashlib
from contextlib import closing

import pytest

from tests.conftest import initialize_native_database, native_store_call
from tests.fixtures.rust_store.schema_contract import database_schema, schema_differences
from tests.sqlite_oracle import connect


def _schema(path):
    with closing(connect(path)) as connection:
        return database_schema(connection)


def test_empty_database_and_released_database_have_the_same_final_schema(tmp_path):
    fresh, upgraded = tmp_path / "fresh.db", tmp_path / "upgraded.db"
    initialize_native_database(upgraded, revision="0033")
    assert initialize_native_database(fresh)["public"] == "0034"
    assert initialize_native_database(upgraded)["public"] == "0034"
    assert schema_differences(_schema(fresh), _schema(upgraded)) == []
    assert "backup" not in initialize_native_database(upgraded)
    with closing(connect(fresh)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert not any(name.startswith("performance_") for name in tables)
        assert {
            "analysis_state",
            "analysis_replay_position",
            "analysis_contribution_state",
            "analysis_day",
            "analysis_contribution_day",
        } <= tables
        assert "history_visible" not in {row[1] for row in connection.execute("PRAGMA table_info(executerecord)")}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("revision", ["0001", "0029", "0031", "0032"])
def test_older_database_requires_released_bridge_before_any_write(tmp_path, revision):
    from axile.server._native import _core

    path = tmp_path / "older.db"
    with closing(connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);"
            "CREATE TABLE business_evidence(value TEXT);"
            "INSERT INTO business_evidence VALUES('preserve');"
        )
        connection.execute("INSERT INTO alembic_version VALUES(?)", (revision,))
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(_core.NativeError, match="v0.23.1"):
        native_store_call(path, "migration_upgrade", {})
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with closing(connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT value FROM business_evidence").fetchone() == ("preserve",)


@pytest.mark.parametrize("revision", ["0034", "0035", "0036", "0037", "0038", "0039", "0040", "unknown"])
def test_development_or_unknown_schema_is_not_treated_as_already_migrated(tmp_path, revision):
    from axile.server._native import _core

    path = tmp_path / "unsupported.db"
    with closing(connect(path)) as connection, connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES(?)", (revision,))
    before = path.read_bytes()
    with pytest.raises(_core.NativeError):
        native_store_call(path, "migration_upgrade", {})
    assert path.read_bytes() == before


def test_unversioned_business_database_is_rejected_without_overwriting_it(tmp_path):
    from axile.server._native import _core

    path = tmp_path / "unversioned.db"
    with closing(connect(path)) as connection, connection:
        connection.execute("CREATE TABLE account(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO account VALUES(7)")
    before = path.read_bytes()
    with pytest.raises(_core.NativeError):
        native_store_call(path, "migration_upgrade", {})
    assert path.read_bytes() == before


def test_readonly_migration_commands_keep_a_missing_database_missing(tmp_path):
    path = tmp_path / "missing.db"
    for operation in ("migration_current", "migration_check"):
        receipt = native_store_call(path, operation, {})
        assert receipt["public"] is None
        assert receipt["public_target"] == "0034"
    assert not path.exists()


@pytest.mark.parametrize(
    ("revision", "operation", "definition", "name"),
    [
        (
            "0033",
            "migration_upgrade",
            "CREATE TRIGGER custom_analysis_audit AFTER UPDATE ON account_analysis BEGIN SELECT 1; END",
            "custom_analysis_audit",
        ),
        (
            "0034",
            "migration_downgrade",
            "CREATE INDEX custom_analysis_source_version ON account_analysis(source_version)",
            "custom_analysis_source_version",
        ),
    ],
)
def test_custom_analysis_objects_block_migration_before_any_write(tmp_path, revision, operation, definition, name):
    from axile.server._native import _core

    path = tmp_path / "custom-analysis.db"
    initialize_native_database(path, revision=revision)
    _core.close_database(str(path))
    with closing(connect(path)) as connection:
        with connection:
            connection.execute(definition)
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        before_object = connection.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
    before = hashlib.sha256(path.read_bytes()).digest()
    request = {"public": "0033"} if operation == "migration_downgrade" else {}
    with pytest.raises(_core.NativeError, match=name):
        native_store_call(path, operation, request)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with closing(connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert (
            connection.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,)).fetchone() == before_object
        )


@pytest.mark.parametrize("revision", ["0033", "0034"])
@pytest.mark.parametrize("definition", ["TEXT", "TEXT GENERATED ALWAYS AS ('keep-me') VIRTUAL"])
def test_custom_analysis_columns_block_migration_without_losing_data(tmp_path, revision, definition):
    from axile.server._native import _core

    path = tmp_path / "custom-analysis-column.db"
    initialize_native_database(path, revision=revision)
    _core.close_database(str(path))
    with closing(connect(path)) as connection:
        with connection:
            connection.execute(f"ALTER TABLE account_analysis ADD COLUMN audit_tag {definition}")
            connection.execute(
                "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
                "VALUES(1,'saved','FUTURES','ctp','','2026-10-28','2026-10-28')"
            )
            connection.execute("INSERT OR IGNORE INTO account_analysis(account_id) VALUES(1)")
            if definition == "TEXT":
                connection.execute("UPDATE account_analysis SET audit_tag='keep-me' WHERE account_id=1")
        assert connection.execute("SELECT audit_tag FROM account_analysis").fetchone() == ("keep-me",)
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = hashlib.sha256(path.read_bytes()).digest()
    operation = "migration_upgrade" if revision == "0033" else "migration_downgrade"
    request = {} if revision == "0033" else {"public": "0033"}
    with pytest.raises(_core.NativeError, match="audit_tag"):
        native_store_call(path, operation, request)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with closing(connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
        assert connection.execute("SELECT audit_tag FROM account_analysis").fetchone() == ("keep-me",)
