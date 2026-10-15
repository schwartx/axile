"""账户 dashboard 聚合路由测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud
from tests.unit.server._execution_test_support import build_account


class _Result:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        return self._items


class _Session:
    def __init__(self, accounts: list[object]) -> None:
        self._accounts = accounts

    async def execute(self, _stmt: object) -> _Result:
        return _Result(self._accounts)


class _Job:
    def __init__(self, next_run_time: datetime | None) -> None:
        self.next_run_time = next_run_time


class _Scheduler:
    def __init__(self, job: _Job | None = None) -> None:
        self._job = job

    def get_job(self, _job_id: str) -> _Job | None:
        return self._job


def _build_app(session: _Session, scheduler: _Scheduler) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[_Session, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: scheduler
    return app


def _snapshot(
    total_asset: float,
    positions: list[dict[str, object]],
    created_at: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        assets={"total_asset": total_asset, "positions": positions},
        created_at=created_at,
    )


def test_dashboard_counts_off_symbols_zero_when_lots_already_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """手数已按渠道量化到位（含 TA2701→TA701）时 off_symbol_count 为 0。"""
    account = build_account(id=1, name="acc", is_started=True)
    equity = 992_670.6124999999
    snapshots = [
        _snapshot(
            equity,
            [
                {
                    "symbol": "c2611",
                    "volume": 1.0,
                    "market_value": 22800.0,
                    "direction": "多头",
                    "extra": {"net_position": 1.0},
                },
                {
                    "symbol": "rb2610",
                    "volume": 4.0,
                    "market_value": 123100.0,
                    "direction": "多头",
                    "extra": {"net_position": 4.0},
                },
                {
                    "symbol": "TA701",
                    "volume": 2.0,
                    "market_value": 55000.0,
                    "direction": "空头",
                    "extra": {"net_position": -2.0},
                },
                {
                    "symbol": "m2701",
                    "volume": 1.0,
                    "market_value": 32960.0,
                    "direction": "空头",
                    "extra": {"net_position": -1.0},
                },
            ],
            "2026-08-26T21:35:11",
        )
    ]
    records = [
        SimpleNamespace(
            raw_result={"status": "SUCCEEDED"},
            raw_input={},
            is_success=1,
            created_at="2026-08-26T21:35:11",
        )
    ]

    async def _bindings(_session: object) -> dict[int, int]:
        return {1: 7}

    async def _recent(_session: object, _account_ids: object, limit: int = 20) -> dict[int, list[object]]:
        return {1: list(records)}

    async def _snapshots(_session: object, _account_ids: object, limit: int = 20) -> dict[int, list[object]]:
        return {1: list(snapshots)}

    async def _no_baseline_records(*_args: object, **_kwargs: object) -> dict[int, list[object]]:
        return {}

    async def _targets(_session: object, _pairs: object) -> dict[int, object]:
        return {
            1: SimpleNamespace(
                normalized_weights={"TA2701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13},
            )
        }

    monkeypatch.setattr(account_crud, "get_portfolios_every_account", _bindings)
    monkeypatch.setattr(account_crud, "get_recent_execute_records_for_accounts", _recent)
    monkeypatch.setattr(account_crud, "get_recent_account_asset_snapshots_for_accounts", _snapshots)
    monkeypatch.setattr(account_crud, "read_performance_summaries", _no_baseline_records)
    monkeypatch.setattr(account_crud, "get_latest_account_target_snapshots_for_accounts", _targets)

    app = _build_app(_Session([account]), _Scheduler(_Job(None)))
    item = TestClient(app).get("/account/dashboard").json()["data"][0]
    assert item["off_symbol_count"] == 0
