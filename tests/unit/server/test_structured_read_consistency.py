"""Normalized readers hold one WAL snapshot while publication evicts old results."""

import asyncio
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_results import RESULT_SCHEMA_VERSION, iter_result_fact_batches
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance_analysis import (
    ENGINE_VERSION,
    LOGIC_VERSION,
    compute_batch,
    read_performance_summaries,
    read_snapshot,
)
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_performance_analysis import record


@pytest.mark.parametrize("reader", ["snapshot", "summary"])
def test_typed_reader_keeps_evicted_snapshot_after_metadata_read(tmp_path, reader):
    """A second connection can evict the selected version without changing this response."""
    path = tmp_path / "read-consistency.db"
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.0)
    ranges, _, _ = compute_batch([record()], [], [], [], settings)
    expected_points = ranges["all"]["performance"]["points"]

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text("PRAGMA journal_mode=WAL"))
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                for identity in ("selected", "replacement"):
                    await session.execute(
                        snapshots.insert(),
                        {
                            "id": identity,
                            "account_id": 2,
                            "source_version": 1,
                            "logic_version": LOGIC_VERSION,
                            "engine_version": ENGINE_VERSION,
                            "computed_at": "2026-01-01T10:00:00",
                            "settings": {},
                            "ranges": {},
                            "result_schema_version": RESULT_SCHEMA_VERSION,
                            "backtest_weight_type": "cs",
                            "backtest_fee_rate": 0.0,
                        },
                    )
                await session.execute(
                    states.insert(),
                    {"account_id": 2, "source_version": 1, "published_version": 1, "current_snapshot": "selected"},
                )
                for table, values in iter_result_fact_batches("selected", ranges):
                    await session.execute(table.insert(), values)
                # New readers must use scalar settings and result tables even
                # when retained compatibility columns cannot be decoded.
                await session.execute(
                    sa.text("UPDATE performance_snapshot SET ranges='invalid-json', settings='invalid-json'")
                )
                await session.commit()

            evictions = []

            def evict_after_metadata(_connection, _cursor, statement, _parameters, _context, _many):
                if evictions or "FROM account_analysis" not in statement or "result_schema_version" not in statement:
                    return
                # This callback runs after the first SELECT starts its read view,
                # before any normalized child query. WAL permits this independent
                # writer to commit while the selected snapshot is still being read.
                with sqlite3.connect(path, timeout=1) as writer:
                    writer.execute("UPDATE account_analysis SET current_snapshot='replacement'")
                    for table in RESULT_TABLES:
                        writer.execute(f'DELETE FROM "{table.name}" WHERE snapshot_id=?', ("selected",))
                    writer.execute("DELETE FROM performance_snapshot WHERE id='selected'")
                evictions.append(True)

            sa.event.listen(engine.sync_engine, "after_cursor_execute", evict_after_metadata)
            try:
                async with sessions() as session:
                    if reader == "snapshot":
                        response = await read_snapshot(session, 2, "all")
                        assert response["snapshot_id"] == "selected"
                        assert response["status"] == "ready"
                        actual_points = response["result"]["points"]
                    else:
                        response = (await read_performance_summaries(session, [2]))[2]
                        assert response.snapshot_id == "selected"
                        assert response.status == "ready"
                        actual_points = [point.model_dump() for point in response.points]
                    assert actual_points == expected_points
            finally:
                sa.event.remove(engine.sync_engine, "after_cursor_execute", evict_after_metadata)
            assert evictions == [True]
            with sqlite3.connect(path) as observer:
                assert (
                    observer.execute("SELECT COUNT(*) FROM performance_snapshot WHERE id='selected'").fetchone()[0] == 0
                )
                assert observer.execute("SELECT current_snapshot FROM account_analysis").fetchone()[0] == "replacement"
        finally:
            await engine.dispose()

    asyncio.run(check())
