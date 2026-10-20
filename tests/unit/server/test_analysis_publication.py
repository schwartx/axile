"""Atomic typed publication never loads raw histories or JSON projections."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_cost_facts import COST_FACT_TABLES
from axile.server.analysis_publication import copy_source_cost_facts
from axile.server.business_facts.repository import store_execution_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_snapshot, cost_execution, cost_trade
from axile.server.db.models.analysis_source import (
    source_attempt,
    source_execution,
    source_execution_symbol,
    source_position,
    source_trade,
    source_transaction,
    source_transaction_fee,
)
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = []

    @sa.event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(build_account(id=2))
        session.add(build_account(id=3))
        await session.commit()
    try:
        yield sessions, statements
    finally:
        await engine.dispose()


def _header(record_id=1, account_id=2):
    return {
        "record_id": record_id,
        "account_id": account_id,
        "projection_version": 1,
        "time": 1767229200000.0,
        "success": True,
        "noop": False,
        "execution_id": f"execution-{record_id}",
        "created_at": "2026-01-01T09:00:00",
        "is_success": 1,
        "key": f"record-{record_id}",
        "positions_present": True,
        "position_count": 0,
        "trade_count": 0,
        "raw_present": 1,
        "raw_status": None,
        "has_symbol_results": True,
    }


async def _record(session, record_id=1, account_id=2):
    header = _header(record_id, account_id)
    record = ExecuteRecord(
        id=record_id,
        account_id=account_id,
        execution_id=header["execution_id"],
        created_at=header["created_at"],
        is_success=header["is_success"],
    )
    session.add(record)
    await session.flush()
    await store_execution_facts(session, record, raw_input={}, raw_result={"status": None, "symbol_results": {}})
    return header


async def _snapshot(session, snapshot_id="snapshot", account_id=2):
    await session.execute(
        analysis_snapshot.insert(),
        {
            "id": snapshot_id,
            "account_id": account_id,
            "source_version": 1,
            "logic_version": "typed",
            "engine_version": "equity-ledger-1",
            "computed_at": "2026-01-03T00:00:00",
            "settings": {},
            "ranges": {},
            "cost_schema_version": 1,
        },
    )


def test_copy_cost_facts_is_sql_only_account_scoped_and_preserves_ids():
    async def check():
        async with _database() as (sessions, statements):
            async with sessions() as session:
                # Physical insert and created-at order intentionally disagree with PK order.
                for record_id, account_id in ((8, 2), (1, 2), (9, 3)):
                    header = await _record(session, record_id, account_id)
                    header["trade_count"] = 2
                    await session.execute(source_execution.insert(), header)
                    await session.execute(
                        source_trade.insert(),
                        [
                            {
                                "record_id": record_id,
                                "trade_index": index,
                                "symbol": f"A{index}",
                                "time": header["time"],
                                "day": "2026-01-01",
                                "estimated": False,
                                "side": "buy",
                                "quantity": 1.0,
                                "price": 2.0,
                                "fee": 0.0,
                                "fee_currency": "CNY",
                                "value": None,
                                "cost": None,
                            }
                            for index in (2, 0)
                        ],
                    )
                await session.execute(
                    source_execution_symbol.insert(),
                    {
                        "record_id": 1,
                        "symbol": "A",
                        "present": 17,
                        "status": None,
                        "reason_code": "",
                    },
                )
                await session.execute(
                    source_position.insert(),
                    {
                        "record_id": 1,
                        "idx": 0,
                        "symbol": "A",
                        "quantity": -2.0,
                        "direction": "SHORT",
                    },
                )
                await session.execute(
                    source_attempt.insert(),
                    {
                        "record_id": 1,
                        "idx": 0,
                        "symbol": "A",
                        "planned": 2.0,
                        "filled": 1.0,
                        "reason": "partial",
                    },
                )
                await session.execute(
                    source_transaction.insert(),
                    {
                        "record_id": 1,
                        "idx": 0,
                        "symbol": "A",
                        "side": "buy",
                        "time_estimated": True,
                        "amount_complete": False,
                        "fee_covered": 1,
                    },
                )
                await session.execute(
                    source_transaction_fee.insert(),
                    {
                        "record_id": 1,
                        "transaction_idx": 0,
                        "currency": "CNY",
                        "value": 0.0,
                    },
                )
                await _snapshot(session)
                statements.clear()
                await copy_source_cost_facts(session, "snapshot", 2)
                publication_statements = list(statements)
                executions = (
                    (await session.execute(sa.select(cost_execution).order_by(cost_execution.c.record_id)))
                    .mappings()
                    .all()
                )
                assert [row["record_id"] for row in executions] == [1, 8]
                assert all(row["payload"] == {} for row in executions)
                assert executions[0]["raw_status"] is None
                assert executions[0]["raw_present"] == 1
                copied_trades = (
                    (await session.execute(sa.select(cost_trade).order_by(cost_trade.c.id))).mappings().all()
                )
                assert [(row["id"], row["record_id"], row["symbol"]) for row in copied_trades] == [
                    (0, 1, "A0"),
                    (1, 1, "A2"),
                    (2, 8, "A0"),
                    (3, 8, "A2"),
                ]
                assert all(row["fee"] == 0.0 and row["value"] is None and row["payload"] == {} for row in copied_trades)
                for source in (
                    source_execution_symbol,
                    source_position,
                    source_attempt,
                    source_transaction,
                    source_transaction_fee,
                ):
                    destination = next(
                        table
                        for table in COST_FACT_TABLES
                        if table.name == source.name.replace("analysis_source_", "performance_cost_")
                    )
                    original = (await session.execute(sa.select(source))).mappings().one()
                    copied = dict((await session.execute(sa.select(destination))).mappings().one())
                    assert copied.pop("snapshot_id") == "snapshot"
                    assert copied == dict(original)
                assert all("raw_input" not in sql and "raw_result" not in sql for sql in publication_statements)
                assert sum(sql.startswith("INSERT INTO performance_cost_") for sql in publication_statements) == 7
                assert all(" SELECT " in sql for sql in publication_statements if sql.startswith("INSERT"))

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"projection_version": 0},
        {"projection_version": 2},
        {"account_id": 3},
        {"execution_id": None},
        {"created_at": "2025-01-01T09:00:00"},
        {"is_success": 0},
        {"time": None},
        {"trade_count": None},
        {"trade_count": -1},
        {"trade_count": 1},
    ],
)
def test_copy_rejects_missing_unsupported_or_stale_sources_before_writing(change):
    async def check():
        async with _database() as (sessions, _):
            async with sessions() as session:
                header = await _record(session)
                if change is not None:
                    await session.execute(source_execution.insert(), header | change)
                await _snapshot(session)
                with pytest.raises(ValueError, match="source cost facts"):
                    await copy_source_cost_facts(session, "snapshot", 2)
                assert await session.scalar(sa.select(sa.func.count()).select_from(cost_execution)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(cost_trade)) == 0

    asyncio.run(check())


def test_copy_requires_matching_snapshot_and_leaves_commit_to_caller():
    async def check():
        async with _database() as (sessions, _):
            async with sessions() as session:
                await session.execute(source_execution.insert(), await _record(session))
                await session.commit()
                await _snapshot(session, "wrong", 3)
                with pytest.raises(ValueError, match="Snapshot"):
                    await copy_source_cost_facts(session, "wrong", 2)
                await session.rollback()
                await _snapshot(session)
                await copy_source_cost_facts(session, "snapshot", 2)
                assert await session.scalar(sa.select(sa.func.count()).select_from(cost_execution)) == 1
                await session.rollback()
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(analysis_snapshot)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(cost_execution)) == 0

    asyncio.run(check())
