"""账户 dashboard 聚合路由测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud
from axile.server.business_facts.assets import write_snapshot_assets
from axile.server.db.models import AccountAssetSnapshot
from tests.unit.server._business_analysis_support import analysis_record, persist_records
from tests.unit.server._execution_test_support import build_account


class _Job:
    def __init__(self, next_run_time: datetime | None) -> None:
        self.next_run_time = next_run_time


class _Scheduler:
    def __init__(self, job: _Job | None = None) -> None:
        self._job = job

    def get_job(self, _job_id: str) -> _Job | None:
        return self._job


def _build_app(sessions: async_sessionmaker, scheduler: _Scheduler) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: scheduler
    return app


def test_dashboard_counts_off_symbols_zero_when_lots_already_match(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """手数已按渠道量化到位（含 TA2701→TA701）时 off_symbol_count 为 0。"""
    account = build_account(id=1, name="acc", is_started=True)
    equity = 992_670.6124999999
    assets = {
        "total_asset": equity,
        "positions": [
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
    }
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dashboard.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(account)
            await session.flush()
            snapshot = AccountAssetSnapshot(id=1, account_id=1, source="manual", created_at="2026-08-26T21:35:11")
            session.add(snapshot)
            await session.flush()
            await write_snapshot_assets(session, snapshot, assets)
            await persist_records(
                session,
                [
                    analysis_record(
                        id=1,
                        account_id=1,
                        execution_id="dashboard-exec",
                        input={},
                        result={"status": "SUCCEEDED"},
                        is_success=1,
                        created_at="2026-08-26T21:35:11",
                    )
                ],
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())

    async def _bindings(_session: object) -> dict[int, int]:
        return {1: 7}

    async def _no_baseline_records(*_args: object, **_kwargs: object) -> dict[int, list[object]]:
        return {}

    async def _targets(_session: object, _pairs: object) -> dict[int, object]:
        return {
            1: SimpleNamespace(
                normalized_weights={"TA2701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13},
            )
        }

    monkeypatch.setattr(account_crud, "get_portfolios_every_account", _bindings)
    monkeypatch.setattr(account_crud, "read_performance_summaries", _no_baseline_records)
    monkeypatch.setattr(account_crud, "get_latest_account_target_snapshots_for_accounts", _targets)

    app = _build_app(sessions, _Scheduler(_Job(None)))
    try:
        item = TestClient(app).get("/account/dashboard").json()["data"][0]
        assert item["off_symbol_count"] == 0
        assert item["holdings_count"] == 4
        assert item["total_asset"] == equity
        assert item["last_output_status"] == "SUCCEEDED"
    finally:
        asyncio.run(engine.dispose())
