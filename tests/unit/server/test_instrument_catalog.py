"""目录后台 API 与日夜盘预热边界。"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.instrument_catalog import CatalogError, InstrumentCatalog
from axile.server.api.routes import instrument_catalog as routes
from axile.server.execution import catalog_preheat
from axile.server.trading_calendar import CalendarDecisionStatus
from tests.unit.server._execution_test_support import build_account


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


def test_exact_query_uses_detached_account_snapshot(monkeypatch):
    account = build_account().snapshot()
    calls: list[object] = []

    async def load(account_id: int):
        assert account_id == account.id
        return account

    async def query(actual_account, symbols, **kwargs):
        calls.append((actual_account, symbols, kwargs))
        return {"SHFE.rb2610": {"symbol": "SHFE.rb2610"}}

    monkeypatch.setattr(routes, "get_account_snapshot", load)
    monkeypatch.setattr(routes, "get_worker_backend_manager", lambda: SimpleNamespace(query_instruments=query))
    app = FastAPI()
    app.include_router(routes.router)
    response = TestClient(app).post(
        "/instrument-catalog/query", json={"account_id": account.id, "symbols": ["SHFE.rb2610"]}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert calls == [(account, ["SHFE.rb2610"], {"timeout": 5, "require_specs": False, "use_cache": True})]


@pytest.mark.parametrize("missing", [False, True])
def test_exact_query_preserves_structured_error(monkeypatch, missing):
    async def load(_account_id: int):
        return None if missing else build_account().snapshot()

    async def query(*_args, **_kwargs):
        raise CatalogError("合约不存在", "CATALOG.NOT_FOUND", causes=[{"symbol": "missing"}])

    monkeypatch.setattr(routes, "get_account_snapshot", load)
    monkeypatch.setattr(routes, "get_worker_backend_manager", lambda: SimpleNamespace(query_instruments=query))
    app = FastAPI()
    app.include_router(routes.router)
    response = TestClient(app).post("/instrument-catalog/query", json={"account_id": 1, "symbols": ["missing"]})
    assert response.status_code == 404
    assert response.json()["detail"] == (
        {"code": "ACCOUNT_NOT_FOUND", "message": "账户不存在"}
        if missing
        else {"code": "CATALOG.NOT_FOUND", "message": "合约不存在", "causes": [{"symbol": "missing"}]}
    )
