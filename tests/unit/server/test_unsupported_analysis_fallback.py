"""显式不支持的分析源使用完整规范事实回退，不放宽正常标量读取。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import performance_analysis as analysis
from axile.server.analysis_history import iter_analysis_records
from axile.server.analysis_observations import write_observation_fact
from axile.server.analysis_source import write_source_facts
from axile.server.business_facts import symbols
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_source import source_execution, source_trade
from tests.unit.server._business_analysis_support import load_record, persist_records, persist_target
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_performance_analysis import record


@asynccontextmanager
async def _database(tmp_path, *, unsupported=True):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unsupported-analysis.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(build_account(id=2, account_control_preset="default"))
            rows = [record(1), record(2)]
            rows[1].input = {}
            rows[1].is_success = 0
            rows[1].result.update(status="PARTIAL", error="some orders unfinished")
            if unsupported:
                rows[1].result["msg"] = {"legacy": "structured message"}
            result = rows[1].result["symbol_results"]["A"]
            result["trades"].extend(
                [
                    {"order_id": "buy", "trade_price": 103, "trade_volume": 1, "extra": {"commission": None}},
                    {"order_id": "buy", "trade_price": 103, "trade_volume": {"invalid": "quantity"}},
                ]
            )
            await persist_records(session, rows)
            await persist_target(
                session,
                account_id=2,
                portfolio_id=1,
                source="manual",
                execution_id=rows[1].execution_id,
                normalized_weights={"A": 0.25},
            )
            for row in rows:
                await write_observation_fact(session, row)
                assert await write_source_facts(session, row) is (not unsupported or row.id == 1)
            await session.commit()
            canonical = [await load_record(session, row.id) for row in rows]
        yield engine, sessions, canonical
    finally:
        await engine.dispose()


def _no_documents(*_args, **_kwargs):
    raise AssertionError("Supported history must not load canonical business documents")


def test_supported_load_uses_only_typed_history(tmp_path, monkeypatch):
    """正常 Python 路径不读业务大 JSON，即使同时存在非法数值审计证据。"""
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def check():
        async with _database(tmp_path, unsupported=False) as (engine, sessions, _):
            statements = []
            sa.event.listen(
                engine.sync_engine, "before_cursor_execute", lambda _, __, sql, *args: statements.append(sql)
            )
            monkeypatch.setattr("axile.server.analysis_history.read_analysis_record", _no_documents)
            monkeypatch.setattr("axile.server.business_facts.repository.read_execution_detail", _no_documents)
            monkeypatch.setattr(analysis, "_prepare_records", _no_documents)
            loaded = await analysis.AnalysisManager(sessions)._load(2)
            assert [item[0] for item in loaded[0].record_times] == [1, 2]
            assert all("raw_input" not in sql and "raw_result" not in sql for sql in statements)
            assert all("business_" not in sql and "json_extract" not in sql.lower() for sql in statements)
            assert all("FROM execution_" not in sql for sql in statements)

    asyncio.run(check())


def test_unsupported_source_loads_complete_canonical_facts_and_frozen_observations(tmp_path, monkeypatch):
    """回退与规范 DTO 的旧计算一致，不把审计异常重注入业务字段或丢掉成交。"""
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def check():
        async with _database(tmp_path) as (_, sessions, canonical):
            seen, batches = [], []
            original = analysis._prepare_records
            from axile.server.analysis_history import read_analysis_record

            async def bounded_records(session, account_id):
                async for batch in iter_analysis_records(session, account_id, batch_size=1):
                    batches.append(len(batch))
                    yield batch

            async def read_in_snapshot(session, header):
                connection = await session.connection()
                assert await connection.run_sync(lambda sync: sync.connection.driver_connection.in_transaction)
                return await read_analysis_record(session, header)

            def prepare(prepared, records, fallback, include_observations=True):
                seen.extend(records)
                return original(prepared, records, fallback, include_observations)

            monkeypatch.setattr("axile.server.analysis_history.iter_analysis_records", bounded_records)
            monkeypatch.setattr("axile.server.analysis_history.read_analysis_record", read_in_snapshot)
            monkeypatch.setattr(analysis, "_prepare_records", prepare)
            inputs = await analysis.AnalysisManager(sessions)._load(2)
            assert batches == [1, 1]
            assert [(item.id, item.input, item.result) for item in seen] == [
                (item.id, item.input, item.result) for item in canonical
            ]
            assert canonical[1].result["audit_evidence"]["invalid_fields"]["msg"] == {"legacy": "structured message"}
            invalid_trade = canonical[1].result["symbol_results"]["A"]["trades"][-1]
            assert invalid_trade.get("trade_volume") is None
            assert invalid_trade["audit_evidence"]["invalid_fields"]["trade_volume"] == {"invalid": "quantity"}
            expected = analysis.compute_batch(
                canonical,
                [SimpleNamespace(execution_id="exec-2", normalized_weights={"A": 0.25})],
                [],
                [],
                inputs[-2],
                inputs[-1],
            )
            actual = analysis.compute_batch(*inputs)
            assert actual == expected
            assert inputs[0].observations[-1].target_weight == {"A": 0.25}
            assert [item["record_id"] for item in actual[1]] == [1, 2]
            assert len(actual[2]) == 4
            assert actual[2][-1]["value"] is None
            assert actual[2][-1]["payload"]["quantity"] is None
            assert actual[0]["all"]["events"][0]["text"] == "执行未全部完成 · some orders unfinished"
            async with sessions() as session:
                assert (await load_record(session, 2)).result == canonical[1].result
                assert (
                    await session.scalar(
                        sa.select(source_execution.c.projection_version).where(source_execution.c.record_id == 2)
                    )
                    == 0
                )

    asyncio.run(check())


def test_native_unsupported_dispatch_publishes_python_result_without_native_execution(tmp_path, monkeypatch):
    """原生发现显式标记后才走兼容计算，不把不完整源交给原生或发布少记录结果。"""
    native = SimpleNamespace(publish_store=AsyncMock(side_effect=AssertionError("Unsupported native dispatch")))
    monkeypatch.setattr("axile.server.native_analysis.get_native_analysis", lambda _session: native)

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            manager = analysis.AnalysisManager(sessions)
            async with sessions() as session:
                await analysis.enqueue(session, 2)
            assert await manager.run_once()
            native.publish_store.assert_not_awaited()
            async with sessions() as session:
                state = (await session.execute(sa.select(states))).mappings().one()
                assert state["error"] is None
                assert state["failures"] == 0
                assert state["published_version"] == state["source_version"]
                response = await analysis.read_snapshot(session, 2, "all")
                assert response["status"] == "ready"
                assert [item["record"]["id"] for item in response["result"]["executions"]] == [1, 2]

    asyncio.run(check())


@pytest.mark.parametrize("batch_size", [0, 101])
def test_unsupported_record_batches_reject_unbounded_reads(batch_size):
    async def check():
        with pytest.raises(ValueError, match="batch_size"):
            async for _ in iter_analysis_records(None, 2, batch_size=batch_size):
                pass

    asyncio.run(check())


@pytest.mark.parametrize(
    "mutation",
    ["typed_identity", "marker_identity", "version", "marker_error", "typed_trade", "marker_trade"],
)
def test_unsupported_marker_does_not_hide_stale_or_corrupt_source_facts(tmp_path, monkeypatch, mutation):
    """混合历史中的正常投影损坏、标记身份过期和未知版本均不能被兼容路径吞掉。"""
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                if mutation == "typed_trade":
                    statement = source_trade.delete().where(source_trade.c.record_id == 1)
                elif mutation == "marker_trade":
                    statement = source_trade.insert().values(
                        record_id=2, trade_index=0, symbol="A", time=0, day="2026-01-02", estimated=True
                    )
                else:
                    record_id, values = {
                        "typed_identity": (1, {"is_success": 0}),
                        "marker_identity": (2, {"execution_id": "stale"}),
                        "version": (2, {"projection_version": 9}),
                        "marker_error": (2, {"projection_error": None}),
                    }[mutation]
                    statement = (
                        source_execution.update().where(source_execution.c.record_id == record_id).values(**values)
                    )
                await session.execute(statement)
                await session.commit()
            with pytest.raises(ValueError, match="missing, unsupported or stale|trade facts changed"):
                await analysis.AnalysisManager(sessions)._load(2)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["missing_facts", "calculator"])
def test_unsupported_fallback_propagates_integrity_and_calculator_errors(tmp_path, monkeypatch, failure):
    """损坏业务事实或旧计算器本身失败时继续报错，不跳记录、不填零、不重试原生。"""
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            if failure == "missing_facts":
                async with sessions() as session:
                    await session.execute(symbols.symbol_result.delete().where(symbols.symbol_result.c.record_id == 2))
                    await session.commit()
            else:

                def fail(*_args, **_kwargs):
                    raise ValueError("legacy calculator failed")

                monkeypatch.setattr(analysis, "project_execution", fail)
            with pytest.raises(ValueError, match="Incomplete|legacy calculator failed"):
                await analysis.AnalysisManager(sessions)._load(2)

    asyncio.run(check())


def test_native_failure_never_retries_python_even_with_unsupported_history(tmp_path, monkeypatch):
    """兼容只针对显式不支持，不扩大为原生运行异常后的第二次计算。"""

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            manager = analysis.AnalysisManager(sessions)
            async with sessions() as session:
                await analysis.enqueue(session, 2)
            monkeypatch.setattr(manager, "_run_native", AsyncMock(side_effect=RuntimeError("native failed")))
            fallback = AsyncMock(side_effect=AssertionError("Must not retry Python after native failure"))
            monkeypatch.setattr(manager, "_load", fallback)
            assert await manager.run_once()
            fallback.assert_not_awaited()
            async with sessions() as session:
                state = (await session.execute(sa.select(states))).mappings().one()
                assert state["failures"] == 1
                assert state["error"]
                assert state["current_snapshot"] is None

    asyncio.run(check())
