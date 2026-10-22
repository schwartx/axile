"""Typed cost readers preserve legacy responses without reading JSON projections."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION, COST_FACT_TABLES, iter_cost_fact_batches
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.db.models.analysis import cost_trade as trades
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from axile.server.performance_costs import execution_list_payload, summarize, timestamp
from axile.server.performance_details import CostQuery, _cursor, read_costs
from tests.unit.server._business_analysis_support import record
from tests.unit.server.test_analysis_cost_facts import _project_rows, _ranges, _rich_projection


def _history():
    projected, costs = _rich_projection()
    failed = record(3)
    failed.is_success = 0
    failed.result.update(status="FAILED", error="拒绝", symbol_results={"C": {"status": "REJECTED"}})
    noop = record(4)
    noop.result.update(status="NOOP", symbol_results={"D": {"status": "NOOP"}})
    extra, extra_costs = _project_rows([record(2, account_assets={"total_asset": 102, "positions": []}), failed, noop])
    projected.extend(extra)
    costs.extend(extra_costs)
    costs = [{**row, "id": index} for index, row in enumerate(costs)]
    ranges = _ranges()
    for key in ("all", "90"):
        ranges[key]["performance"]["end"] = "2026-01-04T09:00:00"
    return projected, costs, ranges


@asynccontextmanager
async def _database(path=None):
    uri = f"sqlite+aiosqlite:///{path}" if path is not None else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(uri)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    projected, costs, ranges = _history()
    try:
        async with engine.begin() as connection:
            if path is not None:
                await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.run_sync(
                lambda sync: snapshots.metadata.create_all(sync, tables=[snapshots, *COST_FACT_TABLES])
            )
        async with sessions() as session:
            await session.execute(
                snapshots.insert().values(
                    id="snapshot",
                    account_id=2,
                    source_version=1,
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                    computed_at="2026-01-05T00:00:00",
                    data_until="2026-01-04T09:00:00",
                    settings={},
                    ranges=ranges,
                    cost_schema_version=0,
                )
            )
            for table, rows in iter_cost_fact_batches("snapshot", projected, costs, ranges, batch_size=2):
                await session.execute(table.insert(), rows)
            await session.commit()
        yield engine, sessions, projected, costs, ranges
    finally:
        await engine.dispose()


async def _discard_legacy_json(sessions):
    async with sessions() as session:
        await session.execute(snapshots.update().values(cost_schema_version=COST_FACT_SCHEMA_VERSION, ranges={}))
        await session.execute(executions.update().values(payload={}))
        await session.execute(trades.update().values(payload={}))
        await session.commit()


def _reject_json_sql(connection, cursor, statement, parameters, context, executemany):
    normalized = statement.lower().replace('"', "").replace("`", "")
    assert "json_extract" not in normalized, statement
    assert "json_type" not in normalized, statement
    assert ".payload" not in normalized, statement
    assert ".ranges" not in normalized, statement


async def _pages(sessions, query):
    pages = []
    for _ in range(10):
        async with sessions() as session:
            page = await read_costs(session, 2, query)
        pages.append(page)
        if page["next_cursor"] is None:
            return pages
        query = query.model_copy(update={"cursor": page["next_cursor"]})
    pytest.fail("cost cursor did not exhaust the bounded fixture")


@pytest.mark.parametrize("dimension", ["execution", "symbol", "trade", "summary"])
@pytest.mark.parametrize("sort", ["time", "cost"])
@pytest.mark.parametrize("filters", [{}, {"symbol": "A"}, {"side": "sell"}], ids=["all", "symbol", "side"])
def test_typed_cost_pages_match_legacy_after_json_is_removed(dimension, sort, filters):
    """Scalar filters, sorting and cursor pages retain every response field."""

    async def check():
        async with _database() as (engine, sessions, _, _, _):
            query = CostQuery(snapshot_id="snapshot", dimension=dimension, sort=sort, limit=1, **filters)
            expected = await _pages(sessions, query)
            if not filters and dimension != "summary":
                assert len(expected) > 1
            await _discard_legacy_json(sessions)
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_json_sql)
            assert await _pages(sessions, query) == expected

    asyncio.run(check())


@pytest.mark.parametrize("filters", [{"symbol_search": "a"}, {"symbol_search": "%"}, {"side": "none"}])
def test_typed_trade_search_and_missing_side_match_legacy(filters):
    """Case-insensitive search escapes SQL wildcards and empty matches stay empty."""

    async def check():
        async with _database() as (engine, sessions, _, _, _):
            query = CostQuery(snapshot_id="snapshot", dimension="trade", **filters)
            expected = await _pages(sessions, query)
            await _discard_legacy_json(sessions)
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_json_sql)
            assert await _pages(sessions, query) == expected

    asyncio.run(check())


@pytest.mark.parametrize(
    ("baseline", "end"),
    [
        ("2026-01-01T09:00:00", "2026-01-04T09:00:00"),
        ("2026-01-01T09:00:00", "2026-01-01T09:00:00"),
        ("2026-01-02T09:00:00", "2026-01-03T09:00:00"),
        (None, None),
    ],
)
def test_execution_results_rebuild_full_snapshot_projection_without_json(baseline, end):
    """Range history retains statuses, positions, attempts, transactions and fees."""
    from axile.server.analysis_cost_reader import read_execution_results

    async def check():
        async with _database() as (engine, sessions, projected, costs, _):
            start = timestamp(baseline) if baseline else 0
            stop = timestamp(end) if end else -1
            expected = [
                execution_list_payload(row["payload"])
                | {
                    "summary": summarize(
                        [trade["payload"] for trade in costs if trade["record_id"] == row["record_id"]]
                    )
                }
                for row in sorted(projected, key=lambda row: (row["time"], row["record_id"]))
                if start <= row["time"] <= stop
            ]
            await _discard_legacy_json(sessions)
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_json_sql)
            async with sessions() as session:
                assert await read_execution_results(session, "snapshot", baseline, end) == expected

    asyncio.run(check())


@pytest.mark.parametrize("dimension", ["execution", "symbol", "trade", "summary"])
@pytest.mark.parametrize(
    "filters",
    [
        {"day": "2026-01-02"},
        {"start": timestamp("2026-01-01T09:00:00"), "end": timestamp("2026-01-03T09:00:00")},
        {"record_id": 1},
        {"range": "30"},
    ],
    ids=["day", "interval", "record", "empty-range"],
)
def test_typed_cost_time_and_record_scopes_match_legacy(dimension, filters):
    """Time bounds, exact record identity and empty ranges retain legacy scope."""

    async def check():
        async with _database() as (engine, sessions, _, _, _):
            query = CostQuery(snapshot_id="snapshot", dimension=dimension, limit=1, **filters)
            expected = await _pages(sessions, query)
            await _discard_legacy_json(sessions)
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_json_sql)
            assert await _pages(sessions, query) == expected

    asyncio.run(check())


def test_execution_result_keyset_batches_preserve_equal_timestamp_records(monkeypatch):
    """Small pages use record identity to advance through equal timestamps."""
    from axile.server import analysis_cost_reader

    monkeypatch.setattr(analysis_cost_reader, "READ_BATCH_SIZE", 2)

    async def check():
        async with _database() as (engine, sessions, projected, costs, _):
            created_at = "2026-01-01T09:00:00"
            for row in projected:
                row["payload"]["record"]["created_at"] = created_at
            expected = [
                execution_list_payload(row["payload"])
                | {
                    "summary": summarize(
                        [trade["payload"] for trade in costs if trade["record_id"] == row["record_id"]]
                    )
                }
                for row in sorted(projected, key=lambda row: row["record_id"])
            ]
            async with sessions() as session:
                await session.execute(executions.update().values(time=timestamp(created_at), created_at=created_at))
                await session.commit()
            await _discard_legacy_json(sessions)
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_json_sql)
            async with sessions() as session:
                actual = await analysis_cost_reader.read_execution_results(session, "snapshot", created_at, created_at)
            assert actual == expected
            assert [row["record"]["id"] for row in actual] == [1, 2, 3, 4]

    asyncio.run(check())


def test_cost_reader_preserves_caller_uncommitted_writes():
    """A nested read transaction must not discard the caller's pending changes."""

    async def check():
        async with _database() as (_, sessions, _, _, _):
            await _discard_legacy_json(sessions)
            async with sessions() as session:
                original = await session.scalar(sa.select(snapshots.c.data_until))
                await session.execute(snapshots.update().values(data_until="uncommitted"))
                result = await read_costs(session, 2, CostQuery(snapshot_id="snapshot"))
                assert result["data_until"] == "uncommitted"
                assert session.in_transaction()
                await session.rollback()
            async with sessions() as session:
                assert await session.scalar(sa.select(snapshots.c.data_until)) == original

    asyncio.run(check())


