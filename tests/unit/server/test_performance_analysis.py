"""Durable analysis races, invalidation, retention and cost semantics."""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from shutil import copyfile
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.routes.account_performance import get_performance
from axile.server.db.models import Account, ExecuteRecord, PortfolioAccount, TargetWeightSnapshot
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import cost_trade as trades
from axile.server.performance_analysis import (
    LOGIC_VERSION,
    AnalysisManager,
    compute_batch,
    enqueue,
    read_performance_summaries,
    read_snapshot,
)
from axile.server.performance_costs import daily_costs, execution_list_payload, project_execution, summarize, timestamp
from axile.server.performance_details import CostQuery, read_costs
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration


def test_version_16_returns_are_rebuilt_from_independent_equity(tmp_path, schema_template):
    """旧日内相加结果在重启后失效，三个区间和卡片都发布新账本结果。"""

    async def check():
        async with database(tmp_path, schema_template, count=0) as (_, sessions, manager):
            async with sessions() as session:
                for i, price in enumerate([100, 110, 121]):
                    session.add(
                        ExecuteRecord(
                            account_id=2,
                            execution_id=f"intraday-{i}",
                            created_at=f"2026-01-01T{9 + i:02d}:00:00",
                            raw_input={"curr_target": {"A": 1}},
                            raw_result={
                                "account_assets": {"total_asset": price},
                                "first_ticks": {"A": {"last_price": price}},
                            },
                            is_success=1,
                        )
                    )
                await session.commit()
            await queue(sessions)
            await manager.run_once()
            original = await snapshot(sessions)
            async with sessions() as session:
                ranges = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                for value in ranges.values():
                    value["performance"]["points"][-1]["portfolio_return"] = 0.2
                await session.execute(snapshots.update().values(logic_version="16", ranges=ranges))
                await session.execute(states.update().values(logic_version="16"))
                await session.commit()
            assert (await snapshot(sessions))["status"] == "pending"
            await manager.start()
            await manager.stop()
            await manager.run_once()
            async with sessions() as session:
                for range_key in ("all", "30", "90"):
                    rebuilt = await read_snapshot(session, 2, range_key)
                    assert rebuilt["status"] == "ready"
                    assert rebuilt["snapshot_id"] != original["snapshot_id"]
                    assert rebuilt["logic_version"] == LOGIC_VERSION
                    assert rebuilt["engine_version"] == "equity-ledger-1"
                    assert rebuilt["result"]["points"][-1]["portfolio_return"] == pytest.approx(0.21)
                summary = (await read_performance_summaries(session, [2]))[2]
                assert summary.snapshot_id == rebuilt["snapshot_id"]
                assert summary.points[-1].portfolio_return == pytest.approx(0.21)

    asyncio.run(check())


def test_execution_projection_keeps_counts_without_large_symbol_evidence():
    import copy

    symbol = {
        "status": "PARTIAL",
        "error": "未到位",
        "orders": [{"order_id": "A", "extra": {"raw": "x" * 10000}}],
        "trades": [{"trade_volume": 1, "trade_price": 10}] * 1000,
        "memory": {"debug": "x" * 10000},
        "account_assets": {"positions": [1] * 1000},
    }
    item = record(symbol_results={"A": symbol})
    original = copy.deepcopy(item.raw_result)
    payload, trades = project_execution(item)
    assert payload["record"]["raw_result"]["symbol_results"] == {
        "A": {"status": "PARTIAL", "error": "未到位", "outcome": "not_reached"}
    }
    assert payload["tradeCount"] == len(trades) == 1000
    assert item.raw_result == original

    old = {"record": {"id": 1, "raw_result": original}}
    compact = execution_list_payload(old)
    assert compact["tradeCount"] == 1000
    assert compact["record"]["raw_result"]["symbol_results"] == payload["record"]["raw_result"]["symbol_results"]
    assert old["record"]["raw_result"] == original
    assert execution_list_payload(compact) == compact


