"""Axile 服务端使用的数据库引擎与异步会话工厂."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from sqlalchemy import AsyncAdaptedQueuePool, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from axile.common.config import settings

# aiosqlite 默认忙等待只有 5 秒；启动恢复等写并发密集的窗口里不够。
SQLITE_BUSY_TIMEOUT_MS = 30_000


def register_sqlite_concurrency_pragmas(target_engine: AsyncEngine) -> None:
    """为 SQLite 异步引擎逐连接启用并发相关 PRAGMA.

    Notes
    -----
    服务端与 spawn 出来的账户 worker 进程会同时写同一个 SQLite 文件。
    默认回滚日志模式下读写互相阻塞，后到的写入等满默认 5 秒 busy
    timeout 就会报 ``database is locked``。这里启用 WAL（读写不互斥）
    并把忙等待上限抬到 30 秒，把短暂锁竞争转化为可等待的排队。
    ``journal_mode=WAL`` 落库后持久，``busy_timeout`` 与
    ``synchronous`` 则是每连接属性，因此放在 connect 事件里设置。
    """

    @event.listens_for(target_engine.sync_engine, "connect")
    def _apply_sqlite_concurrency_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # 先抬忙等待上限再切 WAL：切换本身也可能撞上并发连接。
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            # 控制计数与审计的已提交数据需要掉电持久性，不能用 NORMAL 换吞吐。
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()


engine = create_async_engine(
    str(settings.sqlalchemy_database_uri),
    echo=False,
    poolclass=AsyncAdaptedQueuePool,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),  # type: ignore[arg-type]
)

register_sqlite_concurrency_pragmas(engine)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def database_path() -> str:
    """业务 SQLite 文件路径;原生读写操作按完整路径打开同一数据库."""
    return str(settings.sqlalchemy_database_uri).replace("sqlite+aiosqlite:///", "", 1)


@lru_cache(maxsize=1)
def analysis_store():
    """原生分析只读访问点;绑定产物随构建提供,缺失时让请求显式失败而非静默回退."""
    from axile.server._native import _core

    return _core.Store(database_path())
