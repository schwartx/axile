"""Real Rust publication, atomic Python persistence and JSON-free result reads."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.analysis_cost_facts import COST_FACT_TABLES
from axile.server.analysis_observations import write_observation_fact
from axile.server.analysis_source import write_source_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.analysis_source import source_execution
from axile.server.db.models.performance import PerformanceSettings
from axile.server.native_analysis import NativeAnalysis, NativeAnalysisError, NativeAnalysisProtocolError
from axile.server.performance_analysis import LOGIC_VERSION, AnalysisManager, compute_batch, enqueue
from axile.server.performance_replay import ENGINE_VERSION
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_native_analysis_contract import _cost_records, _same
from tests.unit.server.test_native_analysis_contract import native_binary as native_binary
from tests.unit.server.test_performance_analysis import _create_schema

pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def fixed_calendar(monkeypatch):
    def evaluate(channel, day):
        return CalendarDayDecision(channel=str(channel), day=day, status=CalendarDecisionStatus.NOT_REQUIRED)

    monkeypatch.setattr("axile.server.performance.evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr("axile.server.analysis_calendar.evaluate_channel_calendar_day", evaluate)


@asynccontextmanager
async def _database(tmp_path, binary, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "required")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(binary))
    path = tmp_path / "structured.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(_create_schema)
    records = _cost_records()
    # Python's timestamp()*1000 rounds this boundary differently from a direct
    # integer-microsecond / 1000 conversion. The final execution's estimated
    # trades and failure event must remain inside the inclusive range endpoint.
    records[2].created_at = "2026-01-05T09:00:00.000003+08:00"
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.001)
    async with sessions() as session:
        session.add(build_account(id=7, backtest_weight_type="cs", backtest_fee_rate=0.001))
        await session.commit()
        for record in records:
            parent = ExecuteRecord(**vars(record))
            session.add(parent)
            await session.flush()
            await write_observation_fact(session, parent)
            assert await write_source_facts(session, parent)
        await session.commit()
    try:
        yield path, engine, sessions, AnalysisManager(sessions), records, settings
    finally:
        await engine.dispose()


async def _queue(sessions):
    async with sessions() as session:
        await enqueue(session, 7, force=True)


async def _state(sessions):
    async with sessions() as session:
        return (await session.execute(sa.select(states).where(states.c.account_id == 7))).mappings().one()


async def _counts(sessions):
    async with sessions() as session:
        return {
            table.name: await session.scalar(sa.select(sa.func.count()).select_from(table))
            for table in (snapshots, *RESULT_TABLES, *COST_FACT_TABLES)
        }


async def _snapshot(native, range_key="all"):
    encoded = await native.query(
        "snapshot", account_id=7, range=range_key, logic_version=LOGIC_VERSION, engine_version=ENGINE_VERSION
    )
    return json.loads(encoded)


def test_native_publication_matches_oracle_without_reparsing_history(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, manager, records, settings):
            expected, _, _ = compute_batch(records, [], [], [], settings, trade_channel="ctp")

            def no_history(*_args, **_kwargs):
                raise AssertionError("A ready typed source must not be reparsed for a new analysis round")

            monkeypatch.setattr("axile.server.analysis_source.source_fact_values", no_history)
            monkeypatch.setattr("axile.server.analysis_observations.observation_fact_values", no_history)
            monkeypatch.setattr("axile.server.performance_analysis._prepare_records", no_history)
            await _queue(sessions)
            assert await manager.run_once()
            native = NativeAnalysis(binary=native_binary, database=path)
            for range_key in ("30", "90", "all"):
                actual = await _snapshot(native, range_key)
                assert actual["status"] == "ready"
                performance = expected[range_key]["performance"]
                performance.setdefault("executions", expected["all"]["performance"]["executions"])
                _same(actual["result"], performance)
                _same(actual["daily_costs"], expected[range_key]["daily_costs"])
                _same(actual["events"], expected[range_key]["events"])
                assert actual["event_count"] == expected[range_key]["event_count"]
            async with sessions() as session:
                row = (await session.execute(sa.select(snapshots))).mappings().one()
                assert row["result_schema_version"] == 2
                assert row["ranges"] == row["settings"] == {}
                assert row["backtest_weight_type"] == "cs"
                assert row["backtest_fee_rate"] == 0.001
                for table_name in ("performance_cost_execution", "performance_cost_trade"):
                    payloads = (await session.execute(sa.text(f"SELECT payload FROM {table_name}"))).scalars().all()
                    assert payloads and set(payloads) == {"{}"}
            # A second full round must also consume the frozen source facts.
            await _queue(sessions)
            assert await manager.run_once()
            assert (await _snapshot(native))["status"] == "ready"

    asyncio.run(check())


def test_native_published_results_ignore_poisoned_legacy_json(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            native = NativeAnalysis(binary=native_binary, database=path)
            before = await _snapshot(native)
            query = dict(
                account_id=7,
                snapshot_id=before["snapshot_id"],
                range="all",
                dimension="execution",
                limit=100,
                logic_version=LOGIC_VERSION,
                engine_version=ENGINE_VERSION,
            )
            costs = json.loads(await native.query("costs", **query))
            async with sessions() as session:
                for table, columns in (
                    ("performance_snapshot", ("ranges", "settings")),
                    ("performance_cost_execution", ("payload",)),
                    ("performance_cost_trade", ("payload",)),
                ):
                    for column in columns:
                        await session.execute(sa.text(f"UPDATE {table} SET {column}='invalid-json'"))
                await session.commit()
            _same(await _snapshot(native), before)
            _same(json.loads(await native.query("costs", **query)), costs)

    asyncio.run(check())


@pytest.mark.parametrize("interruption", ["process_error", "invalid_columns", "cancelled", "wrong_complete_version"])
def test_failed_publication_never_exposes_partial_results(native_binary, tmp_path, monkeypatch, interruption):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            original = NativeAnalysis.query_stream

            @asynccontextmanager
            async def interrupted(self, command, **request):
                async with original(self, command, **request) as frames:

                    async def corrupt():
                        async for frame in frames:
                            if interruption == "wrong_complete_version":
                                if frame.get("type") == "complete":
                                    frame = {**frame, "source_version": frame["source_version"] + 1}
                                yield frame
                                continue
                            if interruption == "invalid_columns":
                                yield {
                                    "type": "rows",
                                    "table": "performance_result_point",
                                    "columns": ["invalid"],
                                    "rows": [[0]],
                                }
                                return
                            yield frame
                            if frame.get("type") == "rows":
                                if interruption == "cancelled":
                                    raise asyncio.CancelledError
                                raise NativeAnalysisProtocolError("synthetic interrupted native stream")

                    yield corrupt()

            monkeypatch.setattr(NativeAnalysis, "query_stream", interrupted)
            await _queue(sessions)
            if interruption == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await manager.run_once()
            else:
                assert await manager.run_once()
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["running_version"] is None and current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_source_change_after_staging_discards_publication(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            publish = manager._publish_structured

            async def change_source(row, settings, staged):
                async with sessions() as session:
                    await session.execute(
                        states.update()
                        .where(states.c.account_id == 7)
                        .values(source_version=states.c.source_version + 1)
                    )
                    await session.commit()
                await publish(row, settings, staged)

            monkeypatch.setattr(manager, "_publish_structured", change_source)
            await _queue(sessions)
            assert await manager.run_once()
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["running_version"] is None and current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_source_change_before_rust_snapshot_is_rejected(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            original = NativeAnalysis.query_stream

            @asynccontextmanager
            async def change_source(self, command, **request):
                async with sessions() as session:
                    await session.execute(
                        states.update()
                        .where(states.c.account_id == 7)
                        .values(source_version=states.c.source_version + 1)
                    )
                    await session.commit()
                async with original(self, command, **request) as frames:
                    yield frames

            monkeypatch.setattr(NativeAnalysis, "query_stream", change_source)
            await _queue(sessions)
            assert await manager.run_once()
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["running_version"] is None and current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_transaction_failure_after_cost_copy_rolls_back_every_published_row(native_binary, tmp_path, monkeypatch):
    import axile.server.analysis_publication as publication
    import axile.server.performance_analysis as analysis

    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            original = publication.copy_source_cost_facts

            async def fail_after_copy(session, snapshot_id, account_id):
                await original(session, snapshot_id, account_id)
                raise RuntimeError("synthetic failure after all immutable costs were copied")

            monkeypatch.setattr(publication, "copy_source_cost_facts", fail_after_copy)
            if hasattr(analysis, "copy_source_cost_facts"):
                monkeypatch.setattr(analysis, "copy_source_cost_facts", fail_after_copy)
            await _queue(sessions)
            assert await manager.run_once()
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    [
        {"projection_version": 0, "projection_error": "unsupported historical scalar"},
        {"account_id": 99},
        {"execution_id": "stale-execution"},
        {"created_at": "2025-01-01T00:00:00"},
        {"is_success": 0},
        {"trade_count": 999},
    ],
)
def test_native_rejects_unsupported_or_mismatched_source_headers(native_binary, tmp_path, monkeypatch, change):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, _, _, settings):
            async with sessions() as session:
                await session.execute(
                    source_execution.update().where(source_execution.c.record_id == 1).values(**change)
                )
                await session.commit()
            native = NativeAnalysis(binary=native_binary, database=path)
            with pytest.raises(NativeAnalysisError) as error:
                await native.query(
                    "performance",
                    account_id=7,
                    settings=settings.model_dump(),
                    require_source_facts=True,
                    require_observation_facts=True,
                )
            assert error.value.code == "SOURCE_FACTS_INCOMPLETE"

    asyncio.run(check())
