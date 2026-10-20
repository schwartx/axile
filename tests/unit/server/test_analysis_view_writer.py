"""Compatibility publication freezes small views without taking transaction ownership."""

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from axile.server.analysis_view_writer import prepare_read_views
from tests.unit.server.test_analysis_read_views_migration import _baseline_schema, _facts, _migration


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(_baseline_schema)
        await connection.run_sync(lambda current: _migration(current).upgrade())
    return engine


def test_compatibility_freeze_is_version_bound_idempotent_and_explicit_for_legacy():
    """Only the supplied typed snapshot gets a complete marker and its own currencies."""

    async def run():
        engine = await _engine()
        try:
            async with AsyncSession(engine) as session, session.begin():
                await session.run_sync(lambda current: _facts(current.connection(), "typed"))
                await session.run_sync(lambda current: _facts(current.connection(), "other"))
                await session.run_sync(lambda current: _facts(current.connection(), "legacy", result_version=0))
                assert await prepare_read_views(session, "typed")
                assert await prepare_read_views(session, "typed")
                assert not await prepare_read_views(session, "legacy")
                assert not await prepare_read_views(session, "missing")
                assert session.in_transaction()
                assert dict(
                    (
                        await session.execute(sa.text("SELECT id,read_view_schema_version FROM performance_snapshot"))
                    ).all()
                ) == {"typed": 1, "other": 0, "legacy": 0}
                assert (
                    await session.execute(sa.text("SELECT COUNT(*) FROM performance_execution_summary"))
                ).scalar_one() == 4
                assert (
                    await session.execute(sa.text("SELECT value FROM performance_execution_fee WHERE currency='BTC'"))
                ).scalar_one() == 0.25
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_compatibility_freeze_rolls_back_with_its_publication():
    """Neither summary rows nor the complete marker may survive an aborted publication."""

    async def run():
        engine = await _engine()
        try:
            async with AsyncSession(engine) as session:
                await session.run_sync(lambda current: _facts(current.connection()))
                await session.commit()
                assert await prepare_read_views(session, "typed")
                await session.rollback()
                assert (
                    await session.execute(sa.text("SELECT read_view_schema_version FROM performance_snapshot"))
                ).scalar_one() == 0
                for name in ("performance_execution_summary", "performance_execution_fee"):
                    assert (await session.execute(sa.text(f"SELECT COUNT(*) FROM {name}"))).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
