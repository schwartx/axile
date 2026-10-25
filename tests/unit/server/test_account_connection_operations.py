"""账户连接操作与交易任务、定时配置的边界。"""

import asyncio

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from axile.executor.connection_state import ConnectionObservation
from axile.server.api.routes import account_connection as routes
from axile.server.connection_runtime import observe_session, register_session
from axile.server.connection_state import ConnectionBinding, connection_config_key
from axile.server.db.models.account_connection import AccountConnectionState
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import connection_view


def client(account, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")

    async def get_account(account_id):
        if account.id != account_id:
            raise routes.HTTPException(404, "账户不存在")
        return account

    async def connection_response(account_id):
        current = await get_account(account_id)
        return Response(connection_view(current, detail=True).model_dump_json(), media_type="application/json")

    monkeypatch.setattr(routes, "account_connection", connection_response)
    monkeypatch.setattr(routes, "_get_account_or_404", get_account)
    return TestClient(app)


@pytest.mark.parametrize("action", ["reconnect", "disconnect"])
def test_connection_operations_do_not_change_schedule_or_execute_trades(monkeypatch, action):
    account = build_account(id=7, is_started=True)
    calls = []

    async def operate(snapshot, *, reconnect):
        calls.append((snapshot.id, reconnect))
        binding = ConnectionBinding(7, connection_config_key(account.trade_channel, account.account_config), "test")
        register_session(binding, lambda: True)
        observe_session(binding, 1, ConnectionObservation("ok" if reconnect else "disconnected", "now", kind="session"))
        account.connection_state = AccountConnectionState(
            account_id=7,
            config_key=connection_config_key(account.trade_channel, account.account_config),
            status="ok" if reconnect else "disconnected",
        )

    monkeypatch.setattr(routes, "_operate_connection", operate)
    response = client(account, monkeypatch).post(f"/account/7/connection/{action}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ("ready" if action == "reconnect" else "disconnected")
    assert account.is_started is True
    assert calls == [(7, action == "reconnect")]


@pytest.mark.parametrize("action", ["reconnect", "disconnect"])
@pytest.mark.parametrize("kind", ["running", "queued"])
def test_connection_operations_reject_active_or_queued_execution(monkeypatch, action, kind):
    account = build_account(id=7)
    monkeypatch.setattr(routes, f"get_{kind}_execution_id", lambda _: "active")
    monkeypatch.setattr(routes, "_operate_connection", lambda *args, **kwargs: pytest.fail("不应操作连接"))
    response = client(account, monkeypatch).post(f"/account/7/connection/{action}")
    assert response.status_code == 409


@pytest.mark.parametrize("status", ["connecting", "recovering", "cooldown", "disconnected"])
def test_resident_status_roundtrips_without_migration(status):
    account = build_account(id=7)
    observed = ConnectionObservation(status, "2026-10-15T10:00:00", label="运行时状态")
    account.connection_state = AccountConnectionState(
        account_id=7,
        config_key=connection_config_key(account.trade_channel, account.account_config),
        status=observed.status,
        observed_at=observed.observed_at,
        label=observed.label,
    )
    binding = ConnectionBinding(7, account.connection_state.config_key, "test")
    register_session(binding, lambda: True)
    observe_session(binding, 1, observed)
    detail = connection_view(account, detail=True)
    assert detail.status == ("unavailable" if status == "cooldown" else status)
    assert detail.label == ("运行时状态" if status == "cooldown" else None)


def test_process_reconnect_forwards_force_without_execution_id(monkeypatch):
    calls = []

    class Manager:
        async def prepare_account(self, account, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(routes, "get_worker_backend_manager", Manager)
    asyncio.run(routes._operate_connection(build_account(id=7), reconnect=True))
    assert calls == [{"force_reconnect": True}]


def test_concurrent_failed_reconnects_share_one_attempt(monkeypatch):
    account = build_account(id=7)
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def operate(snapshot, *, reconnect):
            calls.append(snapshot.id)
            entered.set()
            await release.wait()
            raise TimeoutError("startup deadline")

        async def get_account(account_id):
            assert account_id == account.id
            return account

        monkeypatch.setattr(routes, "_get_account_or_404", get_account)
        monkeypatch.setattr(routes, "_operate_connection", operate)
        first = asyncio.create_task(routes.reconnect_account(7))
        await entered.wait()
        second = asyncio.create_task(routes.reconnect_account(7))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, routes.HTTPException) and result.status_code == 502 for result in results)
        assert calls == [7]
        assert not routes._reconnect_tasks

    asyncio.run(scenario())
