"""绩效路由复用原生读写；成本 JSON 字节直接透传。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_performance as routes
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, read_snapshot_status


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock())
    monkeypatch.setattr(routes, "analysis_store", lambda: object())
    monkeypatch.setattr(routes, "database_path", lambda: "/isolated/analysis.sqlite")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_cost_response_passes_native_json_bytes_through():
    content = b'{"data": [], "count": 0}'
    response = routes._json_response(content)
    assert response.body is content
    assert response.media_type == "application/json"


@pytest.mark.parametrize(
    ("snapshot_id", "compatible", "error", "requested", "running", "published", "expected"),
    [
        (None, True, None, False, None, 1, "empty"),
        (None, True, None, False, None, 0, "empty"),
        (None, True, None, True, None, 0, "pending"),
        (None, True, None, False, 0, 1, "pending"),
        ("", False, "", False, None, 1, "empty"),
        ("s1", True, "", False, None, 1, "ready"),
        ("s1", True, None, False, None, 0, "stale"),
        ("s1", True, None, True, 1, 1, "stale"),
        ("s1", True, "failed", True, 1, 0, "failed"),
        ("s1", False, "failed", True, 1, 0, "pending"),
        ("s1", False, None, False, None, 1, "pending"),
    ],
)
def test_wait_status_uses_native_rules_in_committed_database(
    tmp_path, snapshot_id, compatible, error, requested, running, published, expected
) -> None:
    if not hasattr(_core, "queue_status"):
        pytest.skip("Queue status requires rebuilt native extension")
    path = str(tmp_path / "status.sqlite")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
        CREATE TABLE account_analysis(account_id INTEGER, current_snapshot TEXT, error TEXT,
          requested INTEGER, running_version INTEGER, published_version INTEGER, source_version INTEGER);
        CREATE TABLE performance_snapshot(id TEXT, logic_version TEXT, engine_version TEXT);
        """)
        connection.execute(
            "INSERT INTO account_analysis VALUES (1,?,?,?,?,?,1)", (snapshot_id, error, requested, running, published)
        )
        if snapshot_id is not None:
            connection.execute(
                "INSERT INTO performance_snapshot VALUES (?,?,?)",
                (snapshot_id, LOGIC_VERSION if compatible else "old", ENGINE_VERSION),
            )
    assert asyncio.run(read_snapshot_status(path, 1)) == expected


def test_performance_route_computes_synchronously_without_backtest(client, monkeypatch):
    payload = {
        "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
        "engine_version": ENGINE_VERSION,
        "range": "all",
        "backtest_included": False,
        "record_count": 2,
        "calendar": {"status": "not_required"},
    }
    status = AsyncMock(return_value="empty")
    monkeypatch.setattr(routes, "read_snapshot_status", status)
    monkeypatch.setattr(routes._core, "account_performance", lambda *args: json.dumps(payload).encode())
    response = client.get("/performance/1", params={"include_backtest": False})
    assert response.status_code == 200
    assert response.json()["record_count"] == 2
    assert response.json()["backtest_included"] is False
    status.assert_awaited_once_with("/isolated/analysis.sqlite", 1)


@pytest.mark.parametrize("include_backtest", [True, False])
def test_ready_snapshot_invalidated_during_read_retries_or_uses_account_returns(client, monkeypatch, include_backtest):
    content = b'{"record_count":2}'
    read = Mock(side_effect=[_core.NativeError("SNAPSHOT_NOT_READY", "快照已更新"), content])
    account_returns = Mock(return_value=content)
    status = AsyncMock(side_effect=["ready", "pending", "ready"])
    enqueue = AsyncMock()
    monkeypatch.setattr(routes, "read_snapshot_status", status)
    monkeypatch.setattr(routes._core, "performance_result", read)
    monkeypatch.setattr(routes._core, "account_performance", account_returns)
    monkeypatch.setattr(routes, "enqueue", enqueue)
    monkeypatch.setattr(routes, "clock_async_sleep", AsyncMock())

    response = client.get("/performance/1", params={"include_backtest": include_backtest})

    assert response.status_code == 200, response.text
    assert response.content == content
    assert read.call_count == (2 if include_backtest else 1)
    if include_backtest:
        account_returns.assert_not_called()
        enqueue.assert_awaited_once_with("/isolated/analysis.sqlite", 1)
    else:
        account_returns.assert_called_once()
        enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    "code,status_code", [("SNAPSHOT_EXPIRED", 410), ("INVALID_REQUEST", 422), ("DATABASE_QUERY", 503)]
)
def test_ready_snapshot_preserves_other_native_errors(client, monkeypatch, code, status_code):
    monkeypatch.setattr(routes, "read_snapshot_status", AsyncMock(return_value="ready"))
    monkeypatch.setattr(routes._core, "performance_result", Mock(side_effect=_core.NativeError(code, "查询失败")))
    response = client.get("/performance/1")
    assert response.status_code == status_code
    assert response.json() == {"detail": {"code": code, "message": "查询失败"}}


