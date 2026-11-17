"""HTTP 请求体沿用 FastAPI/Pydantic 的 JSON 类型检查。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import portfolio


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
