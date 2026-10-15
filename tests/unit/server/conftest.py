"""服务端单元测试共享夹具."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlmodel import SQLModel

# 导入模型包以将全部数据表登记到 ``SQLModel.metadata``
import axile.server.db.models  # noqa: F401
from axile.server.core.db import engine


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema() -> Iterator[None]:
    """在服务端单元测试会话开始前，于共享引擎上建好全部表.

    Notes
    -----
    服务端部分用例会经由未被 monkeypatch 的真实 ``SessionLocal`` 触达
    ``axile.server.core.db.engine``。根级 ``conftest`` 已在任何服务端模块导入前，
    通过 ``AXILE_CONFIG_TOML`` 将该引擎指向本次测试会话的临时数据库；这里仅
    为该隔离数据库创建完整 schema。建表后 ``dispose`` 连接池，避免连接被绑定
    到本夹具的事件循环而影响后续按用例创建的事件循环。
    """

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())
    yield


@pytest.fixture(autouse=True)
def _isolate_connection_runtime():
    from axile.server.connection_runtime import clear_sessions

    clear_sessions()
    yield
    clear_sessions()
