"""单账户执行记录查询的隔离、排序与条数限制。"""

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

from axile.server import repositories
from tests.unit.server._business_analysis_support import analysis_record, database, persist_records
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_test_support import build_account, insert_account


def test_recent_records_preserve_account_limit_and_id_order(tmp_path, schema_template, monkeypatch):
    """每次原生阶段只返回所需账户的有界头记录，顺序由 ID 决定。"""
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "analysis.db"))

    async def scenario():
        async with database(tmp_path, schema_template, count=0) as (_, sessions, _):
            with closing(sqlite3.connect(tmp_path / "analysis.db")) as connection, connection:
                for account_id in (1, 3):
                    insert_account(connection, build_account(id=account_id))
            async with sessions() as session:
                await persist_records(
                    session,
                    [
                        analysis_record(
                            id=(account_id - 1) * 5 + index + 1,
                            account_id=account_id,
                            execution_id=None,
                            input={},
                            result={"account_assets": {"total_asset": 100.0 + index}},
                            is_success=index % 2,
                            created_at=f"2026-07-{5 - index:02d}T09:00:00",
                        )
                        for account_id in (1, 2, 3)
                        for index in range(5)
                    ],
                )
            operation = AsyncMock(wraps=repositories.native_operation)
            monkeypatch.setattr(repositories, "native_operation", operation)
            for account_id in (1, 2, 3):
                operation.reset_mock()
                records = await repositories.get_recent_execute_records_by_account_id(account_id, limit=3)
                operation.assert_awaited_once_with("recent_execution_records", {"account_id": account_id, "limit": 3})
                assert [row.id for row in records] == [account_id * 5 - offset for offset in range(3)]
                assert all(row.account_id == account_id for row in records)
                assert [row.is_success for row in records] == [0, 1, 0]
                assert all(
                    {"input", "result", "raw_input", "raw_result"}.isdisjoint(row.model_dump()) for row in records
                )
            operation.reset_mock()
            assert await repositories.get_recent_execute_records_by_account_id(4) == []
            operation.assert_awaited_once_with("recent_execution_records", {"account_id": 4, "limit": 20})
            operation.reset_mock()
            assert await repositories.get_recent_execute_records_by_account_id(None) == []
            operation.assert_not_awaited()

    asyncio.run(scenario())
