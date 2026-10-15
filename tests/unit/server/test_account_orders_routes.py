"""当前委托 REST 契约，失败保持未知，撤单受理不冒充实际终态。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.server.api.deps import get_db
from axile.server.api.routes import account_orders as routes
from axile.server.db.models.account_order import AccountOrderPublic, AccountOrdersPublic, OrderCancelReceipt
from axile.server.db.models.execution import ExecutionEvent
from tests.unit.server._execution_test_support import build_account


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")

    async def session():
        account = build_account()

        async def get(_model, account_id):
            return account if account_id == 1 else None

        async def close():
            pass

        async def execute(_query):
            return SimpleNamespace(all=lambda: [])

        yield SimpleNamespace(get=get, close=close, execute=execute)

    app.dependency_overrides[get_db] = session
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


def test_current_orders_link_history_by_symbol_and_order_id(monkeypatch, tmp_path):
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
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with factory() as session:
                session.add(build_account(portfolio_id=None))
                # 同号的其他品种更晚，且同品种的旧执行不能覆盖最新关联。
                for execution, symbol in (("old-rb", "rb2610"), ("new-rb", "rb2610"), ("ag", "ag2612")):
                    session.add(
                        ExecutionEvent(
                            account_id=1,
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
                    )
                await session.commit()
            async with factory() as session:
                result = await query(build_account())
                from axile.server.account_orders import _link_executions

                monkeypatch.setattr("axile.server.core.db.SessionLocal", factory)
                await _link_executions(build_account(), result)
                assert {row.symbol: row.execution_id for row in result.data} == {
                    "rb2610": "new-rb",
                    "ag2612": "ag",
                    "cu2612": None,
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())
