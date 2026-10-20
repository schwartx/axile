"""Permanent scalar analysis facts preserve semantics and bound legacy backfills."""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import analysis_source
from axile.server.analysis_cost_facts import iter_cost_fact_batches
from axile.server.db.models import ExecuteRecord
from axile.server.performance_analysis import _events
from axile.server.performance_costs import project_execution, timestamp
from tests.unit.server._business_analysis_support import persist_records
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_performance_analysis import record


def _rich_record():
    source = record()
    source.is_success = 0
    raw = source.result
    raw.update(status="PARTIAL", error="未全部成交", reason_code=None, msg="")
    raw["account_assets"]["positions"] = [
        {"symbol": "A", "volume": -2, "direction": "SHORT"},
        {"symbol": "FLAT", "volume": 0, "direction": "LONG"},
    ]
    raw["symbol_results"]["A"].update(status="PARTIAL", reason_code=None)
    raw["symbol_results"]["A"]["sizing"].update(current_quantity=0, target_quantity=3)
    raw["symbol_results"]["A"]["orders"][0]["filled_volume"] = 2
    raw["symbol_results"]["B"] = {
        "status": "SUCCEEDED",
        "first_tick": {"last_price": 50},
        "orders": [{"order_id": "sell", "direction": "SELL"}],
        "trades": [{"order_id": "sell", "trade_price": 51, "trade_volume": 1}],
    }
    return source


def _legacy_facts(source):
    payload, trades = project_execution(source)
    projected = [
        {
            "record_id": source.id,
            "time": timestamp(source.created_at),
            "success": source.is_success == 1,
            "noop": payload["noop"],
            "payload": payload,
        }
    ]
    costs = [
        {
            "id": index,
            "record_id": source.id,
            "symbol": trade["symbol"],
            "time": trade["time"],
            "day": trade["day"],
            "value": trade["value"],
            "cost": trade["cost"],
            "loss_bp": trade["lossBp"],
            "fee": trade["fee"],
            "fee_currency": trade["feeCurrency"],
            "estimated": trade["timeEstimated"],
            "payload": trade,
        }
        for index, trade in enumerate(trades)
    ]
    result = defaultdict(list)
    for table, rows in iter_cost_fact_batches("legacy", projected, costs, {}):
        result[table.name].extend(rows)
    return result


def _table(name):
    return SQLModel.metadata.tables["analysis_source_" + name]


@asynccontextmanager
async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(build_account(id=2, account_control_preset="default"))
            await session.commit()
        yield engine, sessions
    finally:
        await engine.dispose()


def test_source_facts_match_existing_cost_previews_and_event_semantics():
    """Typed permanent facts retain null/absent evidence without storing JSON copies."""
    source = _rich_record()
    scalar, children = analysis_source.source_fact_values(source)
    expected = _legacy_facts(source)
    for name, value in expected["performance_cost_execution"][0].items():
        if name not in {"snapshot_id", "payload"}:
            assert scalar[name] == value
    assert scalar["account_id"] == source.account_id
    assert scalar["projection_version"] == analysis_source.SOURCE_FACT_SCHEMA_VERSION
    assert scalar["projection_error"] is None
    event = _events([source], [], [])[0]
    assert (scalar["event_tag"], scalar["event_text"]) == (event["tag"], event["text"])
    for name in ("execution_symbol", "position", "attempt", "transaction", "transaction_fee", "trade"):
        actual_rows = children[_table(name)]
        legacy_rows = expected["performance_cost_" + name]
        assert len(actual_rows) == len(legacy_rows)
        for actual, legacy in zip(actual_rows, legacy_rows, strict=True):
            for key, value in legacy.items():
                if key not in {"snapshot_id", "payload", "id"}:
                    assert actual[key] == value
    assert [row["trade_index"] for row in children[_table("trade")]] == [0, 1]
    for table in (_table("execution"), *children):
        assert not {"raw_input", "raw_result", "payload", "snapshot_id"} & set(table.c.keys())
        assert not any(isinstance(column.type, sa.JSON) for column in table.c)


@pytest.mark.parametrize("raw_result", [{"status": {"legacy": "shape"}}, {"error": ["legacy", "message"]}])
def test_source_writer_marks_unsupported_records_without_losing_business_write(tmp_path, raw_result):
    """A legacy malformed scalar is an explicit marker, not a failed business transaction."""

    async def check():
        async with _database(tmp_path) as (_, sessions):
            async with sessions() as session:
                source = record()
                source.result = raw_result
                await persist_records(session, [source])
                await session.flush()
                assert await analysis_source.write_source_facts(session, source) is False
                await session.commit()
            async with sessions() as session:
                assert await session.get(ExecuteRecord, source.id) is not None
                row = (await session.execute(sa.select(_table("execution")))).mappings().one()
                assert row["projection_version"] == 0
                assert row["projection_error"]
                for name in ("trade", "execution_symbol", "position", "attempt", "transaction", "transaction_fee"):
                    assert (
                        await session.execute(sa.select(sa.func.count()).select_from(_table(name)))
                    ).scalar_one() == 0

    asyncio.run(check())


def test_source_writer_projects_once_and_preserves_caller_transaction(tmp_path, monkeypatch):
    """Projection occurs once per write and the caller can roll back facts with the execution."""
    calls = []
    original = analysis_source.project_execution

    def observed(source):
        calls.append(source.id)
        return original(source)

    monkeypatch.setattr(analysis_source, "project_execution", observed)

    async def check():
        async with _database(tmp_path) as (_, sessions):
            async with sessions() as session:
                source = _rich_record()
                await persist_records(session, [source])
                await session.flush()
                assert await analysis_source.write_source_facts(session, source) is True
                assert calls == [source.id]
                await session.rollback()
            async with sessions() as session:
                assert await session.get(ExecuteRecord, source.id) is None
                assert (
                    await session.execute(sa.select(sa.func.count()).select_from(_table("execution")))
                ).scalar_one() == 0

    asyncio.run(check())


