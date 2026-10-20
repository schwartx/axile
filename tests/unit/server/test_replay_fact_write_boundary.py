"""分析事实与结构化业务写入共用事务，迟到目标只更新对应执行。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_history import AnalysisRecord
from axile.server.business_facts.repository import (
    execution_fact_tables,
    read_execution_input,
    read_execution_result,
)
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord, Portfolio
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.db.models.analysis_replay import replay_observation_symbol as symbols
from axile.server.db.models.analysis_source import SOURCE_FACT_TABLES, source_execution
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
                source = (await session.execute(sa.select(source_execution))).mappings().one()
                assert source["projection_version"] == 1
                assert source["account_id"] == 1
                assert source["record_id"] == record.id
                assert source["execution_id"] == "sized"
                assert record.input_id is not None
                stored_input = await read_execution_input(session, record.input_id)
                assert stored_input == {"curr_target": {"A": 0.9}}

    asyncio.run(scenario())


@pytest.mark.parametrize("writer", ["write_observation_fact", "write_source_facts"])
def test_fact_failure_rolls_back_record_assets_and_facts(monkeypatch: pytest.MonkeyPatch, writer: str) -> None:
    original = getattr(records, writer)

    async def fail_after_write(session: AsyncSession, record: AnalysisRecord) -> None:
        await original(session, record)
        raise RuntimeError("projection failed")

    monkeypatch.setattr(records, writer, fail_after_write)

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
                for table in (
                    *execution_fact_tables(),
                    AccountAssetSnapshot,
                    observations,
                    symbols,
                    *SOURCE_FACT_TABLES,
                ):
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


def test_duplicate_record_retains_first_source_facts_and_projects_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original = records.write_source_facts
    calls: list[str] = []

    async def track_write(session: AsyncSession, record: AnalysisRecord) -> bool:
        calls.append(record.result["status"])
        return await original(session, record)

    monkeypatch.setattr(records, "write_source_facts", track_write)

    async def scenario() -> None:
        async with _database() as sessions:
            first = await records._persist_execute_record(
                account_id=1,
                execution_id="duplicate-source",
                raw_input={},
                raw_result={"status": "NOOP"},
                is_success=1,
                session_factory=cast("records.SessionFactory", sessions),
            )
            second = await records._persist_execute_record(
                account_id=1,
                execution_id="duplicate-source",
                raw_input={},
                raw_result={"status": "FAILED", "error": "late failure"},
                is_success=0,
                session_factory=cast("records.SessionFactory", sessions),
            )
            assert first.id == second.id
            assert calls == ["NOOP"]
            async with sessions() as session:
                assert (await read_execution_result(session, second))["status"] == "NOOP"
                source = (await session.execute(sa.select(source_execution))).mappings().one()
                assert source["projection_version"] == 1
                assert source["noop"] is True
                assert source["success"] is True

    asyncio.run(scenario())


def test_unsupported_source_shape_preserves_business_record_and_marks_fact() -> None:
    async def scenario() -> None:
        async with _database() as sessions:
            record = await records._persist_execute_record(
                account_id=1,
                execution_id="unsupported-source",
                raw_input={},
                raw_result={"status": 123, "account_assets": {"total_asset": 101}, "symbol_results": {}},
                is_success=0,
                session_factory=cast("records.SessionFactory", sessions),
            )
            async with sessions() as session:
                source = (await session.execute(sa.select(source_execution))).mappings().one()
                assert source["record_id"] == record.id
                assert source["projection_version"] == 0
                assert source["projection_error"]
                persisted = await session.get(ExecuteRecord, record.id)
                assert persisted is not None
                assert (await read_execution_result(session, persisted))["status"] == 123
                assert (await session.scalar(sa.select(sa.func.count()).select_from(observations))) == 1

    asyncio.run(scenario())
