"""回测设置和分析计算使用同一个事实失效事务。"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_performance as routes


def test_backtest_save_normalizes_settings_in_one_native_call(monkeypatch):
    result = {"backtest_weight_type": "cs", "backtest_fee_rate": 0.01}
    write = AsyncMock(return_value=result)
    monkeypatch.setattr(routes, "native_operation", write)
    monkeypatch.setattr(routes, "now_str", lambda: "2026-01-01T09:00:00")
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.patch("/performance-settings/1", json={**result, "backtest_weight_type": "ts"})
    assert response.status_code == 200 and response.json() == result
    write.assert_awaited_once_with(
        "account_update_backtest", {"account_id": 1, "patch": result, "now": "2026-01-01T09:00:00"}
    )


def test_backtest_save_preserves_missing_account(monkeypatch):
    monkeypatch.setattr(
        routes, "native_operation", AsyncMock(side_effect=_core.NativeError("ACCOUNT_NOT_FOUND", "账户不存在"))
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.patch("/performance-settings/1", json={"backtest_fee_rate": 0, "backtest_weight_type": "cs"})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "ACCOUNT_NOT_FOUND"
