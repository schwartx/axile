"""执行记录与账户资产快照原子持久化测试."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.server.business_facts.assets import asset_binding, asset_snapshot, read_snapshot_assets
from axile.server.business_facts.repository import read_execution_result
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord
from axile.server.execution.records import append_success_execute_record
from tests.unit.server._execution_test_support import build_account


class _Session(AsyncSession):
    commits: ClassVar[int] = 0

    async def commit(self) -> None:
        await super().commit()
        type(self).commits += 1


def test_success_record_persists_matching_asset_snapshot() -> None:
    account = build_account(id=51)
    result = {
        "account_assets": {
            "available_cash": 700.0,
            "total_asset": 1000.0,
            "market_value": 300.0,
            "positions": [],
        }
    }

    async def check() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            sessions = async_sessionmaker(engine, class_=_Session, expire_on_commit=False)
            async with sessions() as session:
                session.add(account)
                await session.commit()
            _Session.commits = 0
            saved = await append_success_execute_record(
                account,
                raw_input={},
                result=result,
                execution_id="exec-assets-51",
                session_factory=sessions,
            )
            assert _Session.commits == 1
            async with sessions() as session:
                record = await session.get(ExecuteRecord, saved.id)
                snapshot = (await session.execute(select(AccountAssetSnapshot))).scalar_one()
                assert record is not None
                assert snapshot.account_id == 51
                assert snapshot.execution_id == "exec-assets-51"
                assert snapshot.source == "execution"
                assert snapshot.created_at == record.created_at
                binding_id = await session.scalar(
                    sa.select(asset_binding.c.asset_id).where(
                        asset_binding.c.record_id == record.id,
                        asset_binding.c.path == "/account_assets",
                    )
                )
                assert snapshot.asset_fact_id == binding_id
                assert await session.scalar(sa.select(sa.func.count()).select_from(asset_snapshot)) == 1
                assets = await read_snapshot_assets(session, snapshot)
                canonical_result = await read_execution_result(session, record)
                assert assets == canonical_result["account_assets"]
                assert assets == {
                    **result["account_assets"],
                    "availability": {"cash": "value", "equity": "value", "market_value": "value"},
                }
        finally:
            await engine.dispose()

    asyncio.run(check())
