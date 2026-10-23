"""历史查询路由的统一分页边界测试。"""

import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.domain.execution import ExecutionArtifactType
from axile.server._native import _core
from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud
from axile.server.db.models import ExecutionArtifact, ExecutionEvent
from tests.conftest import native_store_call
from tests.fixtures.rust_store.oracle_tables import table
from tests.unit.server._execution_test_support import build_account, insert_account

_ROUTES = [
    ("/account/execute_records/1", "execution_records", "executerecord"),
    ("/account/portfolio_records/1", "account_portfolio_records_json", "portfolioaccount"),
    ("/account/executions/execution-1/events", "list_execution_events", "execution_event"),
    ("/account/executions/execution-1/artifacts", "list_execution_artifacts", "execution_artifact"),
]


class _Scheduler:
    def get_job(self, _job_id: str) -> None:
        return None


def _build_app():
    app = FastAPI()
    app.include_router(account_routes.router)
    app.dependency_overrides[get_scheduler] = _Scheduler
    return app


@pytest.mark.parametrize("path,operation,_table", _ROUTES)
@pytest.mark.parametrize("params", [{"skip": -1}, {"limit": 0}, {"limit": 501}])
def test_history_routes_reject_out_of_range_pagination_before_database_access(
    path, operation, _table, params, monkeypatch
):
    """四个历史端点必须在执行查询前拒绝越界分页参数。"""
    query = Mock(side_effect=AssertionError("invalid pagination must not query the database"))
    monkeypatch.setattr(_core, operation, query)
    with TestClient(_build_app()) as client:
        response = client.get(path, params=params)
    assert response.status_code == 422
    query.assert_not_called()


def _seed(path, name, count):
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_account(connection, build_account(id=1, portfolio_id=None))
    if name == "executerecord":
        for identity in range(1, count + 1):
            native_store_call(
                path,
                "persist_execute_record",
                {
                    "account_id": 1,
                    "execution_id": f"pagination-{identity}",
                    "created_at": "2026-10-20T09:00:00",
                    "is_success": 1,
                    "input_id": f"pagination-input-{identity}",
                    "result_input_id": f"pagination-result-{identity}",
                    "raw_input": {"curr_target": {}},
                    "raw_result": {"status": "NOOP"},
                },
            )
        return
    if name == "portfolioaccount":
        values = [
            {"id": identity, "account_id": 1, "portfolio_id": None, "created_at": f"stamp-{identity:04d}"}
            for identity in range(1, count + 1)
        ]
    else:
        values = [
            (
                ExecutionEvent(
                    id=identity,
                    execution_id="execution-1",
                    account_id=1,
                    channel="ctp",
                    algorithm="SINGLE-MAKER",
                    event_uid=f"page-event-{identity}",
                    event_type="execution_started",
                    status="INFO",
                    reason_family="SYSTEM",
                    reason_code="TEST.PAGING",
                )
                if name == "execution_event"
                else ExecutionArtifact(
                    id=identity, execution_id="execution-1", artifact_type=list(ExecutionArtifactType)[identity - 1]
                )
            ).model_dump(mode="json")
            for identity in range(1, count + 1)
        ]
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(table(name).insert(), values)
    finally:
        engine.dispose()


@pytest.mark.parametrize("path,operation,name", _ROUTES)
def test_history_routes_accept_pagination_bounds_and_apply_limit(path, operation, name, native_database, monkeypatch):
    """实际原生阶段返回最多 500 行，并保留总数、顺序和下一页。"""
    count = len(ExecutionArtifactType) if name == "execution_artifact" else 501
    _seed(native_database, name, count)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(native_database)))
    query = Mock(wraps=getattr(_core, operation))
    monkeypatch.setattr(_core, operation, query)
    with TestClient(_build_app()) as client:
        response = client.get(path, params={"skip": 0, "limit": 500})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["count"] == count
        assert len(payload["data"]) == min(count, 500)
        query.assert_called_once()
        if name == "executerecord":
            assert query.call_args.args[1:4] == (1, 0, 500)
            assert [item["id"] for item in payload["data"]] == list(range(501, 1, -1))
            assert all({"input", "result", "raw_input", "raw_result"}.isdisjoint(item) for item in payload["data"])
        else:
            assert json.loads(query.call_args.args[1])["limit"] == 500
            assert json.loads(query.call_args.args[1])["skip"] == 0
            if name == "portfolioaccount":
                assert [item["created_at"] for item in payload["data"]] == [
                    f"stamp-{identity:04d}" for identity in range(1, 501)
                ]
            else:
                assert [item["id"] for item in payload["data"]] == list(range(1, min(count, 500) + 1))
        query.reset_mock()
        remainder = client.get(path, params={"skip": 500, "limit": 500})
        assert remainder.status_code == 200, remainder.text
        assert remainder.json()["count"] == count
        assert len(remainder.json()["data"]) == max(0, count - 500)
        query.assert_called_once()
        if count < 500:
            assert remainder.json()["data"] == []
            return
        last = remainder.json()["data"][0]
        if name == "portfolioaccount":
            assert last["created_at"] == "stamp-0501"
        else:
            assert last["id"] == (1 if name == "executerecord" else 501)
