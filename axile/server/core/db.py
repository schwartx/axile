"""Axile 原生存储的数据库路径与启动入口。"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from axile.common.config import STARTUP_DIRECTORY, settings

_initialized_databases: set[str] = set()


def database_path() -> str:
    """按服务启动目录解析 SQLite 文件路径。"""
    path = str(settings.sqlalchemy_database_uri).removeprefix("sqlite+aiosqlite:///")
    if not path or path == ":memory:":
        return path
    return str(STARTUP_DIRECTORY / path)


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


def analysis_store():
    """原生分析只读访问点;绑定产物随构建提供,缺失时让请求显式失败而非静默回退."""
    return _analysis_store(database_path())


@lru_cache(maxsize=1)
def _analysis_store(path: str):
    """只缓存路径句柄，连接由 Rust 借用和归还。"""
    from axile.server._native import _core

    return _core.Store(path)
