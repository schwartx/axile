"""执行事实与原始写入共用事务，迟到目标只更新对应执行。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import analysis_observations
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord, Portfolio
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.db.models.analysis_replay import replay_observation_symbol as symbols
from axile.server.execution import records
from axile.server.target_weight_snapshots import append_target_weight_snapshot
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(build_account(id=1))
            session.add(
                Portfolio(
                    id=1,
                    name="事实测试",
                    market="期货",
                    custom_calc_py_code="def calculate_portfolio(context): return {}",
                )
            )
            await session.commit()
        yield sessions
    finally:
        await engine.dispose()


def test_record_fact_freezes_final_sizing_and_commits_with_record() -> None:
    async def scenario() -> None:
        async with _database() as sessions:
            record = await records._persist_execute_record(
                account_id=1,
                execution_id="sized",
                raw_input={"account_config": {"secret": "hidden"}, "curr_target": {"A": 0.9}},
                raw_result={
                    "account_assets": {"total_asset": 1000.0, "positions": []},
                    "symbol_results": {
                        "A": {
                            "first_tick": {"last_price": 100.0},
                            "sizing": {
                                "status": "SIZED",
                                "sizing_mode": "weight",
                                "target_quantity": 2.0,
                                "unit_notional": 300.0,
                                "reference_price": 100.0,
                                "equity": 1000.0,
                            },
                        }
                    },
                },
                is_success=1,
                session_factory=cast("records.SessionFactory", sessions),
            )
            async with sessions() as session:
                scalar = (await session.execute(sa.select(observations))).mappings().one()
                symbol = (await session.execute(sa.select(symbols))).mappings().one()
                assert scalar["record_id"] == record.id
                assert scalar["asset"] == 1000.0
                assert scalar["target_units_present"] is True
                assert scalar["sizing_fallback"] is False
                assert symbol["price"] == 100.0
                assert symbol["target"] == 0.6
                assert symbol["target_weight"] == 0.9
                assert symbol["target_units"] == 6.0
                assert symbol["target_quantity"] == 2.0
                assert (await session.scalar(sa.select(sa.func.count()).select_from(AccountAssetSnapshot))) == 1
            assert "account_config" not in record.raw_input

    asyncio.run(scenario())


def test_fact_failure_rolls_back_record_assets_and_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_after_write(session: AsyncSession, record: ExecuteRecord) -> None:
        await analysis_observations.write_observation_fact(session, record)
        raise RuntimeError("projection failed")

    monkeypatch.setattr(records, "write_observation_fact", fail_after_write)

    async def scenario() -> None:
        async with _database() as sessions:
            with pytest.raises(RuntimeError, match="projection failed"):
                await records._persist_execute_record(
                    account_id=1,
                    execution_id="rollback",
                    raw_input={"curr_target": {"A": 1.0}},
                    raw_result={"account_assets": {"total_asset": 1000.0, "positions": []}},
                    is_success=1,
                    session_factory=cast("records.SessionFactory", sessions),
                )
            async with sessions() as session:
                for table in (ExecuteRecord, AccountAssetSnapshot, observations, symbols):
                    assert (await session.scalar(sa.select(sa.func.count()).select_from(table))) == 0

    asyncio.run(scenario())


def test_late_target_refreshes_only_matching_execution_fact() -> None:
    async def scenario() -> None:
        async with _database() as sessions:
            for execution_id in ("late", "unrelated"):
                await records._persist_execute_record(
                    account_id=1,
                    execution_id=execution_id,
                    raw_input={},
                    raw_result={"first_ticks": {"A": {"last_price": 100.0}}},
                    is_success=1,
                    session_factory=cast("records.SessionFactory", sessions),
                )
            async with sessions() as session:
                await append_target_weight_snapshot(
                    session,
                    portfolio_id=1,
                    account_id=1,
                    raw_weights={"A": 0.6},
                    normalized_weights={"A": 0.6},
                    source="execution",
                    execution_id="late",
                )
            async with sessions() as session:
                rows = (
                    (await session.execute(sa.select(observations).order_by(observations.c.record_id))).mappings().all()
                )
                assert rows[0]["target_present"] is True
                assert rows[0]["sizing_fallback"] is True
                assert rows[1]["target_present"] is False
                symbol = (
                    (await session.execute(sa.select(symbols).where(symbols.c.record_id == rows[0]["record_id"])))
                    .mappings()
                    .one()
                )
                assert symbol["target"] == 0.6
                assert symbol["target_weight"] == 0.6

    asyncio.run(scenario())
