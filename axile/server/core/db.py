"""Axile 原生存储的数据库路径与启动入口。"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from axile.common.config import settings

_initialized_databases: set[str] = set()


def database_path() -> str:
    """业务 SQLite 文件路径;原生读写操作按完整路径打开同一数据库."""
    return str(settings.sqlalchemy_database_uri).replace("sqlite+aiosqlite:///", "", 1)


def initialize_database(migration_runner: Callable[[], None] | None = None) -> None:
    """每个数据库路径仅完成一次启动迁移；失败时保留重试机会。"""
    path = database_path()
    if path in _initialized_databases:
        return
    if migration_runner is None:
        from axile.server.native_store import native_operation_sync

        native_operation_sync("migration_upgrade", {}, database=path)
    else:
        migration_runner()
    _initialized_databases.add(path)


@lru_cache(maxsize=1)
def analysis_store():
    """原生分析只读访问点;绑定产物随构建提供,缺失时让请求显式失败而非静默回退."""
    from axile.server._native import _core

    return _core.Store(database_path())