def test_performance_route_rejects_unknown_account_before_analysis(client, monkeypatch):
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock(side_effect=HTTPException(404, "账户不存在")))
    monkeypatch.setattr(routes, "analysis_store", lambda: pytest.fail("unknown account must not open Store"))
    assert client.get("/performance/1").status_code == 404


def test_snapshot_route_reads_published_snapshot(client, monkeypatch):
    payload = {
        "status": "ready",
        "source_version": 1,
        "snapshot_id": "snap-1",
        "snapshot_version": 1,
        "logic_version": LOGIC_VERSION,
        "engine_version": ENGINE_VERSION,
        "computed_at": None,
        "data_until": None,
        "settings": None,
        "error": None,
        "retry_at": None,
        "result": None,
        "daily_costs": {},
        "events": [],
        "event_count": 0,
    }
    monkeypatch.setattr(routes._core, "snapshot", lambda *args: json.dumps(payload).encode())
    response = client.get("/performance/1/snapshot")
    assert response.status_code == 200
    assert response.json() == payload


def test_backtest_save_uses_one_native_account_transaction(client, monkeypatch):
    write = AsyncMock(return_value=b'{ "backtest_weight_type":"cs", "backtest_fee_rate":0.01 }')
    monkeypatch.setattr(routes, "native_operation", write)
    monkeypatch.setattr(routes, "now_str", lambda: "2026-01-01T09:00:00")
    response = client.patch("/performance-settings/1", json={"backtest_weight_type": "ts", "backtest_fee_rate": 0.01})
    assert response.status_code == 200
    assert response.json() == {"backtest_weight_type": "cs", "backtest_fee_rate": 0.01}
    assert response.content == write.return_value
    write.assert_awaited_once_with(
        "account_update_backtest",
        {
            "account_id": 1,
            "patch": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.01},
            "now": "2026-01-01T09:00:00",
        },
    )


def test_backtest_save_preserves_account_404(client, monkeypatch):
    write = AsyncMock(side_effect=_core.NativeError("ACCOUNT_NOT_FOUND", "账户不存在"))
    monkeypatch.setattr(routes, "native_operation", write)
    response = client.patch("/performance-settings/1", json={"backtest_weight_type": "cs", "backtest_fee_rate": 0.0})
    assert response.status_code == 404
    assert response.json()["detail"] == "账户不存在"


def test_cost_route_accepts_native_legacy_response_without_python_sql(client, monkeypatch):
    content = b'{"snapshot_id":"legacy","data":[],"count":0}'
    monkeypatch.setattr(routes._core, "query_costs", lambda *args: content)
    response = client.get("/performance/1/costs", params={"snapshot_id": "legacy"})
    assert response.status_code == 200
    assert response.content == content


def test_cost_route_preserves_native_expired_snapshot_error(client, monkeypatch):
    def read(*args):
        raise _core.NativeError("SNAPSHOT_EXPIRED", "快照版本已失效，请重新读取绩效")

    monkeypatch.setattr(routes._core, "query_costs", read)
    response = client.get("/performance/1/costs", params={"snapshot_id": "expired"})
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "SNAPSHOT_EXPIRED"
