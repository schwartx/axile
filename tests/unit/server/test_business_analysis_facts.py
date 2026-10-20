"""规范业务输入投影和纯标量 Python 分析回退的回归。"""

import asyncio
from dataclasses import replace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_history import AnalysisRecord, iter_analysis_history, iter_analysis_observations
from axile.server.analysis_observations import write_observation_fact
from axile.server.analysis_source import write_source_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis_replay import replay_observation
from axile.server.db.models.analysis_source import source_execution, source_trade
from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance_analysis import _prepare_fact_batch, _PreparedHistory, compute_batch
from tests.unit.server._execution_test_support import build_account


def _record(index=1):
    return AnalysisRecord(
        id=index,
        account_id=1,
        execution_id=f"typed-{index}",
        created_at=f"2026-01-{index:02d}T09:00:00",
        is_success=1,
        input={"curr_target": {"A": 0.5}},
        result={
            "status": "SUCCEEDED",
            "account_assets": {"total_asset": 1000 + index, "source": "real", "positions": []},
            "symbol_results": {
                "A": {
                    "status": "SUCCEEDED",
                    "first_tick": {"last_price": 10 + index},
                    "sizing": {"unit_multiplier": 10},
                    "orders": [{"order_id": "duplicate", "direction": "BUY", "filled_volume": 2}],
                    "trades": [
                        {
                            "order_id": "duplicate",
                            "trade_price": 11 + index,
                            "trade_volume": 2,
                            "extra": {"commission": 0, "commission_asset": "CNY"},
                        }
                    ],
                }
            },
        },
    )


async def _database(tmp_path, records):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'typed-analysis.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(build_account(id=1))
        for record in records:
            session.add(
                ExecuteRecord(
                    id=record.id,
                    account_id=record.account_id,
                    execution_id=record.execution_id,
                    created_at=record.created_at,
                    is_success=record.is_success,
                )
            )
        await session.flush()
        for record in records:
            assert await write_source_facts(session, record)
            await write_observation_fact(session, record, fallback_weights={})
        await session.commit()
    return engine, sessions


def test_scalar_python_history_matches_explicit_typed_input(tmp_path):
    """同一输入的成本、收益、事件与预览一致，读取过程中不展开业务详情。"""
    records = [_record(1), _record(2)]
    records[1] = replace(
        records[1],
        is_success=0,
        result={**records[1].result, "status": "PARTIAL", "error": "some orders unfinished"},
    )

    async def check():
        engine, sessions = await _database(tmp_path, records)
        statements = []
        sa.event.listen(engine.sync_engine, "before_cursor_execute", lambda _, __, sql, *args: statements.append(sql))
        try:
            prepared = _PreparedHistory()
            async with sessions() as session:
                async for batch in iter_analysis_history(session, 1, batch_size=1):
                    assert len(batch.headers) == 1
                    _prepare_fact_batch(prepared, batch)
            settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0)
            expected = compute_batch(records, [], [], [], settings)
            actual = compute_batch(prepared, [], [], [], settings)
            assert actual == expected
            assert all("raw_input" not in sql and "raw_result" not in sql for sql in statements)
            assert all("business_" not in sql and "json_extract" not in sql.lower() for sql in statements)
            assert len([sql for sql in statements if "FROM analysis_source_execution " in sql]) == 3
            statements.clear()
            async with sessions() as session:
                account_items = [
                    item async for batch in iter_analysis_observations(session, 1, batch_size=1) for item in batch
                ]
            assert account_items == prepared.observations
            assert all("analysis_source_" not in sql and "business_" not in sql for sql in statements)
            assert all("raw_input" not in sql and "raw_result" not in sql for sql in statements)
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("mutation", ["identity", "observation", "trade"])
def test_scalar_history_rejects_stale_identity_and_missing_observations(tmp_path, mutation):
    """缺失或过期投影不能静默发布少记录的结果。"""

    async def check():
        engine, sessions = await _database(tmp_path, [_record()])
        try:
            async with sessions() as session:
                statement = {
                    "identity": source_execution.update().values(execution_id="stale"),
                    "observation": replay_observation.delete(),
                    "trade": source_trade.delete(),
                }[mutation]
                await session.execute(statement)
                await session.commit()
            async with sessions() as session:
                with pytest.raises(ValueError, match="missing, unsupported or stale|facts changed"):
                    async for _ in iter_analysis_history(session, 1):
                        pass
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("batch_size", [0, 101])
def test_scalar_history_rejects_unbounded_batches(batch_size):
    async def check():
        with pytest.raises(ValueError, match="batch_size"):
            async for _ in iter_analysis_history(None, 1, batch_size=batch_size):
                pass

    asyncio.run(check())
