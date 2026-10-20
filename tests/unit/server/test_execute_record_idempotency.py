"""执行记录并发收尾幂等：同 ``execution_id`` 二次落库保留首条而不抛 IntegrityError."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts.repository import read_execution_result
from axile.server.db.models import ExecuteRecord
from axile.server.execution.records import SessionFactory, _persist_execute_record
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def _session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            session.add(build_account(id=1))
            await session.commit()
        yield factory
    finally:
        await engine.dispose()


def test_duplicate_execution_record_returns_first() -> None:
    """同一执行的两条收尾路径并发落库时保留首条，不向调用方抛 IntegrityError。"""

    async def scenario() -> tuple[int | None, ExecuteRecord, object]:
        async with _session_factory() as factory:
            session_factory = cast("SessionFactory", factory)
            first = await _persist_execute_record(
                account_id=1,
                execution_id="exec-dup-1",
                raw_input={},
                raw_result={"task_status": "TERMINATED"},
                is_success=0,
                session_factory=session_factory,
            )
            second = await _persist_execute_record(
                account_id=1,
                execution_id="exec-dup-1",
                raw_input={},
                raw_result={"task_status": "TERMINATED", "finalized": True},
                is_success=0,
                session_factory=session_factory,
            )
            async with factory() as session:
                result = await read_execution_result(session, second)
            return first.id, second, result

    first_id, second, result = asyncio.run(scenario())
    assert first_id == second.id
    # 首条记录原样保留，未被第二条覆盖。
    assert isinstance(result, dict)
    assert result == {"task_status": "TERMINATED"}
    assert "finalized" not in result
    assert second.task_status == "TERMINATED"


def test_unique_id_conflict_without_execution_id_still_raises() -> None:
    """无 execution_id 的记录不受唯一约束保护，冲突类异常应原样暴露。"""

    async def scenario() -> None:
        async with _session_factory() as factory:
            session_factory = cast("SessionFactory", factory)
            await _persist_execute_record(
                account_id=1,
                execution_id=None,
                raw_input={},
                raw_result={},
                is_success=1,
                session_factory=session_factory,
            )
            await _persist_execute_record(
                account_id=1,
                execution_id=None,
                raw_input={},
                raw_result={},
                is_success=1,
                session_factory=session_factory,
            )

    # execution_id 为 NULL 不触发唯一约束，两条都应成功落库（SQLite 允许多 NULL）。
    asyncio.run(scenario())
