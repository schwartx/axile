"""执行记录并发收尾幂等：同一执行保留首条已提交终态。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest

from axile.server.core import db
from axile.server.execution.records import _persist_execute_record
from tests.conftest import require_native_store
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._native_read_views import persist_record_fixture, read_execution_result, read_view


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    with closing(oracle_connect(native_database)) as connection, connection:
        insert_account(connection, build_account(id=1, portfolio_id=None))
    return native_database


def test_duplicate_execution_record_returns_first(database) -> None:
    """同一执行的两条收尾路径落库时保留首条，不抛唯一约束异常。"""

    async def scenario():
        first = await _persist_execute_record(
            account_id=1,
            execution_id="exec-dup-1",
            raw_input={},
            raw_result={"task_status": "TERMINATED"},
            is_success=0,
        )
        second = await _persist_execute_record(
            account_id=1,
            execution_id="exec-dup-1",
            raw_input={},
            raw_result={"task_status": "TERMINATED", "finalized": True},
            is_success=0,
        )
        return first["record_id"], second, await read_execution_result(second)

    first_id, second, result = asyncio.run(scenario())
    assert first_id == second["record_id"]
    assert isinstance(result, dict)
    assert result == {"task_status": "TERMINATED"}
    assert "finalized" not in result
    assert second["status"] == "TERMINATED"


def test_native_concurrent_terminal_writers_share_one_committed_record(database) -> None:
    """独立原生写连接竞争同一唯一索引，胜出的终态与事实始终一致。"""
    barrier = threading.Barrier(2)
    results = [{"task_status": "TERMINATED", "msg": f"writer-{index}"} for index in (1, 2)]

    def persist(result):
        barrier.wait(timeout=2)
        return persist_record_fixture(
            database,
            {
                "account_id": 1,
                "execution_id": "exec-race",
                "raw_input": {},
                "raw_result": result,
                "is_success": 0,
                "created_at": "2026-10-23T10:00:00",
                "input_id": str(uuid4()),
                "result_input_id": str(uuid4()),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(persist, results))
    assert first == second
    stored = read_view(database, "read_execution_result", {"record_id": first["id"], "account_id": 1})
    assert stored in results
    core = require_native_store("execution_records")
    page = json.loads(core.execution_records(core.Store(str(database)), 1, 0, 10))
    assert page["count"] == 1
    assert len(page["data"]) == 1
    for field in ("id", "account_id", "execution_id", "created_at", "is_success", "task_status"):
        assert page["data"][0][field] == first[field]
    with closing(oracle_connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM executerecord").fetchone()[0] == 1
        indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(executerecord)")}
        assert indexes["ix_executerecord_execution_id"] == 1


def test_unique_id_conflict_without_execution_id_still_raises(database) -> None:
    """execution_id 为 NULL 时允许两条独立记录，不能错误地合并终态。"""

    async def scenario():
        records = []
        for _ in range(2):
            records.append(
                await _persist_execute_record(
                    account_id=1,
                    execution_id=None,
                    raw_input={},
                    raw_result={},
                    is_success=1,
                )
            )
        return records

    first, second = asyncio.run(scenario())
    assert first["record_id"] != second["record_id"]
    with closing(oracle_connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM executerecord WHERE execution_id IS NULL").fetchone()[0] == 2