def test_source_rewrite_removes_obsolete_children_and_keeps_explicit_flat(tmp_path):
    """Reprojection replaces every child even without SQLite foreign-key enforcement."""

    async def check():
        async with _database(tmp_path) as (_, sessions):
            async with sessions() as session:
                source = _rich_record()
                await persist_records(session, [source])
                await session.flush()
                assert await analysis_source.write_source_facts(session, source)
                await session.commit()
                source.is_success = 1
                source.result = {"status": "NOOP", "account_assets": {"positions": []}, "symbol_results": {}}
                await persist_records(session, [source])
                assert await analysis_source.write_source_facts(session, source)
                await session.commit()
            async with sessions() as session:
                execution = (await session.execute(sa.select(_table("execution")))).mappings().one()
                assert execution["positions_present"] is True
                assert execution["position_count"] == 0
                assert execution["noop"] is True
                assert execution["event_tag"] is None
                assert execution["event_text"] is None
                for name in ("trade", "execution_symbol", "position", "attempt", "transaction", "transaction_fee"):
                    assert (
                        await session.execute(sa.select(sa.func.count()).select_from(_table(name)))
                    ).scalar_one() == 0

    asyncio.run(check())


def test_source_backfill_is_bounded_resumable_and_does_not_repeat_unsupported(tmp_path, monkeypatch):
    """Only missing records enter bounded batches; unsupported markers remain visible."""
    calls = []
    original = analysis_source.project_execution

    def observed(source):
        calls.append(source.id)
        return original(source)

    monkeypatch.setattr(analysis_source, "project_execution", observed)

    async def check():
        async with _database(tmp_path) as (_, sessions):
            async with sessions() as session:
                rows = [record(index) for index in range(1, 6)]
                rows[2].result = {"status": {"legacy": "shape"}}
                await persist_records(session, rows)
                await session.commit()
            async with sessions() as session:
                first = await analysis_source.backfill_source_facts(session, 2, batch_size=2, through_record_id=4)
                assert (first.processed, first.complete, first.unsupported) == (2, False, 0)
                await session.commit()
                second = await analysis_source.backfill_source_facts(session, 2, batch_size=2, through_record_id=4)
                assert (second.processed, second.complete, second.unsupported) == (2, True, 1)
                await session.commit()
                repeated = await analysis_source.backfill_source_facts(session, 2, batch_size=2, through_record_id=4)
                assert (repeated.processed, repeated.complete, repeated.unsupported) == (0, True, 1)
                assert calls == [1, 2, 3, 4]
                assert set((await session.execute(sa.select(_table("execution").c.record_id))).scalars()) == {
                    1,
                    2,
                    3,
                    4,
                }
            async with sessions() as session:
                final = await analysis_source.ensure_source_facts(session, 2, batch_size=2)
                assert (final.processed, final.complete, final.unsupported) == (1, True, 1)
            assert calls == [1, 2, 3, 4, 5]
            async with sessions() as session:
                assert (
                    await session.execute(sa.select(sa.func.count()).select_from(_table("execution")))
                ).scalar_one() == 5

    asyncio.run(check())


@pytest.mark.parametrize("batch_size", [0, 101])
def test_source_backfill_rejects_unbounded_or_empty_batches(tmp_path, batch_size):
    async def check():
        async with _database(tmp_path) as (_, sessions):
            async with sessions() as session:
                with pytest.raises(ValueError, match="batch_size"):
                    await analysis_source.backfill_source_facts(session, 2, batch_size=batch_size)

    asyncio.run(check())


def test_source_ensure_commits_bounded_batches_at_a_fixed_record_horizon(tmp_path, monkeypatch):
    """New arrivals cannot extend a backfill forever or cause one long write transaction."""
    observed_batches = []
    original = analysis_source.backfill_source_facts

    async def observed(session, account_id, batch_size=100, **kwargs):
        progress = await original(session, account_id, batch_size, **kwargs)
        observed_batches.append((kwargs["through_record_id"], progress.processed))
        if len(observed_batches) == 1:
            await persist_records(session, [record(5)])
        return progress

    monkeypatch.setattr(analysis_source, "backfill_source_facts", observed)

    async def check():
        async with _database(tmp_path) as (engine, sessions):
            async with sessions() as session:
                await persist_records(session, [record(index) for index in range(1, 5)])
                await session.commit()
            statements = []

            def capture(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with sessions() as session:
                    result = await analysis_source.ensure_source_facts(session, 2, batch_size=2)
            finally:
                sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert (result.processed, result.complete, result.unsupported) == (4, True, 0)
            assert observed_batches == [(4, 2), (4, 2)]
            assert sum(statement.upper() == "BEGIN IMMEDIATE" for statement in statements) == 2
            header_reads = [
                statement.lower()
                for statement in statements
                if "from executerecord" in statement.lower() and "limit" in statement.lower()
            ]
            assert len(header_reads) == 2
            assert all("raw_input" not in statement and "raw_result" not in statement for statement in statements)
            async with sessions() as session:
                assert await session.get(ExecuteRecord, 5) is not None
                assert set((await session.execute(sa.select(_table("execution").c.record_id))).scalars()) == {
                    1,
                    2,
                    3,
                    4,
                }

    asyncio.run(check())