def test_cost_reader_keeps_one_snapshot_during_concurrent_retention(tmp_path):
    """WAL readers retain metadata and cost facts from before concurrent deletion."""
    path = tmp_path / "read-snapshot.db"

    async def check():
        async with _database(path) as (engine, sessions, _, _, _):
            await _discard_legacy_json(sessions)
            query = CostQuery(snapshot_id="snapshot", dimension="execution", limit=100)
            async with sessions() as session:
                expected = await read_costs(session, 2, query)
            writer = sa.create_engine(f"sqlite:///{path}")
            removed = False

            def retain_after_metadata(connection, cursor, statement, parameters, context, executemany):
                nonlocal removed
                if removed or "from performance_snapshot" not in statement.lower():
                    return
                removed = True
                with writer.begin() as transaction:
                    for table in COST_FACT_TABLES:
                        transaction.execute(table.delete())
                    transaction.execute(snapshots.delete())

            try:
                sa.event.listen(engine.sync_engine, "after_cursor_execute", retain_after_metadata)
                async with sessions() as session:
                    actual = await read_costs(session, 2, query)
                assert removed
                assert actual == expected
                async with sessions() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(snapshots)) == 0
                    assert await session.scalar(sa.select(sa.func.count()).select_from(executions)) == 0
                    assert await session.scalar(sa.select(sa.func.count()).select_from(trades)) == 0
            finally:
                sa.event.remove(engine.sync_engine, "after_cursor_execute", retain_after_metadata)
                writer.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("dimension", ["execution", "symbol", "trade", "summary"])
