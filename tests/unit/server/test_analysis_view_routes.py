"""新版分析接口严格绑定源版本；前后端同步切换，不兼容快照查询。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_analysis as routes


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")
    monkeypatch.setattr(routes, "native_operation", AsyncMock(return_value={"version": "42"}))
    with TestClient(app) as value:
        yield value


@pytest.mark.parametrize(
    "suffix,params,command",
    [
        ("/status", {}, "status"),
        ("", {"version": "42", "range": "30"}, "chart"),
        ("/costs", {"version": "42", "dimension": "trade", "limit": 1}, "costs"),
        ("/execution/7", {"version": "42", "offset": 200}, "detail"),
    ],
)
def test_queries_use_new_native_operation(client, suffix, params, command):
    response = client.get("/account/analysis/1" + suffix, params=params)
    assert response.status_code == 200
    request = routes.native_operation.await_args.args
    assert request[0] == "analysis_query"
    assert request[1]["account_id"] == 1 and request[1]["command"] == command
    assert "snapshot_id" not in request[1]


@pytest.mark.parametrize(
    "suffix,params",
    [
        ("", {}),
        ("", {"version": "S1"}),
        ("", {"version": "1", "snapshot_id": "old"}),
        ("", {"version": "1", "range": "bad"}),
        ("/execution/7", {"version": "1", "limit": 501}),
        ("/costs", {"version": "1", "limit": 101}),
        ("/costs", {"version": "1", "record_id": 2**63}),
        ("/costs", {"version": "1", "start": 1}),
        ("/costs", {"version": "1", "start": 10, "end": 1}),
    ],
)
def test_invalid_queries_fail_before_reading(client, suffix, params):
    assert client.get("/account/analysis/1" + suffix, params=params).status_code == 422
    routes.native_operation.assert_not_awaited()


@pytest.mark.parametrize(
    "code,status",
    [
        ("ACCOUNT_NOT_FOUND", 404),
        ("EXECUTION_NOT_FOUND", 404),
        ("ANALYSIS_VERSION_CHANGED", 409),
        ("ANALYSIS_PENDING", 409),
        ("INVALID_REQUEST", 422),
        ("ANALYSIS_DATABASE", 503),
    ],
)
def test_native_errors_keep_explicit_http_boundary(client, code, status):
    routes.native_operation.side_effect = _core.NativeError(code, "message")
    response = client.get("/account/analysis/1", params={"version": "1"})
    assert response.status_code == status and response.json()["detail"]["code"] == code


def test_old_analysis_routes_are_removed(client):
    for suffix in ["", "/snapshot", "/chart", "/execution-summary"]:
        assert client.get("/account/performance/1" + suffix).status_code == 404


def test_refresh_is_explicit_and_enqueues_after_account_check(client, monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(routes, "enqueue", enqueue)
    monkeypatch.setattr(routes, "database_path", lambda: "/isolated.db")
    assert client.post("/account/analysis/1/refresh").status_code == 202
    enqueue.assert_awaited_once_with("/isolated.db", 1, force=True)
