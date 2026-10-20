"""品种汇总流式读取保持成交顺序及原汇总精度。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord
from axile.server.performance_costs import project_execution, summarize
from tests.unit.server._business_analysis_support import persist_records
from tests.unit.server.test_performance_analysis import database, record
from tests.unit.server.test_performance_analysis import schema_template as schema_template


def test_stream_preserves_all_fills_and_summary_and_bounds_live_records(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=205) as (engine, sessions, _):
            async with sessions() as session:
                rows = [record(index) for index in range(205, 0, -1)]
                expected = [
                    {**trade, "record_id": row.id, "execution_id": row.execution_id}
                    for row in rows
                    for trade in project_execution(row)[1]
                ]
                bulky = record(1)
                bulky.input["algorithm"] = {"params": {"unused": "x" * 2_000_000}}
                await persist_records(session, [bulky], project=True)
                await session.commit()
            decoded = []

            def deserialize(raw):
                decoded.append(len(raw))
                return json.loads(raw)

            selected = create_async_engine(engine.url, json_deserializer=deserialize)
            try:
                async with async_sessionmaker(selected)() as session:
                    live_records = []
                    projector = account_schedule.projected_trade
                    stream = session.stream
                    streamed_statements = []

                    async def bounded_stream(statement):
                        streamed_statements.append(statement)
                        assert statement.get_execution_options()["yield_per"] == 100
                        return await stream(statement)

                    def project(row):
                        live_records.append(
                            sum(isinstance(obj, ExecuteRecord) for obj in session.identity_map.values())
                        )
                        return projector(row)

                    with (
                        patch.object(account_schedule, "projected_trade", new=project),
                        patch.object(session, "stream", new=bounded_stream),
                    ):
                        expanded = await account_schedule.account_activity_symbols(
                            session, 2, "2020", "2030", symbol="A"
                        )
                    assert len(live_records) == 205
                    assert max(live_records) == 0, "成交标量流不应创建执行记录 ORM 实例"
                    assert len(streamed_statements) == 1
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
    class Result:
        closed = False

        def mappings(self):
            return self

        async def __aiter__(self):
            yield {}

        async def close(self):
            self.closed = True

    result = Result()

    class Session:
        async def stream(self, statement):
            assert statement.get_execution_options()["yield_per"] == 100
            return result

    session = Session()

    async def account_exists(_session, account_id):
        return SimpleNamespace(id=account_id)

    def fail(_row):
        raise error

    with (
        patch.object(account_schedule, "_get_account_or_404", new=account_exists),
        patch.object(account_schedule, "projected_trade", new=fail),
        pytest.raises(type(error)),
    ):
        asyncio.run(account_schedule.account_activity_symbols(session, 2, "2020", "2030", symbol=None))
    assert result.closed
