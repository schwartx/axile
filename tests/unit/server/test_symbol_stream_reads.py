"""品种汇总流式读取保持成交顺序及原汇总精度。"""

import asyncio
import json
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord
from axile.server.performance_costs import summarize
from tests.unit.server.test_account_activity_projection import _FakeSession, _record, _Result
from tests.unit.server.test_performance_analysis import database
from tests.unit.server.test_performance_analysis import schema_template as schema_template


def test_stream_preserves_all_fills_and_summary_and_bounds_live_records(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=205) as (engine, sessions, _):
            async with sessions() as session:
                rows = (
                    (
                        await session.execute(
                            sa.select(ExecuteRecord).order_by(ExecuteRecord.created_at.desc(), ExecuteRecord.id.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
                expected = [trade for row in rows for trade in account_schedule._projected_trades(row)]
                await session.execute(
                    sa.update(ExecuteRecord).where(ExecuteRecord.id == 1).values(raw_input={"unused": "x" * 2_000_000})
                )
                await session.commit()
            decoded = []

            def deserialize(raw):
                decoded.append(len(raw))
                return json.loads(raw)

            selected = create_async_engine(engine.url, json_deserializer=deserialize)
            try:
                async with async_sessionmaker(selected)() as session:
                    live_records = []
                    projector = account_schedule._projected_trades

                    def project(row):
                        live_records.append(
                            sum(isinstance(obj, ExecuteRecord) for obj in session.identity_map.values())
                        )
                        return projector(row)

                    with patch.object(account_schedule, "_projected_trades", new=project):
                        expanded = await account_schedule.account_activity_symbols(
                            session, 2, "2020", "2030", symbol="A"
                        )
                    assert len(live_records) == 205
                    assert max(live_records) <= 101
                    assert expanded.count == 1
                    assert expanded.data[0].trades == expected
                    assert expanded.data[0].summary.model_dump() == summarize(expected)
                    overview = await account_schedule.account_activity_symbols(session, 2, "2020", "2030", symbol=None)
                    assert overview.data[0].summary == expanded.data[0].summary
                    assert overview.data[0].trades == []
                    assert overview.data[0].last_time == max(trade["time"] for trade in expected)
                    missing = await account_schedule.account_activity_symbols(
                        session, 2, "2020", "2030", symbol="missing"
                    )
                    assert missing.count == 0
                    empty = await account_schedule.account_activity_symbols(session, 2, "2090", "2091", symbol=None)
                    assert empty.count == 0
                assert decoded and max(decoded) < 4096
            finally:
                await selected.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("error", [RuntimeError("projection failed"), asyncio.CancelledError()])
def test_projection_failure_or_cancellation_closes_stream(error):
    result = _Result(rows=[_record()])
    session = _FakeSession([result])

    def fail(_row):
        raise error

    with patch.object(account_schedule, "_projected_trades", new=fail), pytest.raises(type(error)):
        asyncio.run(account_schedule.account_activity_symbols(session, 2, "2020", "2030", symbol=None))
    assert result.closed
