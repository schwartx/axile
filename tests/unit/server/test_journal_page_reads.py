"""日记分页保持混排、筛选和摘要口径，且只解析当前页的结果。"""

import asyncio
import json
from datetime import date
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.api.routes import account_schedule
from axile.server.business_facts.assets import read_result_asset_totals
from axile.server.db.models import ExecuteRecord, ScheduleSkip
from tests.unit.server._business_analysis_support import persist_records
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_performance_analysis import database, record
from tests.unit.server.test_performance_analysis import schema_template as schema_template


def test_mixed_pages_keep_ties_counts_filters_and_existing_projection(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=6) as (_, sessions, _):
            async with sessions() as session:
                session.add(build_account(id=3, account_control_preset="default"))
                for index in (2, 4, 6):
                    session.add(
                        ScheduleSkip(
                            id=index,
                            account_id=2,
                            channel="ctp",
                            triggered_at=record(index).created_at,
                            calendar_id="china",
                            calendar_day=date(2026, 1, index),
                            calendar_label="休市",
                        )
                    )
                other = record(7)
                other.account_id = 3
                await persist_records(session, [other], project=True)
                await session.commit()
            async with sessions() as session:
                rows = (
                    (await session.execute(sa.select(ExecuteRecord).where(ExecuteRecord.account_id == 2)))
                    .scalars()
                    .all()
                )
                summaries = await account_schedule.read_execution_summaries(session, rows)
                trades = await account_schedule.read_projected_trades(session, [row.id for row in rows])
                totals = await read_result_asset_totals(session, [row.id for row in rows])
                expected = {
                    row.id: account_schedule._compact_execution_activity(
                        row, summaries[row.id], trades[row.id], totals[row.id]
                    ).model_dump()
                    for row in rows
                }
            order = [
                ("execution", 6),
                ("schedule_skip", 6),
                ("execution", 5),
                ("execution", 4),
                ("schedule_skip", 4),
                ("execution", 3),
                ("execution", 2),
                ("schedule_skip", 2),
                ("execution", 1),
            ]
            for window, filters in (
                (order, {}),
                (order[3:8], dict(since=record(2).created_at, until=record(5).created_at)),
                ([], dict(since="2099")),
            ):
                for offset in range(0, len(window) + 3, 2):
                    async with sessions() as session:
                        page = await account_schedule.account_activity(session, 2, skip=offset, limit=2, **filters)
                        assert page.count == len(window)
                        actual = [
                            (item.kind, item.record.id if item.kind == "execution" else item.id) for item in page.data
                        ]
                        assert actual == window[offset : offset + 2]
                        for item in page.data:
                            if item.kind == "execution":
                                assert item.model_dump() == expected[item.record.id]
            async with sessions() as session:
                # The route must not commit or roll back its caller's pending work.
                await session.execute(sa.update(ExecuteRecord).where(ExecuteRecord.id == 1).values(is_success=0))
                await account_schedule.account_activity(session, 2, skip=0, limit=2)
                await session.rollback()
                assert await session.scalar(sa.select(ExecuteRecord.is_success).where(ExecuteRecord.id == 1)) == 1

    asyncio.run(check())


def test_deep_page_never_decodes_raw_input_or_projects_discarded_results(tmp_path, schema_template):
    async def check():
        async with database(tmp_path, schema_template, count=6) as (engine, sessions, _):
            async with sessions() as session:
                rows = [record(index) for index in range(1, 7)]
                for row in rows:
                    row.input["algorithm"] = {"params": {"unused": "x" * 2_000_000}}
                await persist_records(session, rows, project=True)
                await session.commit()
            decoded = []

            def deserialize(raw):
                decoded.append(len(raw))
                return json.loads(raw)

            selected = create_async_engine(engine.url, json_deserializer=deserialize)
            try:
                projector = account_schedule._compact_execution_activity
                with patch.object(account_schedule, "_compact_execution_activity", wraps=projector) as compact:
                    async with async_sessionmaker(selected)() as session:
                        page = await account_schedule.account_activity(session, 2, skip=4, limit=2)
                    assert [item.record.id for item in page.data] == [2, 1]
                    assert sorted(call.args[0].id for call in compact.call_args_list) == [1, 2]
                assert decoded and max(decoded) < 4096
            finally:
                await selected.dispose()

    asyncio.run(check())


def test_page_uses_one_snapshot_when_a_writer_deletes_selected_history(tmp_path, schema_template):
    """页头和详情之间的并发删除不能导致缺行或 KeyError。"""
    import sqlite3

    async def check():
        async with database(tmp_path, schema_template, count=3) as (engine, sessions, _):
            async with sessions() as session:
                await session.execute(sa.text("PRAGMA journal_mode=WAL"))
                await session.commit()
            deleted = False

            def delete_after_count(_conn, _cursor, statement, _parameters, _context, _many):
                nonlocal deleted
                if deleted or not statement.startswith("SELECT count("):
                    return
                with sqlite3.connect(tmp_path / "analysis.db", timeout=1) as writer:
                    writer.execute("DELETE FROM executerecord WHERE id=3")
                deleted = True

            sa.event.listen(engine.sync_engine, "after_cursor_execute", delete_after_count)
            try:
                async with sessions() as session:
                    page = await account_schedule.account_activity(session, 2, skip=0, limit=2)
                    assert page.count == 3
                    assert [item.record.id for item in page.data] == [3, 2]
                assert deleted
                async with sessions() as session:
                    assert await session.get(ExecuteRecord, 3) is None
            finally:
                sa.event.remove(engine.sync_engine, "after_cursor_execute", delete_after_count)

    asyncio.run(check())
