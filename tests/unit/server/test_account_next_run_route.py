"""账户未来排程使用一次原生上下文和真实 scheduler trigger。"""

from datetime import datetime
from unittest.mock import AsyncMock

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account_crud
from tests.unit.server.test_account_control_routes import _build_account


class _Job:
    def __init__(self, next_run_time, trigger):
        self.next_run_time = next_run_time
        self.trigger = trigger


class _Scheduler:
    def __init__(self, job=None):
        self.job = job

    def get_job(self, _job_id):
        return self.job


def test_next_run_time_returns_three_real_trigger_times(monkeypatch) -> None:
    trigger = CronTrigger(hour=9, minute=3, timezone="Asia/Shanghai")
    next_run = datetime.fromisoformat("2026-07-02T09:03:00+08:00")
    operation = AsyncMock(
        return_value={"account": _build_account().model_dump(mode="json"), "pending_scheduled_at": []}
    )
    monkeypatch.setattr(account_crud, "native_operation", operation)
    app = FastAPI()
    app.include_router(account_crud.router, prefix="/account")
    app.dependency_overrides[get_scheduler] = lambda: _Scheduler(_Job(next_run, trigger))
    response = TestClient(app).get("/account/1/next_run_time")
    assert response.status_code == 200
    assert response.json() == {
        "account_id": 1,
        "is_scheduled": True,
        "next_run_time": next_run.isoformat(),
        "next_run_times": ["2026-07-02T09:03:00+08:00", "2026-07-03T09:03:00+08:00", "2026-07-04T09:03:00+08:00"],
        "next_execution_times": ["2026-07-02T09:03:00+08:00", "2026-07-03T09:03:00+08:00", "2026-07-06T09:03:00+08:00"],
    }
    operation.assert_awaited_once_with("account_next_run_context", {"account_id": 1})
