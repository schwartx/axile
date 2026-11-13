"""HTTP 参数边界、账户币种继承及 worker 只读命令验收。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.models.cash_flow import ChannelCashFlow
from axile.server.api.routes import account_cash_flows as routes
from axile.server.execution.worker_backend import worker
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.conftest import native_store_call
from tests.unit.server._execution_test_support import build_account


def test_http_input_currency_revision_and_account_scope(native_database, monkeypatch):
    from axile.server.core import db

    account = build_account(id=1, trade_channel="tq", portfolio_id=None, is_started=False)
    native_store_call(
        native_database, "account_create", {"account": account.model_dump(mode="json"), "now": "2026-11-13"}
    )
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    monkeypatch.setattr(routes, "_get_account_or_404", AsyncMock(return_value=account))
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")
    client = TestClient(app)
    body = {"direction": "deposit", "amount": 500, "occurred_at": "2026-11-12T09:30:00+08:00"}
    assert client.post("/account/1/cash-flows", json={**body, "currency": "USD"}).status_code == 422
    assert client.post("/account/1/cash-flows", json={**body, "amount": True}).status_code == 422
    assert client.post("/account/1/cash-flows", json={**body, "occurred_at": "2026-11-12T09:30:00"}).status_code == 422
    response = client.post("/account/1/cash-flows", json=body)
    assert response.status_code == 201
    flow_id = response.json()["id"]
    listing = client.get("/account/1/cash-flows").json()
    assert listing["currency"] == "CNY"
    assert not listing["sync_supported"]
    assert listing["data"][0]["amount"] == 500
    assert client.patch(f"/account/1/cash-flows/{flow_id}", json={**body, "revision": 2}).status_code == 409
    assert client.post(f"/account/2/cash-flows/{flow_id}/void", json={"revision": 1}).status_code == 404


def test_worker_cash_flow_command_reuses_ready_session(monkeypatch):
    account = build_account(id=1)
    flow = ChannelCashFlow(
        direction="deposit", amount=500, occurred_at="2026-11-12T09:30:00+08:00", external_id="ctp:12"
    )
    executor = SimpleNamespace(get_account_cash_flows=lambda: [flow])
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return executor

    monkeypatch.setattr(worker, "_resolve_prepared_executor", resolve)
    monkeypatch.setattr(worker, "_finalize_with_orders", lambda *_: None)
    request = WorkerBackendRequest(
        request_id="cash-flow",
        command="get_account_cash_flows",
        account_payload=account.model_dump(mode="json"),
        execution_id=None,
        payload={},
    )
    response = worker._handle_worker_request(request, _WorkerBackendState())
    assert response.kind == "result"
    assert response.output_payload["rows"][0]["external_id"] == "ctp:12"
    assert calls[0]["allow_connect"] is False
    assert calls[0]["allow_recovery"] is False
