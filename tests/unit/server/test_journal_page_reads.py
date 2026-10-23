"""日记分页保持混排、筛选和摘要口径，且只解析当前页的结果。"""

import asyncio
import json
import pickle
import subprocess
import sys
from contextlib import closing
from datetime import date
from unittest.mock import Mock
from uuid import uuid4

from axile.server._native import _core
from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord, ScheduleSkip
from tests.conftest import native_store_call
from tests.unit.server._business_analysis_support import _release_oracle_connections, database, record
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_read_oracle import ExecuteRecordSummaryPublic
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._journal_oracle import _compact_execution_activity
from tests.unit.server._performance_costs_oracle import project_execution

_SQLITE_ORACLE = """
import pickle, sqlite3, sys
from contextlib import closing
from tests.unit.server._business_analysis_support import _record_tables, _reset_records, _move_record_id
from tests.unit.server._business_facts_test_support import rows
path, operation, arguments = pickle.loads(sys.stdin.buffer.read())
if operation == 'rows':
    result = rows(path, *arguments)
else:
    with closing(sqlite3.connect(path, timeout=2)) as connection, connection:
        if operation == 'prepare':
            connection.execute('PRAGMA foreign_keys=ON')
            result = _record_tables(connection)
            _reset_records(connection, *arguments)
        elif operation == 'move':
            connection.execute('PRAGMA foreign_keys=ON')
            _move_record_id(connection, *arguments)
            result = None
        else:
            result = [connection.execute(statement, parameters).fetchall() for statement, parameters in arguments[0]]
sys.stdout.buffer.write(pickle.dumps(result))
"""


def _oracle(path, operation, *arguments):
    # SQL 批次在子进程完成，避免 stdlib SQLite 关闭同进程原生连接的 WAL 锁。
    result = subprocess.run(
        [sys.executable, "-c", _SQLITE_ORACLE],
        input=pickle.dumps((str(path), operation, arguments)),
        capture_output=True,
        timeout=5,
        check=True,
    )
    return pickle.loads(result.stdout)


class _OracleConnection:
    def __init__(self, path):
        self.path = path
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._flush()
        self.close()

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        return self

    def _flush(self):
        statements, self.statements = self.statements, []
        return _oracle(self.path, "sql", statements)

    def fetchone(self):
        return self._flush()[-1][0]

    def close(self):
        self.statements.clear()


async def persist_records(session, records):
    records = list(records)
    if not records:
        return
    path = await _release_oracle_connections(session)
    tables = _oracle(path, "prepare", [source.id for source in records if source.id is not None])
    for source in records:
        saved = native_store_call(
            path,
            "persist_execute_record",
            {
                "account_id": source.account_id,
                "execution_id": source.execution_id,
                "created_at": source.created_at,
                "is_success": source.is_success,
                "input_id": str(uuid4()),
                "result_input_id": str(uuid4()),
                "raw_input": source.input,
                "raw_result": source.result,
            },
        )
        if source.id is not None and source.id != saved["id"]:
            _oracle(path, "move", saved["id"], source.id, source.execution_id, tables)
        else:
            source.id = saved["id"]


def test_mixed_pages_keep_ties_counts_filters_and_existing_projection(tmp_path, schema_template, monkeypatch):
    path = tmp_path / "analysis.db"
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(path)))

    async def check():
        async with database(tmp_path, schema_template, count=6) as (_, sessions, _):
            with closing(_OracleConnection(path)) as connection, connection:
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
            heads = [head for head in _oracle(path, "rows", "executerecord") if head["account_id"] == 2]
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
            with closing(_OracleConnection(path)) as connection:
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
            with closing(_OracleConnection(path)) as connection, connection:
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
