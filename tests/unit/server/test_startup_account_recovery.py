"""服务生命周期与账户连接解耦，不接真实柜台或启动实际交易任务。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from axile.server import app as server_app
from axile.server.account_control import monitor as control_monitor
from axile.server.api.routes import utils
from axile.server.connection_validation import service as validation_service
from axile.server.execution import notification_outbox, supplements


@pytest.fixture
def isolated_lifespan(monkeypatch):
    monkeypatch.setattr(server_app, "ensure_single_worker", lambda: None)
    monkeypatch.setattr(server_app, "is_configured", lambda: True)
    monkeypatch.setattr(server_app.live_hub, "bind_loop", lambda _loop: None)
    monkeypatch.setattr(control_monitor, "recover_sessions", AsyncMock())
    monkeypatch.setattr(control_monitor, "monitor", MagicMock(close=AsyncMock()))
    monkeypatch.setattr(server_app, "_load_configured_algorithm_directories", lambda: None)
    monkeypatch.setattr(server_app, "_load_configured_algorithm_modules", lambda: None)
    monkeypatch.setattr(server_app, "register_china_channel_jobs", lambda _scheduler: None)
    monkeypatch.setattr(server_app, "scheduler", MagicMock())
    monkeypatch.setattr(supplements, "recover_supplements", AsyncMock())
    monkeypatch.setattr(server_app, "recover_intents_on_startup", AsyncMock())
    monkeypatch.setattr(notification_outbox, "recover_notifications", AsyncMock())
    monkeypatch.setattr(server_app, "AnalysisManager", lambda _factory: MagicMock(start=AsyncMock(), stop=AsyncMock()))
    monkeypatch.setattr(validation_service, "close", AsyncMock())
    monkeypatch.setattr(server_app, "shutdown_dispatchers", AsyncMock())
    monkeypatch.setattr(server_app, "shutdown_worker_backend_manager", MagicMock())


def test_api_ready_while_account_connection_is_blocked_and_shutdown_cancels_recovery(isolated_lifespan, monkeypatch):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def recover():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(server_app, "init_scheduler", recover)
        application = FastAPI()
        application.include_router(utils.router, prefix="/api/v1")
        lifespan = server_app.lifespan(application)
        await asyncio.wait_for(lifespan.__aenter__(), 1)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not application.state.account_recovery.done()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                response = await asyncio.wait_for(client.get("/api/v1/utils/health-check/"), 1)
                assert response.status_code == 200 and response.json() is True
        finally:
            await asyncio.wait_for(lifespan.__aexit__(None, None, None), 1)
        assert cancelled.is_set()
        assert application.state.account_recovery.done()

    asyncio.run(scenario())


def test_account_recovery_failure_does_not_close_api(isolated_lifespan, monkeypatch):
    async def recover():
        raise RuntimeError("账户恢复失败")

    async def scenario():
        monkeypatch.setattr(server_app, "init_scheduler", recover)
        application = FastAPI()
        application.include_router(utils.router, prefix="/api/v1")
        async with server_app.lifespan(application):
            await asyncio.wait_for(application.state.account_recovery, 1)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/utils/health-check/")
                assert response.status_code == 200 and response.json() is True

    asyncio.run(scenario())
