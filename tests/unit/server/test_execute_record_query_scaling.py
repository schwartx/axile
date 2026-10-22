"""单账户执行记录查询的隔离、排序与条数限制。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.repositories import get_recent_execute_records_by_account_id
from tests.unit.server._business_analysis_support import analysis_record, persist_records
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _make_record(record_id: int, account_id: int, total_asset: float, created_at: str, is_success: int = 1):
    return analysis_record(
        id=record_id,
        account_id=account_id,
        execution_id=None,
        input={},
        result={"account_assets": {"total_asset": total_asset}},
        is_success=is_success,
        created_at=created_at,
    )


async def _seed(session: AsyncSession, accounts: int, per_account: int) -> None:
    records = []
    for account_id in range(1, accounts + 1):
        session.add(build_account(id=account_id, account_control_preset="default"))
        for index in range(per_account):
            records.append(
                _make_record(
                    len(records) + 1,
                    account_id,
                    100.0 + index,
                    f"2026-07-{per_account - index:02d}T09:00:00",
                    is_success=index % 2,
                )
            )
    await session.flush()
    await persist_records(session, records)
    await session.commit()


def test_recent_records_preserve_account_limit_and_id_order() -> None:
    """逐账户读取按 ID 而非时间倒序，保留成败记录并限制条数。"""

    async def scenario() -> None:
        async with _session_scope() as session:
            await _seed(session, accounts=3, per_account=5)

            for account_id in (1, 2, 3):
                records = await get_recent_execute_records_by_account_id(session, account_id, limit=3)
                assert [r.id for r in records] == [account_id * 5 - offset for offset in range(3)]
                assert all(r.account_id == account_id for r in records)
                assert [r.is_success for r in records] == [0, 1, 0]
            assert await get_recent_execute_records_by_account_id(session, 4) == []

    asyncio.run(scenario())
