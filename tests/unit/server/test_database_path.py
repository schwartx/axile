"""GM 切换进程工作目录后，延迟打开的 SQLite 连接仍访问服务数据库。"""

from axile.server import connection_state, order_submissions
from axile.server.core import db
from axile.server.execution import notification_state


def test_worker_database_path_stays_at_startup_directory(tmp_path, monkeypatch):
    startup = tmp_path / "startup"
    strategy = tmp_path / "strategy"
    startup.mkdir()
    strategy.mkdir()
    monkeypatch.setattr(db, "STARTUP_DIRECTORY", startup)
    monkeypatch.setattr(db.settings, "sqlalchemy_database_uri", "sqlite+aiosqlite:///axile.db")

    monkeypatch.chdir(strategy)
    for engine_factory in (notification_state._notification_engine, connection_state.connection_engine):
        engine_factory.cache_clear()
        engine = engine_factory()
        try:
            with engine.connect() as connection:
                database = connection.exec_driver_sql("PRAGMA database_list").one()[2]
            assert database == str(startup / "axile.db")
        finally:
            engine.dispose()
            engine_factory.cache_clear()
    assert order_submissions.submission_database() == str(startup / "axile.db")
    assert not (strategy / "axile.db").exists()


def test_database_url_preserves_memory_absolute_path_and_parameters(tmp_path):
    memory = db.sqlite_url_at_startup("sqlite+aiosqlite:///:memory:")
    assert memory.database == ":memory:"
    path = tmp_path / "custom.db"
    absolute = db.sqlite_url_at_startup(f"sqlite+aiosqlite:///{path}?timeout=7")
    assert absolute.database == str(path)
    assert absolute.query["timeout"] == "7"
