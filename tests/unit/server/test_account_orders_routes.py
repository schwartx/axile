"""当前委托 REST 契约，失败保持未知，撤单受理不冒充实际终态。"""

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.server.api.routes import account_orders as routes
from axile.server.db.models.account_order import AccountOrderPublic, AccountOrdersPublic, OrderCancelReceipt
from axile.server.db.models.execution import ExecutionEvent
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._submission_test_support import initialize_submission_database


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")

    async def account(account_id):
        if account_id != 1:
            raise HTTPException(404, detail="账户不存在")
        return build_account()

    monkeypatch.setattr(routes, "_get_account_snapshot_or_404", account)
    with TestClient(app) as client:
        yield client


def test_missing_account_and_validation(client):
    assert client.get("/account/404/orders").status_code == 404
    assert client.post("/account/1/orders/cancel", json={"order_id": "a", "symbol": "b"}).status_code == 422


def test_empty_success_and_query_failure_are_distinct(client, monkeypatch):
    async def empty(_account, **_kwargs):
        return AccountOrdersPublic(data=[], count=0, sampled_at="2026-10-15T10:00:00")

    monkeypatch.setattr(routes, "query_account_orders", empty)
    assert client.get("/account/1/orders").json()["count"] == 0

    async def failed(_account, **_kwargs):
        raise RuntimeError("secret credential details")

    monkeypatch.setattr(routes, "query_account_orders", failed)
    response = client.get("/account/1/orders")
    assert response.status_code == 502
    assert "secret" not in response.text
    assert "count" not in response.json()


def test_cancel_returns_acceptance_separately_from_terminal_status(client, monkeypatch):
    async def cancel(_account, payload):
        return OrderCancelReceipt(
            **payload.model_dump(), state="submitted", message="等待实际回报", requested_at="2026-10-15T10:00:00"
        )

    monkeypatch.setattr(routes, "cancel_account_order", cancel)
    response = client.post("/account/1/orders/cancel", json={"request_id": "r1", "order_id": "a", "symbol": "rb2610"})
    assert response.status_code == 202
    assert response.json()["state"] == "submitted"


def test_timeout_returns_unknown_gateway_error(client, monkeypatch):
    async def timeout(_account, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(routes, "query_account_orders", timeout)
    assert client.get("/account/1/orders").status_code == 504


def test_recent_activity_validates_scope_and_keeps_partial_query_failure_visible(client, monkeypatch):
    from axile.server.db.models.account_order import AccountActivityPublic

    calls = []

    async def query(_account, **kwargs):
        calls.append(kwargs)
        return AccountActivityPublic(
            orders=[],
            trades=[],
            failures=[],
            scope="当前交易日",
            started_at="2026-10-16T09:00:00",
            sampled_at="2026-10-16T10:00:00",
            limit=100,
            errors=["成交核对失败"],
        )

    monkeypatch.setattr(routes, "query_account_activity", query)
    response = client.get("/account/1/orders/activity?refresh=true&symbol=rb2610")
    assert response.status_code == 200
    assert response.json()["errors"] == ["成交核对失败"]
    assert calls == [{"refresh": True, "symbol": "rb2610", "limit": 100}]
    assert client.get("/account/404/orders/activity").status_code == 404
    assert client.get("/account/1/orders/activity?limit=501").status_code == 422
    assert client.get("/account/1/orders/activity?symbol=%20bad").status_code == 422


def test_recent_activity_gateway_error_does_not_disclose_channel_exception(client, monkeypatch):
    async def fail(_account, **_kwargs):
        raise RuntimeError("password=secret")

    monkeypatch.setattr(routes, "query_account_activity", fail)
    response = client.get("/account/1/orders/activity")
    assert response.status_code == 502
    assert "secret" not in response.text


def test_current_orders_link_history_by_symbol_and_order_id(monkeypatch, tmp_path):
    from tests.conftest import native_store_call

    database = str(tmp_path / "orders.db")
    initialize_submission_database(database, monkeypatch)
    for execution, symbol, account_id in (
        ("old-rb", "rb2610", 1),
        ("new-rb", "rb2610", 1),
        ("ag", "ag2612", 1),
        ("another-account", "rb2610", 2),
    ):
        event = ExecutionEvent(
            account_id=account_id,
            execution_id=execution,
            event_uid=execution,
            channel="ctp",
            algorithm="SINGLE-MAKER",
            event_type=ExecutionEventType.ORDER_SUBMITTED,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="TEST.ORDER",
            symbol=symbol,
            order_id="same-id",
        )
        assert native_store_call(database, "append_execution_event", event.model_dump(mode="json")) is True

    async def query(_account, **_kwargs):
        return AccountOrdersPublic(
            data=[
                AccountOrderPublic(
                    order_id="same-id",
                    symbol=symbol,
                    direction="BUY",
                    order_type="LIMIT",
                    price=3500,
                    volume=1,
                    filled_volume=0,
                    remaining_volume=1,
                    status="待成交",
                )
                for symbol in ("rb2610", "ag2612", "cu2612")
            ],
            count=3,
            sampled_at="2026-10-15T10:00:00",
        )

    monkeypatch.setattr(routes, "query_account_orders", query)

    async def scenario():
        from axile.server.account_orders import _link_executions

        result = await query(build_account())
        await _link_executions(build_account(), result)
        assert {row.symbol: row.execution_id for row in result.data} == {
            "rb2610": "new-rb",
            "ag2612": "ag",
            "cu2612": None,
        }

    asyncio.run(scenario())


def test_history_link_uses_one_batch_and_keeps_existing_associations(monkeypatch):
    from axile.server import account_orders as service

    calls = []

    async def lookup(name, request):
        calls.append((name, request))
        return [["rb2610", "same-id", "history"]]

    monkeypatch.setattr(service, "native_operation", lookup)
    result = AccountOrdersPublic(
        data=[
            AccountOrderPublic(
                order_id="same-id",
                symbol=symbol,
                direction="BUY",
                order_type="LIMIT",
                price=3500,
                volume=1,
                filled_volume=0,
                remaining_volume=1,
                status="待成交",
                execution_id=execution,
            )
            for symbol, execution in (("rb2610", None), ("ag2612", "existing"))
        ],
        count=2,
        sampled_at="2026-10-15T10:00:00",
    )
    asyncio.run(service._link_executions(build_account(), result))
    assert calls == [("order_execution_links", {"account_id": 1, "identities": [("rb2610", "same-id")]})]
    assert [order.execution_id for order in result.data] == ["history", "existing"]
    asyncio.run(service._link_executions(build_account(), result))
    assert len(calls) == 1