def record(index=1, **kwargs):
    return ExecuteRecord(
        account_id=2,
        id=index,
        execution_id=f"exec-{index}",
        is_success=1,
        created_at=(datetime(2026, 1, 1, 9) + timedelta(days=index - 1)).isoformat(),
        raw_input={"curr_target": {"A": 1}},
        raw_result={
            "account_assets": {"total_asset": 100 + index},
            "symbol_results": {
                "A": {
                    "first_tick": {"bid_price": 100 + index, "ask_price": 100 + index},
                    "sizing": {"unit_multiplier": 10},
                    "orders": [{"order_id": "buy", "direction": "BUY"}],
                    "trades": [
                        {
                            "order_id": "buy",
                            "trade_price": 102 + index,
                            "trade_volume": 2,
                            "extra": {"commission": 0, "commission_asset": "CNY"},
                        }
                    ],
                }
            },
            **kwargs,
        },
    )


def _create_schema(connection):
    SQLModel.metadata.create_all(connection)
    migration = _load_migration(_MIGRATIONS_DIR / "0012_performance_snapshots.py")
    migration.install_triggers(connection)
    settings_migration = _load_migration(_MIGRATIONS_DIR / "0020_account_settings.py")
    settings_migration.install_settings_trigger(connection)


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory):
    """模板包含真实索引和触发器，复制前关闭全部连接。"""
    path = tmp_path_factory.mktemp("analysis-schema") / "template.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _create_schema(connection)
    finally:
        engine.dispose()
    return path


@asynccontextmanager
async def database(tmp_path, schema_template, count=3, *, fresh_schema=False):
    if not fresh_schema:
        copyfile(schema_template, tmp_path / "analysis.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    if fresh_schema:
        async with engine.begin() as conn:
            await conn.run_sync(_create_schema)
    async with sessions() as session:
        session.add(build_account(id=2, account_control_preset="default"))
        await session.commit()
        session.add_all([record(i + 1) for i in range(count)])
        await session.commit()
    try:
        yield engine, sessions, AnalysisManager(sessions)
    finally:
        await engine.dispose()


async def queue(sessions, force=False):
    async with sessions() as session:
        await enqueue(session, 2, force)


async def snapshot(sessions):
    async with sessions() as session:
        return await read_snapshot(session, 2, "all")


def test_legacy_performance_waits_for_current_settings(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            async with sessions() as session:
                account = await session.get(Account, 2)
                account.backtest_fee_rate = 0.001
                await session.commit()
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(analysis_manager=manager)))
            async with sessions() as session:
                pending = asyncio.create_task(get_performance(session, request, 2))
                try:
                    await asyncio.wait_for(manager.wake.wait(), timeout=2)
                    assert not pending.done()
                    await manager.run_once()
                    result = await asyncio.wait_for(pending, timeout=2)
                    assert result.settings.backtest_fee_rate == 0.001
                    assert result.backtest_included
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(check())


def test_dashboard_summary_matches_full_90_day_snapshot_in_one_query(tmp_path, schema_template):
    """2160 条小时观测完整进入同版卡片，不再取最近 200 条。"""

    async def check():
        async with database(tmp_path, schema_template, count=0) as (engine, sessions, manager):
            async with sessions() as session:
                rows = [record(i + 1) for i in range(2160)]
                for i, row in enumerate(rows):
                    row.created_at = (datetime(2026, 1, 1) + timedelta(hours=i)).isoformat()
                session.add_all(rows)
                await session.commit()
            await queue(sessions)
            await manager.run_once()
            full = await snapshot(sessions)
            statements = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with sessions() as session:
                    summaries = await read_performance_summaries(session, [2, 999])
            finally:
                sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert len(statements) == 1
            assert "executerecord" not in statements[0].lower()
            assert "json_extract" in statements[0].lower()
            summary = summaries[2]
            assert summary.snapshot_id == full["snapshot_id"]
            assert len(summary.points) == 91
            assert [point.model_dump() for point in summary.points] == full["result"]["points"]
            assert summary.account_equity == 2260
            assert summary.account_daily_return == full["result"]["points"][-1]["account_daily_return"]
            assert summary.observed_at == full["result"]["points"][-1]["observed_at"]
            for error, expected in [(None, "stale"), ("calculation failed", "failed")]:
                async with sessions() as session:
                    await session.execute(
                        states.update().where(states.c.account_id == 2).values(requested=True, error=error)
                    )
                    await session.commit()
                    old = (await read_performance_summaries(session, [2]))[2]
                    assert old.status == expected
                    assert old.snapshot_id == summary.snapshot_id
                    assert old.account_equity == summary.account_equity

    asyncio.run(check())


