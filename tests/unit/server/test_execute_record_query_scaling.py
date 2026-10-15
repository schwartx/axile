"""执行记录查询伸缩性测试。

覆盖 issue #33：

- ``ExecuteRecord`` 需要按账户查询的复合索引；
- 仪表盘的逐账户查询（N+1）需被批量查询取代，查询数不随账户数线性增长；
- 批量版本必须与逐账户版本语义完全一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.db.models import ExecuteRecord
from axile.server.repositories import (
    get_recent_execute_records_by_account_id,
    get_recent_execute_records_for_accounts,
)


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


def _make_record(account_id: int, total_asset: float, created_at: str, is_success: int = 1) -> ExecuteRecord:
    return ExecuteRecord(
        account_id=account_id,
        raw_input={},
        raw_result={"account_assets": {"total_asset": total_asset}},
        is_success=is_success,
        created_at=created_at,
    )


async def _seed(session: AsyncSession, accounts: int, per_account: int) -> None:
    for account_id in range(1, accounts + 1):
        for index in range(per_account):
            session.add(_make_record(account_id, 100.0 + index, f"2026-07-{index + 1:02d}T09:00:00"))
    await session.commit()


def test_batch_recent_matches_per_account() -> None:
    """批量取记录必须与逐账户版本逐条一致（含每账户各自 limit）。"""

    async def scenario() -> None:
        async with _session_scope() as session:
            await _seed(session, accounts=3, per_account=5)

            batched = await get_recent_execute_records_for_accounts(session, [1, 2, 3], limit=3)

            for account_id in (1, 2, 3):
                expected = await get_recent_execute_records_by_account_id(session, account_id, limit=3)
                assert [r.id for r in batched[account_id]] == [r.id for r in expected]
                assert len(batched[account_id]) == 3, "每个账户各自取 limit 条，而非全局 limit 条"

    asyncio.run(scenario())
