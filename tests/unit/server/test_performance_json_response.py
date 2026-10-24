"""绩效 HTTP 出口直接发送原生 JSON，保留状态与完整响应正文。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import account_performance as routes


@pytest.mark.parametrize(
    "url,operation,status",
    [
        ("/performance/1", "performance_result", "ready"),
        ("/performance/1?include_backtest=false", "account_performance", "pending"),
        ("/performance/1/snapshot", "snapshot", "ready"),
    ],
)
def test_performance_response_uses_native_json_bytes(monkeypatch, url, operation, status):
    content = b'{ "zero":0, "unknown":null, "large_id":9223372036854775807 }\n'
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock())
    monkeypatch.setattr(routes, "database_path", lambda: "unused")
    monkeypatch.setattr(routes, "analysis_store", object)
    monkeypatch.setattr(routes, "read_snapshot_status", AsyncMock(return_value=status))
    monkeypatch.setattr(routes._core, operation, lambda *_args: content)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == content