def test_legacy_summary_waits_for_equity_and_rebuilds_on_logic_upgrade(tmp_path, schema_template):
    """旧 JSON 不伪造金额，启动升级后发布完整新快照。"""

    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            initial = await snapshot(sessions)
            async with sessions() as session:
                ranges = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                for result in ranges.values():
                    for point in result["performance"]["points"]:
                        point.pop("account_equity", None)
                await session.execute(snapshots.update().values(ranges=ranges, logic_version="4"))
                await session.execute(states.update().values(logic_version="4"))
                await session.commit()
                old = (await read_performance_summaries(session, [2]))[2]
                assert old.account_equity is None
                assert old.points == []
                assert old.status == "pending"
                assert (await read_snapshot(session, 2, "all"))["result"] is None
                with pytest.raises(HTTPException) as exc:
                    await read_costs(session, 2, CostQuery(snapshot_id=initial["snapshot_id"]))
                assert exc.value.status_code == 410
            await manager.start()
            await manager.stop()
            await manager.run_once()
            async with sessions() as session:
                current = (await read_performance_summaries(session, [2]))[2]
                assert current.snapshot_id != initial["snapshot_id"]
                assert current.status == "ready"
                assert current.account_equity == 103
                # 日末缺失时金额保持缺失，不回退到前一天。
                ranges = (
                    await session.execute(sa.select(snapshots.c.ranges).where(snapshots.c.id == current.snapshot_id))
                ).scalar_one()
                ranges["all"]["performance"]["points"][-1]["account_equity"] = None
                await session.execute(
                    snapshots.update().where(snapshots.c.id == current.snapshot_id).values(ranges=ranges)
                )
                await session.commit()
                assert (await read_performance_summaries(session, [2]))[2].account_equity is None

    asyncio.run(check())


@pytest.mark.parametrize("state", ["empty", "stale", "failed"])
def test_account_only_performance_ignores_unavailable_backtest(tmp_path, monkeypatch, state, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            if state != "empty":
                await queue(sessions)
                await manager.run_once()
            async with sessions() as session:
                account = await session.get(Account, 2)
                account.backtest_fee_rate = 0.001
                session.add(PortfolioAccount(account_id=2, portfolio_id=None, created_at="2026-01-02T09:00:00"))
                await session.commit()

            def fail(*args):
                raise RuntimeError("WBT unavailable")

            monkeypatch.setattr("axile.server.performance._portfolio_daily", fail)
            if state == "failed":
                await manager.run_once()
                assert (await snapshot(sessions))["status"] == "failed"
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(analysis_manager=manager)))
            async with sessions() as session:
                result = await get_performance(session, request, 2, include_backtest=False)
                assert result.settings.backtest_fee_rate == 0.001
                assert result.points[-1].account_return == pytest.approx(103 / 101 - 1)
                assert not result.backtest_included
                assert result.skips is None
                assert result.used_record_count == 0
                assert all(point.portfolio_return is None for point in result.points)
                assert result.bindings[0].time == "2026-01-02T09:00:00"
                assert result.bindings[0].portfolio_id is None
                if state == "failed":
                    with pytest.raises(HTTPException) as error:
                        await get_performance(session, request, 2)
                    assert error.value.status_code == 503

    asyncio.run(check())


