"""分析设置保留；旧计算路由明确要求浏览器分析。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_performance as routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock())
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "endpoint", ["", "/snapshot", "/status", "/chart", "/execution-summary", "/execution-detail", "/costs", "/refresh"]
)
def test_backend_never_computes_performance(client, monkeypatch, endpoint):
    monkeypatch.setattr(routes._core, "account_performance", lambda *args: pytest.fail("backend calculation"))
    response = (
        client.post("/performance/1" + endpoint) if endpoint == "/refresh" else client.get("/performance/1" + endpoint)
    )
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "BROWSER_ANALYSIS_REQUIRED"


def test_missing_account_is_404(client, monkeypatch):
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock(side_effect=HTTPException(404, "账户不存在")))
    assert client.get("/performance/999").status_code == 404


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
