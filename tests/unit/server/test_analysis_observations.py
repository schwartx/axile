"""结构化回放事实的业务语义、事务和有界历史补齐。"""

import asyncio
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import analysis_observations
from axile.server.analysis_observations import (
    backfill_observation_facts,
    ensure_observation_facts,
    observation_fact_values,
    write_observation_fact,
)
from axile.server.db.models import ExecuteRecord, Portfolio, TargetWeightSnapshot
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.db.models.analysis_replay import replay_observation_symbol as symbols
from axile.server.performance import observation
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration


def record(index=1, *, raw_input=None, raw_result=None, created_at="2026-01-02T09:00:00"):
    return ExecuteRecord(
        id=index,
        account_id=1,
        execution_id=f"exec-{index}",
        is_success=1,
        created_at=created_at,
        raw_input={"curr_target": {"A": 1}} if raw_input is None else raw_input,
        raw_result={
            "account_assets": {"total_asset": 1000},
            "first_ticks": {"A": {"last_price": 100}},
        }
        if raw_result is None
        else raw_result,
    )


def restore(scalar, rows):
    maps = {}
    for field, column in (
        ("target", "target"),
        ("prices", "price"),
        ("target_weight", "target_weight"),
        ("target_units", "target_units"),
        ("target_quantities", "target_quantity"),
    ):
        maps[field] = (
            {row["symbol"]: row[column] for row in rows if row[column] is not None}
            if field == "prices" or scalar[f"{field}_present"]
            else None
        )
    return SimpleNamespace(**maps)


@pytest.mark.parametrize(
    "raw_input,raw_result,fallback",
    [
        ({}, {}, None),
        ({"curr_target": {}}, {}, None),
        ({}, {}, {}),
        ({}, {}, {"A": 0.5}),
        ({"curr_target": {"A": 0.5}}, {"execution_kind": "clear_positions"}, None),
        (
            {"curr_target": {"A": 1}},
            {
                "account_assets": {"total_asset": 1000},
                "symbol_results": {
                    "A": {
                        "first_tick": {"last_price": 10},
                        "sizing": {
                            "status": "SIZED",
                            "sizing_mode": "weight",
                            "equity": 1000,
                            "target_quantity": 2,
                            "unit_notional": 200,
                            "reference_price": 10,
                        },
                    },
                    "ZERO": {
                        "sizing": {"status": "SIZED", "equity": 1000, "target_quantity": 0},
                    },
                },
            },
            None,
        ),
        ({"curr_target": {"A": float("nan")}}, {"account_assets": {"total_asset": float("inf")}}, None),
    ],
)
def test_projection_preserves_optional_maps_and_numeric_semantics(raw_input, raw_result, fallback):
    item = record(raw_input=raw_input, raw_result=raw_result)
    expected = observation(item, fallback)
    scalar, rows = observation_fact_values(item, fallback)
    actual = restore(scalar, rows)
    for field in ("target", "prices", "target_weight", "target_units", "target_quantities"):
        assert getattr(actual, field) == getattr(expected, field)
    assert scalar["asset"] == expected.asset
    assert scalar["sizing_fallback"] == expected.sizing_fallback
    assert all(not isinstance(value, (dict, list)) for row in [scalar, *rows] for value in row.values())


def test_same_local_instant_is_canonical_but_records_remain_distinct():
    left, _ = observation_fact_values(record(1, created_at="2026-01-02T01:00:00Z"))
    right, _ = observation_fact_values(record(2, created_at="2026-01-02T09:00:00+08:00"))
    assert left["time"] == right["time"] == "2026-01-02T09:00:00.000000"
    assert left["record_id"] < right["record_id"]
    with pytest.raises(ValueError, match="flush"):
        observation_fact_values(record(None))