def test_snapshot_hits_never_read_history_or_call_wbt_and_survive_restart(tmp_path, monkeypatch, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (engine, sessions, manager):
            assert (await snapshot(sessions))["status"] == "empty"
            await queue(sessions)
            assert await manager.run_once()
            initial = await snapshot(sessions)
            assert initial["status"] == "ready"
            assert initial["result"]["points"][-1]["account_return"] == pytest.approx(103 / 101 - 1)
            assert initial["daily_costs"]["2026-01-01"]["cost"] == 40
            monkeypatch.setattr(
                "axile.server.performance_analysis.compute_batch", lambda *args: pytest.fail("WBT cache miss")
            )
            queries = []
            sa.event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                lambda conn, cursor, statement, *args: queries.append(statement),
            )
            restarted = AnalysisManager(sessions)
            await restarted.start()
            try:
                for _ in range(5):
                    assert await snapshot(sessions) == initial
            finally:
                await restarted.stop()
            assert not any("executerecord" in stmt or "target_weight_snapshot" in stmt for stmt in queries)

    asyncio.run(check())


def test_refresh_merges_and_inflight_source_change_discards_batch(tmp_path, monkeypatch, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            await asyncio.gather(*(queue(sessions, True) for _ in range(8)))
            entered, release = threading.Event(), threading.Event()
            calls = []

            def blocked(*args):
                calls.append(1)
                entered.set()
                assert release.wait(10)
                return compute_batch(*args)

            monkeypatch.setattr("axile.server.performance_analysis.compute_batch", blocked)
            job = asyncio.create_task(manager.run_once())
            while not entered.is_set():
                await asyncio.sleep(0.01)
            async with sessions() as session:
                await session.execute(
                    sa.update(ExecuteRecord)
                    .where(ExecuteRecord.id == 3)
                    .values(raw_result={"account_assets": {"total_asset": 200}})
                )
                await session.commit()
            release.set()
            await job
            stale = await snapshot(sessions)
            assert stale["snapshot_id"] is None
            assert stale["status"] == "pending"
            assert await manager.run_once()
            assert len(calls) == 2
            assert not await manager.run_once()
            assert (await snapshot(sessions))["result"]["points"][-1]["account_return"] == pytest.approx(200 / 101 - 1)

    asyncio.run(check())


def test_failed_batch_keeps_results_and_limits_retries_then_new_data_recovers(tmp_path, monkeypatch, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            initial = await snapshot(sessions)
            await queue(sessions, True)

            def fail(*args):
                raise RuntimeError("secret broker configuration must not be sent to client")

            with monkeypatch.context() as patch:
                patch.setattr("axile.server.performance_analysis.compute_batch", fail)
                for attempt, delay in enumerate((5, 30, 120, 120), 1):
                    before = time.time()
                    assert await manager.run_once()
                    result = await snapshot(sessions)
                    assert result["status"] == "failed" and result["result"] == initial["result"]
                    assert "secret" not in result["error"]
                    async with sessions() as session:
                        row = (await session.execute(sa.select(states))).mappings().one()
                        assert row["failures"] == attempt
                        assert before + delay <= row["retry_at"] <= time.time() + delay
                        assert not await manager.run_once()
                        await session.execute(states.update().values(retry_at=0))
                        await session.commit()
                assert not await manager.run_once()
            async with sessions() as session:
                account = await session.get(Account, 2)
                account.backtest_fee_rate = 0.001
                await session.commit()
            assert await manager.run_once()
            assert (await snapshot(sessions))["settings"]["backtest_fee_rate"] == 0.001

    asyncio.run(check())


def test_transactional_invalidation_and_bulk_delete(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            initial = await snapshot(sessions)
            async with sessions() as session:
                await session.execute(sa.update(ExecuteRecord).values(raw_input={"curr_target": {}}))
                await session.rollback()
            assert await snapshot(sessions) == initial
            async with sessions() as session:
                session.add(PortfolioAccount(account_id=2, portfolio_id=None))
                session.add(TargetWeightSnapshot(account_id=2, portfolio_id=1, source="manual", normalized_weights={}))
                await session.commit()
            assert (await snapshot(sessions))["source_version"] > initial["source_version"]
            async with sessions() as session:
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.account_id == 2))
                await session.commit()
            assert await manager.run_once()
            assert (await snapshot(sessions))["result"]["points"] == []

    asyncio.run(check())


def test_cost_pages_bind_filter_sort_and_retained_versions(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=25) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            initial = await snapshot(sessions)
            query = CostQuery(snapshot_id=initial["snapshot_id"])
            async with sessions() as session:
                first = await read_costs(session, 2, query)
                assert first["count"] == 25 and len(first["data"]) == 20
                assert first["summary"]["cost"] == 1000
                assert "trades" not in first["data"][0]
                second = await read_costs(session, 2, query.model_copy(update={"cursor": first["next_cursor"]}))
                assert len(second["data"]) == 5
                assert not {row["key"] for row in first["data"]} & {row["key"] for row in second["data"]}
                with pytest.raises(HTTPException) as error:
                    await read_costs(
                        session, 2, query.model_copy(update={"cursor": first["next_cursor"], "sort": "cost"})
                    )
                assert error.value.status_code == 400
                symbols = await read_costs(session, 2, query.model_copy(update={"dimension": "symbol"}))
                assert symbols["summary"] == first["summary"]
                assert symbols["data"][0]["buy"] == 50
            await queue(sessions, True)
            await manager.run_once()
            async with sessions() as session:
                assert (await read_costs(session, 2, query))["summary"] == first["summary"]
            await queue(sessions, True)
            await manager.run_once()
            async with sessions() as session:
                with pytest.raises(HTTPException) as error:
                    await read_costs(session, 2, query)
                assert error.value.status_code == 410
                assert await session.scalar(sa.select(sa.func.count()).select_from(snapshots)) == 2
                assert await session.scalar(sa.select(sa.func.count()).select_from(trades)) == 50

    asyncio.run(check())


def test_cost_cross_day_unknowns_fees_noop_and_interval_boundary(tmp_path, schema_template):
    item = record()
    result = item.raw_result["symbol_results"]["A"]
    result["trades"] = [
        {
            "order_id": "buy",
            "trade_price": 102,
            "trade_volume": 2,
            "trade_time": "2026-01-01T23:59:00",
            "extra": {"commission": 0, "commission_asset": "CNY"},
        },
        {
            "order_id": "sell",
            "trade_price": 100,
            "trade_volume": 2,
            "trade_time": "2026-01-02T00:01:00",
            "extra": {"direction": "sell", "commission": 0.1, "commission_asset": "USD"},
        },
        {"trade_price": 100, "trade_volume": 2, "trade_time": "bad"},
    ]
    payload, fills = project_execution(item)
    assert not payload["noop"]
    assert [fill["cost"] for fill in fills] == [20, 20, None]
    assert fills[-1]["timeEstimated"]
    assert summarize(fills)["fees"] == {"CNY": 0, "USD": 0.1}
    assert summarize(fills)["feeCovered"] == 2
    assert summarize(fills)["coverage"] == pytest.approx(4040 / 6040)
    assert daily_costs(fills)["2026-01-02"]["cost"] == 20
    previews = payload["transactions"]
    assert [preview["side"] for preview in previews] == ["buy", "sell", "none"]
    assert previews[0]["time"] == timestamp("2026-01-01T23:59:00")
    assert previews[1]["time"] == timestamp("2026-01-02T00:01:00")
    assert previews[2]["summary"]["cost"] is None
    assert previews[2]["timeEstimated"] is True

    async def check():
        async with database(tmp_path, schema_template, count=0) as (_, sessions, manager):
            async with sessions() as session:
                session.add(item)
                session.add(record(2, status="NOOP", symbol_results={}))
                await session.commit()
            await queue(sessions)
            await manager.run_once()
            batch = await snapshot(sessions)
            async with sessions() as session:
                page = await read_costs(session, 2, CostQuery(snapshot_id=batch["snapshot_id"], day="2026-01-02"))
                assert page["summary"]["cost"] == 20
                assert page["noop"] == 1
                assert page["count"] == 2
                interval = await read_costs(
                    session,
                    2,
                    CostQuery(
                        snapshot_id=batch["snapshot_id"],
                        start=timestamp("2026-01-01T23:59:00"),
                        end=timestamp("2026-01-02T00:01:00"),
                    ),
                )
                assert interval["summary"]["cost"] == 20
                assert interval["summary"]["count"] == 1
                exact = await read_costs(session, 2, CostQuery(snapshot_id=batch["snapshot_id"], record_id=item.id))
                assert exact["count"] == 1
                assert exact["summary"]["cost"] == 40
                assert exact["summary"]["count"] == 3

    asyncio.run(check())


@pytest.mark.parametrize(
    ("assets", "expected"),
    [
        ({}, None),
        ({"positions": []}, 0),
        ({"positions": [{"symbol": "A", "volume": 2}, {"symbol": "B", "volume": 0}]}, 1),
        ({"positions": [{"symbol": "A"}]}, None),
        ({"source": "assumed", "positions": []}, None),
        ({"source": "error", "positions": []}, None),
    ],
)
def test_execution_position_state_requires_valid_snapshot(assets, expected):
    payload, _ = project_execution(record(account_assets=assets))
    assert payload["positionCount"] == expected


def test_execution_strip_preserves_intraday_failed_and_empty_records():
    records = [
        record(1),
        record(2, status="NOOP", symbol_results={}, account_assets={"total_asset": 100, "positions": []}),
        record(3),
    ]
    records[1].created_at = "2026-01-01T10:00:00"
    records[2].created_at = "2026-01-01T11:00:00"
    records[2].is_success = 0
    from axile.server.db.models.performance import PerformanceSettings

    ranges, _, _ = compute_batch(
        records, [], [], [], PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0)
    )
    result = ranges["all"]["performance"]
    assert [point["record_id"] for point in result["points"]] == [1, 3]
    assert [row["record"]["id"] for row in result["executions"]] == [1, 2, 3]
    assert result["executions"][1]["positionCount"] == 0
    assert result["executions"][1]["noop"] is True
    assert result["executions"][2]["record"]["is_success"] == 0