@pytest.mark.parametrize("filters", [{}, {"limit": 1}, {"limit": 1, "symbol_search": "a", "side": "sell"}])
def test_native_cost_bytes_pass_through_route(tmp_path, monkeypatch, dimension, filters):
    import json
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from axile.server._native import _core
    from axile.server.api.deps import get_db
    from axile.server.api.routes import account_performance as routes

    database = tmp_path / "native-costs.db"

    async def check():
        async with _database(database) as (_, sessions, _, _, _):
            await _discard_legacy_json(sessions)
            query = CostQuery(snapshot_id="snapshot", dimension=dimension, **filters)
            content = _core.query_costs(
                _core.Store(str(database)),
                2,
                query.snapshot_id,
                query.range,
                query.dimension,
                query.sort,
                query.day,
                query.start,
                query.end,
                query.record_id,
                query.symbol,
                query.symbol_search,
                query.side,
                query.limit,
                _cursor(query, 2)[0],
                query.cursor,
                LOGIC_VERSION,
                ENGINE_VERSION,
            )
            assert isinstance(content, bytes)
            async with sessions() as session:
                expected = await read_costs(session, 2, query)
            assert json.loads(content) == expected
            monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(str(database)))
            monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock())

            async def get_session():
                async with sessions() as session:
                    yield session

            app = FastAPI()
            app.include_router(routes.router)
            app.dependency_overrides[get_db] = get_session
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/performance/2/costs", params=query.model_dump(exclude_none=True))
                assert response.status_code == 200, response.text
                assert response.content == content
                assert response.headers["content-type"] == "application/json"
                if expected["next_cursor"] is not None:
                    next_query = query.model_copy(update={"cursor": expected["next_cursor"]})
                    async with sessions() as session:
                        next_expected = await read_costs(session, 2, next_query)
                    response = await client.get("/performance/2/costs", params=next_query.model_dump(exclude_none=True))
                    assert response.status_code == 200, response.text
                    assert response.json() == next_expected
                    invalid = next_query.model_copy(update={"limit": 2})
                    response = await client.get("/performance/2/costs", params=invalid.model_dump(exclude_none=True))
                    assert response.status_code == 400
                    assert response.json()["detail"]["code"] == "INVALID_COST_CURSOR"

    asyncio.run(check())
