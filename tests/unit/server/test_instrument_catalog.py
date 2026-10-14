"""目录后台 API 与日夜盘预热边界。"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.instrument_catalog import InstrumentCatalog
from axile.server.api.routes import instrument_catalog as routes
from axile.server.execution import catalog_preheat
from axile.server.trading_calendar import CalendarDecisionStatus


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("08:29:59", None),
        ("08:30:00", "day"),
        ("08:45:00", "day"),
        ("08:57:00", "day"),
        ("08:57:01", None),
        ("08:58:00", None),
        ("09:00:00", None),
        ("12:00:00", None),
        ("20:30:00", "night"),
        ("20:57:00", "night"),
        ("20:57:01", None),
        ("21:00:00", None),
    ],
)
def test_startup_preheat_window_includes_exact_three_minute_boundary(monkeypatch, clock, expected):
    monkeypatch.setattr(
        catalog_preheat,
        "evaluate_channel_calendar_moment",
        lambda *a: SimpleNamespace(status=CalendarDecisionStatus.AVAILABLE_OPEN),
    )
    now = datetime.fromisoformat(f"2026-10-13T{clock}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    result = catalog_preheat.preheat_window(now)
    assert (result[0].split(":")[-1] if result else None) == expected


@pytest.mark.parametrize("status", [CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE])
def test_calendar_closed_or_unavailable_skips_automatic_full_refresh(monkeypatch, status):
    monkeypatch.setattr(catalog_preheat, "evaluate_channel_calendar_moment", lambda *a: SimpleNamespace(status=status))
    assert catalog_preheat.preheat_window(datetime(2026, 10, 13, 20, 45)) is None


def test_catalog_api_is_async_and_does_not_wait_for_account_login(monkeypatch):
    catalog = InstrumentCatalog()
    monkeypatch.setattr(routes, "get_worker_backend_manager", lambda: SimpleNamespace(instrument_catalog=catalog))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/instrument-catalog").json()["records"] == 0
        response = client.post("/instrument-catalog/refresh")
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert client.get(f"/instrument-catalog/refresh/{task_id}").status_code == 200
        assert client.get("/instrument-catalog/refresh/unknown").status_code == 404
        assert "version" not in client.get("/instrument-catalog").json()