def test_short_range_execution_history_is_read_from_all_snapshot(tmp_path, schema_template):
    """新快照只保存全量执行列表，旧快照仍可读已保存的范围列表。"""

    async def check():
        async with database(tmp_path, schema_template, count=120) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            async with sessions() as session:
                stored = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                assert "executions" in stored["all"]["performance"]
                assert "executions" not in stored["30"]["performance"]
                assert "executions" not in stored["90"]["performance"]
                all_rows = stored["all"]["performance"]["executions"]
                current = await read_snapshot(session, 2, "30")
                expected = [
                    row
                    for row in all_rows
                    if timestamp(current["result"]["baseline"])
                    <= timestamp(row["record"]["created_at"])
                    <= timestamp(current["result"]["end"])
                ]
                assert current["result"]["executions"] == expected

                # 升级前的同一范围副本读取结果与新快照的读时投影完全一致。
                stored["30"]["performance"]["executions"] = expected
                await session.execute(snapshots.update().values(ranges=stored))
                await session.commit()
                assert await read_snapshot(session, 2, "30") == current

                # 任意旧范围副本仍优先保留，避免改变已有快照的读取结果。
                legacy = {"legacy": True}
                stored["30"]["performance"]["executions"] = [legacy]
                await session.execute(snapshots.update().values(ranges=stored))
                await session.commit()
                assert (await read_snapshot(session, 2, "30"))["result"]["executions"] == [legacy]

    asyncio.run(check())


