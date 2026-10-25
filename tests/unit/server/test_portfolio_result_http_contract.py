"""Result API 退役后保留 HTTP 状态、错误定位及原快照。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import portfolio as routes
from axile.server.db.models import Portfolio
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError
from tests.unit.server._execution_test_support import build_account

GOOD = "def calculate_portfolio(context):\n    return {'x': 0.25}\n"
BAD = "def calculate_portfolio(context)\n    return {}\n"
REFRESH = "/api/v1/portfolio/991/target_snapshot/refresh"
VALIDATE = "/api/v1/portfolio/validate_custom_calc"


def _client(monkeypatch, *, code=BAD, account=None):
    portfolio = Portfolio(id=991, name="retained", market="futures", custom_calc_py_code=code)

    async def native(name, request):
        assert name == "portfolio_operation"
        if request["op"] == "validation_context":
            assert account is not None
            return account.model_dump(mode="json")
        assert request["op"] == "context"
        return {
            "portfolio": portfolio.model_dump(mode="json"),
            "account_id": account.id if account is not None else None,
            "account": account.model_dump(mode="json") if account is not None else None,
        }

    monkeypatch.setattr(routes, "_portfolio_native", native)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.parametrize("code,valid", [(GOOD, True), (BAD, False)])
def test_sample_validation_http_keeps_success_and_structured_failure(monkeypatch, code, valid):
    with _client(monkeypatch) as client:
        response = client.post(VALIDATE, json={"custom_calc_py_code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is valid
    if valid:
        assert body["target"] == {"x": 0.25}
    else:
        assert body["target"] is None
        assert body["error_type"] == "SyntaxError" and body["error_line"] == 1
        assert body["error_offset"] is not None and "SyntaxError" in body["traceback"]
        assert body["error_message"] == body["error"]


def test_refresh_script_failure_keeps_400_and_does_not_write_snapshot(monkeypatch):
    save = AsyncMock(side_effect=AssertionError("failed script cannot overwrite snapshot"))
    monkeypatch.setattr(routes, "append_target_weight_snapshot", save)
    with _client(monkeypatch) as client:
        response = client.post(REFRESH)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["exception_type"] == "PortfolioFunctionError"
    assert "SyntaxError" in detail["traceback"]
    assert detail["message"].startswith("自定义组合函数执行失败：")
    save.assert_not_awaited()
    assert routes.try_register_target_refresh(991, None)
    routes.clear_target_refresh(991, None)


@pytest.mark.parametrize("validation", [False, True])
def test_sample_start_failure_preserves_500_vs_validation_response(monkeypatch, validation):
    def failed_start(code):
        raise RuntimeError("spawn start failed")

    monkeypatch.setattr(routes, "calculate_sample_portfolio", failed_start)
    with _client(monkeypatch) as client:
        response = client.post(VALIDATE if validation else REFRESH, json={"custom_calc_py_code": GOOD})
    if validation:
        assert response.status_code == 200 and response.json()["valid"] is False
        assert response.json()["error_type"] == "RuntimeError"
    else:
        assert response.status_code == 500
        assert response.json()["detail"]["exception_type"] == "RuntimeError"
    assert "spawn start failed" in response.text


@pytest.mark.parametrize("validation", [False, True])
def test_account_prepare_failure_keeps_http_classification_and_remote_evidence(monkeypatch, validation):
    class Manager:
        async def calculate_portfolio(self, *args, **kwargs):
            raise WorkerBackendExecutionError(
                "public",
                technical_detail="SDK 1026",
                formatted_traceback="remote SDK traceback",
                error_scope="account_connection",
            )

    monkeypatch.setattr(worker_manager, "get_worker_backend_manager", Manager)
    with _client(monkeypatch, code=GOOD, account=build_account(id=7)) as client:
        response = client.post(VALIDATE if validation else REFRESH, json={"custom_calc_py_code": GOOD, "account_id": 7})
    if validation:
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False and body["error_type"] == "WorkerBackendExecutionError"
        assert body["error"] == "SDK 1026" and body["traceback"] == "remote SDK traceback"
    else:
        assert response.status_code == 400
        body = response.json()["detail"]
        assert body["error_scope"] == "account_connection"
        assert body["technical_detail"] == "SDK 1026" and body["traceback"] == "remote SDK traceback"
