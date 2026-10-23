"""v0.22.0 的真实迁移基线；仅连接 tmp_path 内的 SQLite。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from tests.unit.server.test_initial_migration import _load_migration

_ROOT = Path(__file__).parents[3]
_TAG = "v0.22.0"
_COMMIT = "bc70e4a3785eacedeaf4eadcda5a3109d3a3ffa6"
_VERSIONS = "axile/server/alembic/versions"
_CONTRACT = _ROOT / "tests/fixtures/rust_store/v0.22.0.sql"


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    """覆盖共享建表夹具，不创建当前版本数据库。"""
    yield


@pytest.fixture(autouse=True)
def _isolate_connection_runtime():
    """本模块不使用服务端连接运行时。"""
    yield


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(_ROOT), *args], text=True, timeout=5)


def _baseline_ddl(tmp_path: Path) -> str:
    assert _git("rev-parse", f"{_TAG}^{{commit}}").strip() == _COMMIT
    # 历史迁移依赖这些模块；拒绝悄悄混用后续版本的实现。
    assert not _git(
        "diff",
        _TAG,
        "--",
        "axile/server/migration_support",
        "axile/common/default_account_notification.py",
    )
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    paths = _git("ls-tree", "-r", "--name-only", _TAG, "--", _VERSIONS).splitlines()
    for path in paths:
        if path.endswith(".py"):
            (versions / Path(path).name).write_text(_git("show", f"{_TAG}:{path}"), encoding="utf-8")

    scripts = ScriptDirectory(str(versions.parent))
    assert scripts.get_bases() == ["0001"]
    assert scripts.get_heads() == ["0033"]
    revisions = list(reversed(list(scripts.walk_revisions())))
    assert [revision.revision for revision in revisions] == [f"{n:04}" for n in range(1, 34)]
    assert [revision.down_revision for revision in revisions] == [None, *[f"{n:04}" for n in range(1, 33)]]

    engine = sa.create_engine(sa.URL.create("sqlite", database=str(tmp_path / "baseline.sqlite")))
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            for revision in revisions:
                migration = _load_migration(Path(revision.path))
                migration.op = Operations(context)
                migration.upgrade()
            context.stamp(scripts, "0033")
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0033"
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            rows = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).scalars()
            return (
                "-- Generated from Axile v0.22.0 (public head 0033), empty SQLite.\n"
                + "\n\n".join(f"{ddl};" for ddl in rows)
                + "\n"
            )
    finally:
        engine.dispose()


def test_v0220_rust_store_baseline_ddl(tmp_path: Path) -> None:
    """锁定标签链及真实表、列、默认值、索引、外键和唯一性 DDL。"""
    assert _baseline_ddl(tmp_path) == _CONTRACT.read_text(encoding="utf-8")