def test_failed_execution_preview_exposes_plan_and_confirmed_zero_fills():
    item = record(
        status="FAILED",
        symbol_results={
            "A": {
                "sizing": {"current_quantity": 2, "target_quantity": 5},
                "orders": [],
                "trades": [],
                "error": "超过最大下单量",
            }
        },
    )
    item.is_success = 0
    payload, _ = project_execution(item)
    assert payload["transactions"] == []
    assert payload["attempts"] == [{"symbol": "A", "planned": 3, "filled": 0, "reason": "超过最大下单量"}]


def test_migration_roundtrip_and_restart_running_state(tmp_path, schema_template):
    migration = _load_migration(_MIGRATIONS_DIR / "0012_performance_snapshots.py")

    async def check():
        async with database(tmp_path, schema_template, fresh_schema=True) as (engine, sessions, manager):
            async with engine.begin() as conn:

                def roundtrip(connection):
                    migration.op = Operations(MigrationContext.configure(connection))
                    migration.downgrade()
                    migration.upgrade()
                    assert len(sa.inspect(connection).get_columns("account_analysis")) == len(states.columns)

                await conn.run_sync(roundtrip)
            await queue(sessions)
            async with sessions() as session:
                await session.execute(states.update().values(running_version=1, requested=False))
                await session.commit()
            await manager.start()
            for _ in range(100):
                if (await snapshot(sessions))["status"] == "ready":
                    break
                await asyncio.sleep(0.02)
            await manager.stop()
            assert (await snapshot(sessions))["status"] == "ready"

    asyncio.run(check())


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (41.2, 41.2), ("65.5", 65.5), (None, None), (-1, None), (True, None), ("NaN", None)],
)
def test_execution_duration_projection(value, expected):
    payload, _ = project_execution(record(execution_time=value))
    assert payload["durationSec"] == expected


