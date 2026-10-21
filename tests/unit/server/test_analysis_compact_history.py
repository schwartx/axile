"""全历史预处理保持原结果，且不让原始载荷或冻结快照驻留缓存。"""

import asyncio
import gc
import json
import weakref
from datetime import date
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server import performance_analysis as analysis
from axile.server.business_facts.repository import business_result
from axile.server.db.models import Account, PortfolioAccount, ScheduleSkip
from axile.server.db.models.performance import PerformanceSettings
from tests.unit.server._business_analysis_support import persist_records, persist_target
from tests.unit.server.test_performance_analysis import database, queue, record
from tests.unit.server.test_performance_analysis import schema_template as schema_template


class _WeakDict(dict):
    """允许在不延长生命周期的情况下检查 JSON 根对象。"""


def test_compact_batches_match_full_history_and_release_raw_values(tmp_path, schema_template, monkeypatch):
    async def check():
        async with database(tmp_path, schema_template, count=0) as (engine, sessions, _):
            rows = [record(index) for index in range(1, 206)]
            for row in rows[:110]:
                row.created_at = "2026-01-02T09:00:00"
                row.is_success = 0
                row.result.update(status="PARTIAL", error="部分完成", execution_time=0, flag=False, missing=None)
            rows[0].input = {}
            rows[1].input = {"curr_target": {}}
            for row in rows:
                row.result = business_result(row.result, is_success=row.is_success)
            async with sessions() as session:
                await persist_records(session, rows, project=True)
                await persist_target(
                    session,
                    account_id=2,
                    portfolio_id=1,
                    source="manual",
                    execution_id="exec-1",
                    normalized_weights={"A": 0},
                )
                session.add(PortfolioAccount(account_id=2, portfolio_id=None, created_at=rows[0].created_at))
                session.add(
                    ScheduleSkip(
                        account_id=2,
                        channel="ctp",
                        triggered_at=rows[0].created_at,
                        calendar_id="test",
                        calendar_day=date(2026, 1, 2),
                        calendar_label="休市",
                    )
                )
                await session.commit()
                history = [rows, [SimpleNamespace(execution_id="exec-1", normalized_weights={"A": 0})]] + [
                    (await session.execute(sa.select(model).where(model.account_id == 2))).scalars().all()
                    for model in (PortfolioAccount, ScheduleSkip)
                ]
                account = await session.get(Account, 2)
                settings = PerformanceSettings.model_validate(account, from_attributes=True)
                channel = str(account.trade_channel)
            expected = analysis.compute_batch(*history, settings, channel)
            # 缺失投影的冷补齐仍覆盖一次规范事实读取，随后正常批次只读标量事实。
            async with sessions() as session:
                from axile.server.analysis_observations import ensure_observation_facts
                from axile.server.analysis_source import ensure_source_facts

                await ensure_observation_facts(session, 2)
                await ensure_source_facts(session, 2)
            original = analysis._prepare_fact_batch
            references, batch_sizes = [], []

            def prepare(prepared, batch, include_observations=True):
                batch_sizes.append(len(batch.headers))
                headers, trades = [_WeakDict(row) for row in batch.headers], [_WeakDict(row) for row in batch.trades]
                references.extend(weakref.ref(row) for row in (*headers, *trades))
                original(
                    prepared,
                    SimpleNamespace(
                        headers=headers,
                        payloads=batch.payloads,
                        trades=trades,
                        observations=batch.observations,
                    ),
                    include_observations,
                )

            def no_business_documents(*_args, **_kwargs):
                raise AssertionError("Frozen analysis history must not reconstruct canonical business documents")

            monkeypatch.setattr(analysis, "_prepare_fact_batch", prepare)
            monkeypatch.setattr(analysis, "_prepare_records", no_business_documents)
            monkeypatch.setattr("axile.server.analysis_history.read_analysis_record", no_business_documents)

            def deserialize(raw):
                value = json.loads(raw)
                return _WeakDict(value) if isinstance(value, dict) else value

            selected = create_async_engine(engine.url, json_deserializer=deserialize)
            try:
                manager = analysis.AnalysisManager(async_sessionmaker(selected))
                inputs = await manager._load(2)
                actual = analysis.compute_batch(*inputs)
                assert json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
                    expected, sort_keys=True, allow_nan=False
                )
                assert batch_sizes == [100, 100, 5]
                await asyncio.sleep(0)
                gc.collect()
                assert all(reference() is None for reference in references)
            finally:
                await selected.dispose()

    asyncio.run(check())


def test_published_snapshot_is_not_retained_by_compilation_cache(tmp_path, schema_template, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def check():
        async with database(tmp_path, schema_template, count=2) as (_, sessions, manager):
            references = []
            original = analysis.compute_batch

            def compute(*args):
                ranges, executions, trades = original(*args)
                tracked = _WeakDict(ranges)
                references.append(weakref.ref(tracked))
                return tracked, executions, trades

            monkeypatch.setattr(analysis, "compute_batch", compute)
            for _ in range(2):
                await queue(sessions, force=True)
                assert await manager.run_once()
                await asyncio.sleep(0)
                gc.collect()
                assert all(reference() is None for reference in references)
            assert len(references) == 2
            async with sessions() as session:
                assert await session.scalar(sa.text("SELECT count(*) FROM performance_snapshot")) == 2

    asyncio.run(check())
