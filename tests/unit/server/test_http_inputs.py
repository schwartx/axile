"""HTTP 请求体沿用 FastAPI/Pydantic 的 JSON 类型检查。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.inputs import json_body
from axile.server.api.routes import portfolio
from axile.server.db.models.portfolio import PortfolioCreate, PortfolioUpdate


@pytest.mark.parametrize(
    "model,payload,error_type,location",
    [
        (
            PortfolioCreate,
            {"name": "contract", "market": "ctp", "custom_calc_py_code": " \n"},
            "value_error",
            ["body", "custom_calc_py_code"],
        ),
        (PortfolioUpdate, {"custom_calc_py_code": " \n"}, "value_error", ["body", "custom_calc_py_code"]),
        (PortfolioUpdate, {"custom_calc_py_code": None}, "value_error", ["body", "custom_calc_py_code"]),
        (PortfolioUpdate, {"unknown": True}, "extra_forbidden", ["body"]),
        (PortfolioCreate, {"name": "contract", "market": "ctp"}, "missing", ["body", "custom_calc_py_code"]),
    ],
)
def test_portfolio_validation_preserves_error_contract(model, payload, error_type, location):
    from fastapi import Depends

    app = FastAPI()

    @app.post("/portfolio")
    def save_portfolio(body=Depends(json_body(model))):
        pytest.fail("无效源码不应进入写入处理")

    with TestClient(app) as client:
        response = client.post("/portfolio", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == location
    assert response.json()["detail"][0]["type"] == error_type


@pytest.mark.parametrize(
    "content_type,expected_status",
    [("application/json", 200), ("application/vnd.axile+json", 200), ("text/plain", 422), (None, 422)],
)
def test_portfolio_code_requires_json_content_type(monkeypatch, content_type, expected_status):
    calls = []
    code = "def calculate_portfolio(context): return {}"

    async def run(_account, submitted_code, **_kwargs):
        calls.append(submitted_code)
        return {"valid": True, "target": {}}

    monkeypatch.setattr(portfolio, "_run_custom_calc_validation", run)
    app = FastAPI()
    app.include_router(portfolio.router)
    headers = {"Origin": "https://other.example"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    with TestClient(app) as client:
        response = client.post(
            "/portfolio/validate_custom_calc",
            content=json.dumps({"custom_calc_py_code": code, "simulation_channel": "ctp"}),
            headers=headers,
        )
    assert response.status_code == expected_status, response.text
    assert calls == ([code] if expected_status == 200 else [])
