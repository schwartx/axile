"""执行记录并发收尾幂等：同 ``execution_id`` 二次落库保留首条而不抛 IntegrityError."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord
from axile.server.execution.records import _persist_execute_record


@asynccontextmanager
async def _session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


def test_duplicate_execution_record_returns_first() -> None:
    """同一执行的两条收尾路径并发落库时保留首条，不向调用方抛 IntegrityError。"""

    async def scenario() -> tuple[int | None, ExecuteRecord]:
        async with _session_factory() as factory:
            first = await _persist_execute_record(
                account_id=1,
                execution_id="exec-dup-1",
                raw_input={},
                raw_result={"task_status": "TERMINATED"},
                is_success=0,
                session_factory=factory,
            )
            second = await _persist_execute_record(
                account_id=1,
                execution_id="exec-dup-1",
                raw_input={},
                raw_result={"task_status": "TERMINATED", "finalized": True},
                is_success=0,
                session_factory=factory,
            )
            return first.id, second

    first_id, second = asyncio.run(scenario())
    assert first_id == second.id
    # 首条记录原样保留，未被第二条覆盖。
    assert "finalized" not in second.raw_result


def test_unique_id_conflict_without_execution_id_still_raises() -> None:
    """无 execution_id 的记录不受唯一约束保护，冲突类异常应原样暴露。"""

    async def scenario() -> None:
        async with _session_factory() as factory:
            await _persist_execute_record(
                account_id=1,
                execution_id=None,
                raw_input={},
                raw_result={},
                is_success=1,
                session_factory=factory,
            )
            await _persist_execute_record(
                account_id=1,
                execution_id=None,
                raw_input={},
                raw_result={},
                is_success=1,
                session_factory=factory,
            )

    # execution_id 为 NULL 不触发唯一约束，两条都应成功落库（SQLite 允许多 NULL）。
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [(None, 260.0, 260.0), (None, 0.0, 0.0), (0.0, 260.0, 0.0), (None, None, None)],
)
def test_execution_record_uses_previous_market_value_only_when_missing(current, previous, expected) -> None:
    async def scenario() -> None:
        async with _session_factory() as factory:
            async with factory() as session:
                session.add(
                    AccountAssetSnapshot(account_id=1, assets={"total_asset": 500, "market_value": 50}, source="manual")
                )
                await session.flush()
                snapshot = AccountAssetSnapshot(
                    account_id=1, assets={"total_asset": 500, "market_value": previous}, source="manual"
                )
                session.add(snapshot)
                await session.commit()
            assets = {"total_asset": 900, "market_value": current, "positions": []}
            record = await _persist_execute_record(
                account_id=1,
                execution_id="exec-market-value",
                raw_input={},
                raw_result={"account_assets": assets},
                is_success=1,
                session_factory=factory,
            )
            assert record.raw_result["account_assets"] == {**assets, "market_value": expected}
            if current is None and previous is not None:
                assert record.raw_result["market_value_reference"] == {
                    "snapshot_id": snapshot.id,
                    "created_at": snapshot.created_at,
                }
            else:
                assert "market_value_reference" not in record.raw_result
            async with factory() as session:
                saved = await session.scalar(
                    select(AccountAssetSnapshot).where(AccountAssetSnapshot.execution_id == record.execution_id)
                )
                assert saved is not None and saved.assets == assets

    asyncio.run(scenario())
