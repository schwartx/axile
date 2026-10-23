"""日记分页保持混排、筛选和摘要口径，且只解析当前页的结果。"""

import asyncio
import json
from contextlib import closing
from datetime import date
from unittest.mock import Mock

import sqlalchemy as sa

from axile.server._native import _core
from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord, ScheduleSkip
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import connect as oracle_connect
from tests.sqlite_oracle import sqlite_oracle
from tests.unit.server._business_analysis_support import database, persist_records, record
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_read_oracle import ExecuteRecordSummaryPublic
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._journal_oracle import _compact_execution_activity
from tests.unit.server._performance_costs_oracle import project_execution


@sqlite_oracle
def _heads(path):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            records = table("executerecord")
            return [
                dict(head)
                for head in connection.execute(sa.select(records).where(records.c.account_id == 2)).mappings()
            ]
    finally:
        engine.dispose()


def test_mixed_pages_keep_ties_counts_filters_and_existing_projection(tmp_path, schema_template, monkeypatch):
    path = tmp_path / "analysis.db"
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(path)))

    async def check():
        async with database(tmp_path, schema_template, count=6) as (_, sessions, _):
            with closing(oracle_connect(path)) as connection, connection:
                insert_account(connection, build_account(id=3, account_control_preset="default"))
                for index in (2, 4, 6):
                    skip = ScheduleSkip(
                        id=index,
                        account_id=2,
                        channel="ctp",
                        triggered_at=record(index).created_at,
                        calendar_id="china",
                        calendar_day=date(2026, 1, index),
                        calendar_label="休市",
                    ).model_dump(mode="json")
                    connection.execute(
                        f"INSERT INTO schedule_skip ({','.join(skip)}) VALUES ({','.join('?' for _ in skip)})",
                        list(skip.values()),
                    )
            async with sessions() as session:
                other = record(7)
                other.account_id = 3
                await persist_records(session, [other])
            heads = _heads(path)
            expected = {}
            for head in heads:
                source = record(head["id"])
                projected, trades = project_execution(source)
                summary = ExecuteRecordSummaryPublic.model_validate(
                    {**head, "symbol_results": {"A": {"outcome": None}}, "trade_count": projected["tradeCount"]}
                )
                expected[source.id] = _compact_execution_activity(
                    ExecuteRecord.model_validate(head), summary, trades, source.result["account_assets"]["total_asset"]
                ).model_dump()
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
            before = path.read_bytes()
            for window, filters in (
                (order, {}),
                (order[3:8], dict(since=record(2).created_at, until=record(5).created_at)),
                ([], dict(since="2099")),
            ):
                for offset in range(0, len(window) + 3, 2):
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
                            reference = expected[item["record"]["id"]]
                            assert item == reference, {
                                key: (value, reference["record"][key])
                                for key, value in item["record"].items()
                                if value != reference["record"][key]
                            }
            route = next(route for route in account_schedule.router.routes if route.path == "/{account_id}/activity")
            assert route.dependant.dependencies == []
            assert path.read_bytes() == before
            with closing(oracle_connect(path)) as connection:
                assert connection.execute("SELECT is_success FROM executerecord WHERE id=1").fetchone()[0] == 1

    asyncio.run(check())


def test_deep_page_never_decodes_raw_input_or_projects_discarded_results(tmp_path, schema_template, monkeypatch):
    async def check():
        async with database(tmp_path, schema_template, count=6) as (_, sessions, _):
            async with sessions() as session:
                rows = [record(index) for index in range(1, 7)]
                for row in rows:
                    row.input["algorithm"] = {"params": {"unused": "x" * 2_000_000}}
                await persist_records(session, rows)
            path = tmp_path / "analysis.db"
            with closing(oracle_connect(path)) as connection, connection:
                connection.execute("UPDATE execution_input SET audit_evidence='not-json'")
            monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(path)))
            query = Mock(wraps=_core.journal)
            monkeypatch.setattr(_core, "journal", query)
            page = json.loads((await account_schedule.account_activity(2, skip=4, limit=2)).body)
            assert [item["record"]["id"] for item in page["data"]] == [2, 1]
            assert all(
                {"input", "result", "raw_input", "raw_result"}.isdisjoint(item["record"]) for item in page["data"]
            )
            query.assert_called_once()
            assert query.call_args.args[1:] == (2, 4, 2, None, None)

    asyncio.run(check())