async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")

    def create_schema(connection):
        SQLModel.metadata.create_all(connection)
        migration = _load_migration(_MIGRATIONS_DIR / "0034_analysis_rust_facts.py")
        migration.install_triggers(connection)

    async with engine.begin() as connection:
        await connection.run_sync(create_schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(build_account(id=1))
        await session.commit()
    return engine, sessions


def test_writer_replaces_symbol_rows_atomically_and_rolls_back(tmp_path):
    async def check():
        engine, sessions = await database(tmp_path)
        try:
            async with sessions() as session:
                item = record()
                session.add(item)
                await session.flush()
                await write_observation_fact(session, item)
                await session.commit()
                assert (await session.execute(sa.select(symbols.c.symbol))).scalars().all() == ["A"]
                item.raw_input = {"curr_target": {}}
                item.raw_result = {"execution_kind": "clear_positions"}
                await write_observation_fact(session, item)
                assert await session.scalar(sa.select(sa.func.count()).select_from(symbols)) == 0
                await session.rollback()
            async with sessions() as session:
                assert (await session.execute(sa.select(symbols.c.target))).scalar_one() == 1
                item = await session.get(ExecuteRecord, 1)
                item.raw_input = {"curr_target": {}}
                item.raw_result = {"execution_kind": "clear_positions"}
                await write_observation_fact(session, item)
                await session.commit()
                scalar = (await session.execute(sa.select(observations))).mappings().one()
                assert scalar["target_present"] and scalar["target_units_present"]
                assert await session.scalar(sa.select(sa.func.count()).select_from(symbols)) == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_backfill_one_batch_then_resume_with_late_fallback_and_upper_bound(tmp_path):
    async def check():
        engine, sessions = await database(tmp_path)
        try:
            async with sessions() as session:
                session.add(
                    Portfolio(
                        id=1,
                        name="facts",
                        market="stock",
                        custom_calc_py_code="def calculate_portfolio(context): return {}",
                    )
                )
                session.add_all([record(i, raw_input={}) for i in range(1, 6)])
                session.add(
                    TargetWeightSnapshot(
                        portfolio_id=1, account_id=1, execution_id="exec-1", normalized_weights={}, source="execution"
                    )
                )
                await session.commit()
                first = await backfill_observation_facts(session, 1, batch_size=2, through_record_id=4)
                assert (first.processed, first.complete) == (2, False)
                await session.commit()
            async with sessions() as session:
                second = await backfill_observation_facts(session, 1, batch_size=2, through_record_id=4)
                assert (second.processed, second.complete) == (2, True)
                await session.commit()
                assert await session.scalar(sa.select(sa.func.count()).select_from(observations)) == 4
                fallback = (
                    (await session.execute(sa.select(observations).where(observations.c.record_id == 1)))
                    .mappings()
                    .one()
                )
                assert fallback["target_present"]
                assert not fallback["sizing_fallback"]
            async with sessions() as session:
                assert await ensure_observation_facts(session, 1, batch_size=1) == 1
                assert await ensure_observation_facts(session, 1, batch_size=1) == 0
                with pytest.raises(ValueError, match="batch_size"):
                    await backfill_observation_facts(session, 1, batch_size=0)
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_cancelled_backfill_preserves_committed_batches_and_resumes(tmp_path, monkeypatch):
    async def check():
        engine, sessions = await database(tmp_path)
        original = analysis_observations.backfill_observation_facts
        calls = 0

        async def cancel_second_batch(*args, **kwargs):
            nonlocal calls
            progress = await original(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError
            return progress

        try:
            async with sessions() as session:
                session.add_all([record(i) for i in range(1, 6)])
                await session.commit()
            monkeypatch.setattr(analysis_observations, "backfill_observation_facts", cancel_second_batch)
            with pytest.raises(asyncio.CancelledError):
                async with sessions() as session:
                    await ensure_observation_facts(session, 1, batch_size=2)
            monkeypatch.setattr(analysis_observations, "backfill_observation_facts", original)
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(observations)) == 2
            async with sessions() as session:
                assert await ensure_observation_facts(session, 1, batch_size=2) == 3
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_parent_and_fallback_mutations_invalidate_then_rebuild_facts(tmp_path):
    async def check():
        engine, sessions = await database(tmp_path)
        try:
            async with sessions() as session:
                item = record(raw_input={})
                session.add(item)
                session.add(
                    Portfolio(
                        id=1,
                        name="facts",
                        market="stock",
                        custom_calc_py_code="def calculate_portfolio(context): return {}",
                    )
                )
                target = TargetWeightSnapshot(
                    portfolio_id=1,
                    account_id=1,
                    execution_id="exec-1",
                    normalized_weights={"A": 1},
                    source="execution",
                )
                session.add(target)
                await session.commit()
                assert await ensure_observation_facts(session, 1) == 1
                target.normalized_weights = {"B": 0.25}
                await session.commit()
                assert await session.scalar(sa.select(sa.func.count()).select_from(observations)) == 0
                await session.commit()
                assert await ensure_observation_facts(session, 1) == 1
                values = (await session.execute(sa.select(symbols.c.symbol, symbols.c.target))).all()
                assert values == [("A", None), ("B", 0.25)]
                await session.delete(target)
                await session.commit()
                assert await session.scalar(sa.select(sa.func.count()).select_from(observations)) == 0
                await session.commit()
                assert await ensure_observation_facts(session, 1) == 1
                scalar = (await session.execute(sa.select(observations))).mappings().one()
                assert not scalar["target_present"]
                item.raw_input = {"curr_target": {}}
                item.created_at = "2026-01-03T01:00:00Z"
                await session.commit()
                assert await session.scalar(sa.select(sa.func.count()).select_from(observations)) == 0
                await session.commit()
                assert await ensure_observation_facts(session, 1) == 1
                scalar = (await session.execute(sa.select(observations))).mappings().one()
                assert scalar["target_present"] and scalar["time"] == "2026-01-03T09:00:00.000000"
        finally:
            await engine.dispose()

    asyncio.run(check())
