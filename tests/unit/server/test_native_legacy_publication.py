"""Unsupported legacy values remain readable without publishing partial typed facts."""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.analysis_cost_facts import COST_FACT_TABLES
from axile.server.api.routes import account_performance as routes
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance_analysis import AnalysisManager, compute_batch, enqueue, read_snapshot
from axile.server.performance_details import CostQuery
from tests.unit.server._business_analysis_support import load_record, persist_records
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_performance_analysis import _create_schema, record


@pytest.mark.parametrize("malformed", ["key", "raw_error"])
def test_unsupported_projection_publishes_legacy_costs_and_rolls_back_typed_children(tmp_path, monkeypatch, malformed):
    """A late conversion failure rolls back already inserted batches and preserves the original value."""
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def reject_native(*_args, **_kwargs):
        raise AssertionError("Schema-zero costs must choose the legacy reader before native dispatch")

    monkeypatch.setattr(routes, "_native_response", reject_native)

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-publication.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(_create_schema)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                await persist_records(session, [record()])
                await session.commit()
                canonical = await load_record(session, 1)
                await enqueue(session, 2)
            settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.0)
            ranges, projected, costs = compute_batch([canonical], [], [], [], settings)
            template = projected[0]
            # Publication flushes 500 execution headers and their child rows
            # before reaching the malformed final execution.
            rows = [
                {
                    **template,
                    "record_id": identity,
                    "payload": {
                        **template["payload"],
                        "key": str(identity),
                        "record": {**template["payload"]["record"], "id": identity},
                    },
                }
                for identity in range(1, 502)
            ]
            bad_payload = rows[-1]["payload"]
            if malformed == "key":
                bad_payload["key"] = 123
            else:
                bad_payload["record"]["raw_result"] = {
                    **bad_payload["record"]["raw_result"],
                    "error": {"legacy": "structured error"},
                }
            manager = AnalysisManager(sessions)
            claimed = await manager._claim()
            assert claimed is not None
            await manager._publish(claimed, settings, (ranges, rows, costs))

            async with sessions() as session:
                state = (await session.execute(sa.select(states))).mappings().one()
                identity = state["current_snapshot"]
                assert state["published_version"] == state["source_version"]
                assert state["running_version"] is None
                assert state["requested"] is False
                assert state["error"] is None
                assert await session.scalar(sa.select(snapshots.c.cost_schema_version)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(executions)) == 501
                persisted = await session.scalar(sa.select(executions.c.payload).where(executions.c.record_id == 501))
                assert persisted == bad_payload
                for table in COST_FACT_TABLES:
                    if table.name in {"performance_cost_execution", "performance_cost_trade"}:
                        continue
                    assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0, table.name
                assert (await read_snapshot(session, 2, "all"))["status"] == "ready"

            async with sessions() as session:
                page = await routes.get_costs(session, 2, CostQuery(snapshot_id=identity))
                assert isinstance(page, dict)
                assert page["count"] == 501
                first = page["data"][0]
                if malformed == "key":
                    assert first["key"] == 123
                else:
                    assert first["record"]["raw_result"]["error"] == {"legacy": "structured error"}
        finally:
            await engine.dispose()

    asyncio.run(check())
