"""账户 next_run_time 路由测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.db.models import Account
from tests.unit.server._execution_test_support import build_account


class _RouteSession:
    def __init__(self, account: Account | None = None) -> None:
        self.account = account

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account is not None and self.account.id == account_id:
            return self.account
        return None


class _Job:
    def __init__(self, next_run_time: datetime | None, trigger: object | None = None) -> None:
        self.next_run_time = next_run_time
        self.trigger = trigger


class _Scheduler:
    def __init__(self, job: _Job | None = None) -> None:
        self._job = job

    def get_job(self, _job_id: str) -> _Job | None:
        return self._job


def _build_app(session: _RouteSession, scheduler: _Scheduler) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[_RouteSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: scheduler
    return app


def test_next_run_time_returns_iso_when_job_scheduled() -> None:
    """存在调度任务时返回未来三次，并保留单值字段兼容调用方。"""
    timezone = "Asia/Shanghai"
    trigger = CronTrigger(hour=9, minute=3, timezone=timezone)
    next_run = datetime.fromisoformat("2026-07-02T09:03:00+08:00")
    app = _build_app(_RouteSession(build_account(id=1)), _Scheduler(_Job(next_run, trigger)))

    response = TestClient(app).get("/account/1/next_run_time")

    assert response.status_code == 200
    assert response.json() == {
        "account_id": 1,
        "is_scheduled": True,
        "next_run_time": next_run.isoformat(),
        "next_run_times": [
            "2026-07-02T09:03:00+08:00",
            "2026-07-03T09:03:00+08:00",
            "2026-07-04T09:03:00+08:00",
        ],
        "next_execution_times": [
            "2026-07-02T09:03:00+08:00",
            "2026-07-03T09:03:00+08:00",
            "2026-07-06T09:03:00+08:00",
        ],
    }
