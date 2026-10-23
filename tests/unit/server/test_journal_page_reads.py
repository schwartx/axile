"""日记分页保持混排、筛选和摘要口径，且只解析当前页的结果。"""

import asyncio
import json
from datetime import date

import sqlalchemy as sa

from axile.server._native import _core
from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord, ScheduleSkip
from tests.unit.server._business_analysis_support import database, persist_records, record
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_read_oracle import read_execution_summaries, read_projected_trades
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._journal_oracle import _compact_execution_activity, read_result_asset_totals


def test_mixed_pages_keep_ties_counts_filters_and_existing_projection(tmp_path, schema_template, monkeypatch):
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(tmp_path / "analysis.db")))

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
                await persist_records(session, [other])
                await session.commit()
            async with sessions() as session:
                rows = (
                    (await session.execute(sa.select(ExecuteRecord).where(ExecuteRecord.account_id == 2)))
                    .scalars()
                    .all()
                )
                summaries = await read_execution_summaries(session, rows)
                trades = await read_projected_trades(session, [row.id for row in rows])
                totals = await read_result_asset_totals(session, [row.id for row in rows])
                expected = {
                    row.id: _compact_execution_activity(
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
                        page = json.loads(
                            (await account_schedule.account_activity(2, skip=offset, limit=2, **filters)).body
                        )
                        assert page["count"] == len(window)
                        actual = [
                            (item["kind"], item["record"]["id"] if item["kind"] == "execution" else item["id"])
                            for item in page["data"]
                        ]
                        assert actual == window[offset : offset + 2]
                        for item in page["data"]:
                            if item["kind"] == "execution":
                                assert item == expected[item["record"]["id"]]
            async with sessions() as session:
                # The route must not commit or roll back its caller's pending work.
                await session.execute(sa.update(ExecuteRecord).where(ExecuteRecord.id == 1).values(is_success=0))
                json.loads((await account_schedule.account_activity(2, skip=0, limit=2)).body)
                await session.rollback()
                assert await session.scalar(sa.select(ExecuteRecord.is_success).where(ExecuteRecord.id == 1)) == 1

    asyncio.run(check())


def test_deep_page_never_decodes_raw_input_or_projects_discarded_results(tmp_path, schema_template, monkeypatch):
    async def check():
        async with database(tmp_path, schema_template, count=6) as (_, sessions, _):
            async with sessions() as session:
                rows = [record(index) for index in range(1, 7)]
                for row in rows:
                    row.input["algorithm"] = {"params": {"unused": "x" * 2_000_000}}
                await persist_records(session, rows)
                await session.commit()
                await session.execute(sa.text("UPDATE execution_input SET audit_evidence='not-json'"))
                await session.commit()
            monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(tmp_path / "analysis.db")))
            page = json.loads((await account_schedule.account_activity(2, skip=4, limit=2)).body)
            assert [item["record"]["id"] for item in page["data"]] == [2, 1]

    asyncio.run(check())
