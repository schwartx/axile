"""事实同步的 HTTP 边界与日历准备顺序。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_analysis as routes


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "code,status",
    [
        ("ACCOUNT_NOT_FOUND", 404),
        ("SOURCE_CHANGED", 409),
        ("FACT_TOO_LARGE", 413),
        ("INVALID_REQUEST", 422),
        ("ANALYSIS_FACT_READ", 503),
    ],
)
def test_fact_errors_are_explicit(client, monkeypatch, code, status):
    monkeypatch.setattr(routes, "native_operation", AsyncMock(side_effect=_core.NativeError(code, "error")))
    response = client.get("/performance/1/facts-state")
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code, "message": "error"}


def test_calendar_is_prepared_before_new_revision_is_read(client, monkeypatch):
    order = []

    async def operation(name, request):
        order.append(name)
        if name == "analysis_context":
            return {"trade_channel": "ctp", "first_day": "2026-01-01", "last_day": "2026-01-03"}
        return b'{"source_version":2}'

    async def calendar(*args):
        order.append("calendar")
        assert args == ("ctp", "2026-01-01", "2026-01-03")

    monkeypatch.setattr(routes, "native_operation", operation)
    monkeypatch.setattr(routes, "ensure_calendar_facts", calendar)
    assert client.post("/performance/1/facts-prepare").json() == {"source_version": 2}
    assert order == ["analysis_context", "calendar", "browser_analysis_state"]


@pytest.mark.parametrize("account", ["0", "-1", str(2**63), "bad"])
def test_invalid_account_rejected_before_native(client, monkeypatch, account):
    operation = AsyncMock()
    monkeypatch.setattr(routes, "native_operation", operation)
    assert client.get(f"/performance/{account}/facts-state").status_code == 422
    operation.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"until": "-1"},
        {"until": "9" * 20},
        {"cursor": {"seq": "1", "table": 23}},
        {"cursor": {"seq": "1", "table": 0, "after": [1]}},
        {"unexpected": True},
    ],
)
def test_invalid_page_request_rejected(client, monkeypatch, patch):
    operation = AsyncMock()
    monkeypatch.setattr(routes, "native_operation", operation)
    response = client.post(
        "/performance/1/facts",
        json={"source_id": "source", "until": "1", "cursor": {"seq": "0", "table": 22, "after": None}, **patch},
    )
    assert response.status_code == 422
    operation.assert_not_called()
