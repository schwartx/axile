"""原生活动品种读取保持成交顺序、汇总口径和账户隔离。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from axile.server._native import _core
from axile.server.api.routes import account_schedule
from tests.unit.server._business_analysis_support import database, persist_records, record
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._performance_costs_oracle import project_execution, summarize


def test_native_preserves_all_fills_summary_bounds_and_account_scope(tmp_path, schema_template, monkeypatch):
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(tmp_path / "analysis.db")))

    async def check():
        async with database(tmp_path, schema_template, count=205) as (_, sessions, _):
            expected = [
                {**trade, "record_id": row.id, "execution_id": row.execution_id}
                for row in [record(index) for index in range(205, 0, -1)]
                for trade in project_execution(row)[1]
            ]
            with closing(sqlite3.connect(tmp_path / "analysis.db")) as connection, connection:
                insert_account(connection, build_account(id=3, account_control_preset="default"))
                connection.execute(
                    "UPDATE execution_input SET algorithm=? WHERE id=(SELECT input_id FROM executerecord WHERE id=1)",
                    (json.dumps({"params": {"unused": "x" * 2_000_000}}),),
                )
            async with sessions() as session:
                other = record(206)
                other.account_id = 3
                await persist_records(session, [other])
            with (
                patch.object(AsyncSession, "execute", side_effect=AssertionError("no Python session reads")),
                patch.object(AsyncSession, "stream", side_effect=AssertionError("no Python trade stream")),
            ):
                expanded = json.loads(
                    (await account_schedule.account_activity_symbols(2, "2020", "2030", symbol="A")).body
                )
                assert expanded["count"] == 1
                row = expanded["data"][0]
                assert row["trades"] == expected
                assert row["summary"] == summarize(expected)
                assert all(isinstance(trade["timeEstimated"], bool) for trade in row["trades"])
                overview = json.loads((await account_schedule.account_activity_symbols(2, "", "", symbol=None)).body)
                assert overview["data"][0]["summary"] == row["summary"]
                assert overview["data"][0]["trades"] == []
                assert overview["data"][0]["last_time"] == int(max(trade["time"] for trade in expected))
                for since, until, symbol in [("2020", "2030", "missing"), ("2090", "2091", None), ("", "", "")]:
                    assert json.loads(
                        (await account_schedule.account_activity_symbols(2, since, until, symbol=symbol)).body
                    ) == {
                        "data": [],
                        "count": 0,
                    }
                bounded = json.loads(
                    (
                        await account_schedule.account_activity_symbols(
                            2, record(2).created_at, record(3).created_at, symbol="A"
                        )
                    ).body
                )
                assert bounded["data"][0]["trades"][0]["record_id"] == 2
                assert bounded["data"][0]["n_trades"] == 1
                with pytest.raises(HTTPException) as error:
                    json.loads((await account_schedule.account_activity_symbols(999, "", "", symbol=None)).body)
                assert error.value.status_code == 404
                assert error.value.detail == "账户不存在"

    asyncio.run(check())


def test_native_route_requires_time_bounds_without_session_dependency():
    app = FastAPI()
    app.include_router(account_schedule.router)
    route = next(route for route in account_schedule.router.routes if route.path == "/{account_id}/activity/symbols")
    assert route.dependant.dependencies == []
    with TestClient(app) as client:
        for params in ({}, {"since": "2020"}, {"until": "2030"}):
            response = client.get("/2/activity/symbols", params=params)
            assert response.status_code == 422
            assert {error["loc"][-1] for error in response.json()["detail"]} == {"since", "until"} - params.keys()


@pytest.mark.parametrize("code,status", [("ACCOUNT_NOT_FOUND", 404), ("SYMBOLS_READ_ERROR", 503)])
def test_native_errors_become_http_errors(monkeypatch, code, status):
    monkeypatch.setattr(account_schedule, "analysis_store", object)

    def fail(*_args):
        raise _core.NativeError(code, "read failed")

    monkeypatch.setattr(_core, "activity_symbols", fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_schedule.account_activity_symbols(2, "", "", symbol=None))
    assert error.value.status_code == status
    assert error.value.detail == ("账户不存在" if status == 404 else {"code": code, "message": "read failed"})