def test_legacy_execution_without_duration():
    payload, _ = project_execution(record())
    assert payload["durationSec"] is None


def test_journal_snapshot_trade_identity_filter_and_aggregation(tmp_path, schema_template):
    """Journal drilldown retains the chart scope and original execution identity."""

    async def check():
        async with database(tmp_path, schema_template, count=2) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            batch = await snapshot(sessions)
            async with sessions() as session:
                query = CostQuery(snapshot_id=batch["snapshot_id"], dimension="trade", symbol_search="a")
                page = await read_costs(session, 2, query)
                assert page["count"] == 2
                assert page["data_until"] == batch["data_until"]
                assert len({row["trade_id"] for row in page["data"]}) == 2
                assert all(row["record_id"] and row["execution_id"] for row in page["data"])
                assert page["summary"]["coveredValue"] == page["summary"]["value"]
                sells = await read_costs(session, 2, query.model_copy(update={"side": "sell"}))
                assert all(row["side"] == "sell" for row in sells["data"])
                assert sells["count"] == 0
                literal = await read_costs(session, 2, query.model_copy(update={"symbol_search": "%"}))
                assert literal["count"] == 0
                symbols = await read_costs(session, 2, query.model_copy(update={"dimension": "symbol"}))
                assert symbols["data"][0]["lastTime"] == max(row["time"] for row in page["data"])
                assert symbols["summary"] == page["summary"]
                record_id = page["data"][0]["record_id"]
                exact = await read_costs(session, 2, query.model_copy(update={"record_id": record_id}))
                assert all(row["record_id"] == record_id for row in exact["data"])

    asyncio.run(check())


def test_version_8_projection_is_rebuilt_with_execution_evidence(tmp_path, schema_template):
    """旧投影缺少新结果字段时，重启后从完整记录重建。"""
    from axile.server.db.models.analysis import cost_execution as executions

    async def check():
        async with database(tmp_path, schema_template, count=1) as (_, sessions, manager):
            await queue(sessions)
            await manager.run_once()
            initial = await snapshot(sessions)
            async with sessions() as session:
                payload = (await session.execute(sa.select(executions.c.payload))).scalar_one()
                payload["record"]["raw_result"] = {"status": "PARTIAL", "outcome": "not_reached"}
                payload.pop("tradeCount", None)
                await session.execute(executions.update().values(payload=payload))
                await session.execute(snapshots.update().values(logic_version="8"))
                await session.execute(states.update().values(logic_version="8"))
                await session.commit()
                with pytest.raises(HTTPException) as exc:
                    await read_costs(session, 2, CostQuery(snapshot_id=initial["snapshot_id"]))
                assert exc.value.status_code == 410
            await manager.start()
            await manager.stop()
            await manager.run_once()
            current = await snapshot(sessions)
            assert current["status"] == "ready"
            assert current["snapshot_id"] != initial["snapshot_id"]
            async with sessions() as session:
                page = await read_costs(session, 2, CostQuery(snapshot_id=current["snapshot_id"]))
                rebuilt = page["data"][0]
                expected, _ = project_execution(record())
                assert rebuilt["record"]["raw_result"] == expected["record"]["raw_result"]
                assert rebuilt["tradeCount"] == expected["tradeCount"] == 1

    asyncio.run(check())
