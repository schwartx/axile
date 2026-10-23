"""审计路由验证参数、错误状态，并直接输出原生 JSON 字节。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server import account_control_audit
from axile.server._native import _core
from axile.server.api.routes import account_control_audit_routes


def client():
    app = FastAPI()
    app.include_router(account_control_audit_routes.router, prefix="/account")
    return TestClient(app)


def test_queries_require_date_and_reject_excessive_page_sizes(monkeypatch):
    async def unexpected(*_args):
        raise AssertionError("invalid query reached native storage")

    monkeypatch.setattr(account_control_audit, "native_operation", unexpected)
    current = client()
    for path, params in [
        ("/account/1/control/audit/summary", {}),
        ("/account/1/control/audit/executions", {}),
        ("/account/1/control/audit/executions", {"control_date": "2026-03-22", "limit": 501}),
        ("/account/executions/exec/control/events", {"skip": -1}),
        ("/account/executions/exec/control/events", {"limit": 501}),
    ]:
        assert current.get(path, params=params).status_code == 422


def test_execution_events_forward_filters_and_native_bytes_without_reencoding(monkeypatch):
    payload = b' {"data": [{"decision":"allowed", "metadata":{"null":null}}], "count":1} '

    async def native(name, request):
        assert name == "control_audit_events_json"
        assert request == {
            "execution_id": "exec-1",
            "symbol": "rb2610",
            "operation": "place_order",
            "skip": 0,
            "limit": 2,
        }
        return payload

    monkeypatch.setattr(account_control_audit, "native_operation", native)
    response = client().get(
        "/account/executions/exec-1/control/events", params={"symbol": "rb2610", "operation": "place_order", "limit": 2}
    )
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("kind", ["summary", "executions"])
def test_missing_account_keeps_existing_404_response(monkeypatch, kind):
    async def native(_name, _request):
        raise _core.NativeError("ACCOUNT_NOT_FOUND", "Account not found")

    monkeypatch.setattr(account_control_audit, "native_operation", native)
    response = client().get(f"/account/99/control/audit/{kind}", params={"control_date": "2026-03-22"})
    assert response.status_code == 404
    assert response.json() == {"detail": "账户不存在"}
