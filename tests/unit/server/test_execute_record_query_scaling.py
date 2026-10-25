"""单账户执行记录查询的隔离、排序与条数限制。"""

import asyncio
from contextlib import closing
from unittest.mock import Mock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from axile.server._native import _core
from axile.server.api.routes import account_crud
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_analysis_support import analysis_record, database, persist_records
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_test_support import build_account, insert_account


def test_recent_records_preserve_account_limit_and_id_order(tmp_path, schema_template, monkeypatch):
    """真实 HTTP 列表保持账户隔离、ID 倒序及分页，不加载完整结果。"""
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(tmp_path / "analysis.db")))

    async def scenario():
        async with database(tmp_path, schema_template, count=0) as (_, sessions, _):
            with closing(oracle_connect(tmp_path / "analysis.db")) as connection, connection:
                for account_id in (1, 3, 4):
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
            operation = Mock(wraps=_core.execution_records)
            monkeypatch.setattr(_core, "execution_records", operation)
            app = FastAPI()
            app.include_router(account_crud.router)
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                for account_id in (1, 2, 3):
                    operation.reset_mock()
                    response = await client.get(f"/execute_records/{account_id}", params={"limit": 3})
                    assert response.status_code == 200, response.text
                    operation.assert_called_once()
                    assert operation.call_args.args[1:] == (account_id, 0, 3, None, None)
                    page = response.json()
                    assert page["count"] == 5
                    records = page["data"]
                    assert [row["id"] for row in records] == [account_id * 5 - offset for offset in range(3)]
                    assert all(row["account_id"] == account_id for row in records)
                    assert [row["is_success"] for row in records] == [0, 1, 0]
                    assert all({"input", "result", "raw_input", "raw_result"}.isdisjoint(row) for row in records)

                response = await client.get("/execute_records/1", params={"skip": 3, "limit": 3})
                assert response.status_code == 200
                assert response.json()["count"] == 5
                assert [row["id"] for row in response.json()["data"]] == [2, 1]
                for identity, count in ((5, 1), (15, 0)):
                    response = await client.get("/execute_records/1", params={"record_id": identity})
                    assert response.status_code == 200
                    assert response.json()["count"] == count
                    assert [row["id"] for row in response.json()["data"]] == ([5] if count else [])
                response = await client.get("/execute_records/4")
                assert response.status_code == 200
                assert response.json() == {"data": [], "count": 0}
                response = await client.get("/execute_records/5")
                assert response.status_code == 404
                operation.reset_mock()
                for params in ({"limit": 0}, {"limit": 501}, {"skip": -1}):
                    response = await client.get("/execute_records/1", params=params)
                    assert response.status_code == 422
                operation.assert_not_called()

    asyncio.run(scenario())
