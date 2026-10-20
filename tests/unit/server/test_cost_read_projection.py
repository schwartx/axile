"""Cost pages retain scalar identity and avoid decoding unused snapshot packages."""

import asyncio
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.performance_details import CostQuery, read_costs
from tests.unit.server.test_performance_analysis import database, queue, snapshot
from tests.unit.server.test_performance_analysis import schema_template as schema_template


@pytest.mark.parametrize("token", ["null", "false", "0", "-0.0", '"中文-id"', "5.346525073723255485091955e-135"])
def test_trade_execution_identifier_preserves_original_json_type_and_precision(tmp_path, schema_template, token):
    async def check():
        async with database(tmp_path, schema_template, count=1) as (_, sessions, manager):
            await queue(sessions)
            assert await manager.run_once()
            batch = await snapshot(sessions)
            raw = '{"record":{"id":1,"execution_id":' + token + "}}"
            async with sessions() as session:
                await session.execute(sa.text("UPDATE performance_cost_execution SET payload=:raw"), {"raw": raw})
                await session.commit()
                page = await read_costs(session, 2, CostQuery(snapshot_id=batch["snapshot_id"], dimension="trade"))
            expected = json.loads(token)
            actual = page["data"][0]["execution_id"]
            assert type(actual) is type(expected)
            assert actual == expected
            if isinstance(expected, float):
                assert actual.hex() == expected.hex()

    asyncio.run(check())


def test_cost_pages_never_decode_unrelated_large_json_fields(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=2) as (engine, sessions, manager):
            await queue(sessions)
            assert await manager.run_once()
            batch = await snapshot(sessions)
            query = CostQuery(snapshot_id=batch["snapshot_id"], dimension="trade")
            async with sessions() as session:
                expected = await read_costs(session, 2, query)
                ranges = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                ranges["unrelated"] = "x" * 2_000_000
                await session.execute(snapshots.update().values(ranges=ranges))
                rows = (await session.execute(sa.select(executions))).mappings().all()
                for row in rows:
                    payload = row["payload"] | {"unrelated": "x" * 1_000_000}
                    await session.execute(
                        executions.update().where(executions.c.record_id == row["record_id"]).values(payload=payload)
                    )
                await session.commit()
            decoded = []

            def deserialize(raw):
                decoded.append(len(raw))
                return json.loads(raw)

            selected = create_async_engine(engine.url, json_deserializer=deserialize)
            try:
                async with async_sessionmaker(selected)() as session:
                    actual = await read_costs(session, 2, query)
                assert actual == expected
                assert decoded and max(decoded) < 4096
            finally:
                await selected.dispose()

    asyncio.run(check())


def test_missing_keys_remain_distinct_from_explicit_null(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=1) as (_, sessions, manager):
            await queue(sessions)
            assert await manager.run_once()
            batch = await snapshot(sessions)
            query = CostQuery(snapshot_id=batch["snapshot_id"], dimension="trade")
            async with sessions() as session:
                ranges = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                ranges["all"]["performance"].pop("baseline")
                await session.execute(snapshots.update().values(ranges=ranges))
                await session.commit()
                with pytest.raises(KeyError, match="baseline"):
                    await read_costs(session, 2, query)
                ranges["all"]["performance"]["baseline"] = None
                await session.execute(snapshots.update().values(ranges=ranges))
                await session.execute(executions.update().values(payload={"record": {"id": 1, "execution_id": None}}))
                await session.commit()
                assert (await read_costs(session, 2, query))["data"][0]["execution_id"] is None
                await session.execute(executions.update().values(payload={"record": {"id": 1}}))
                await session.commit()
                with pytest.raises(KeyError, match="execution_id"):
                    await read_costs(session, 2, query)

    asyncio.run(check())
