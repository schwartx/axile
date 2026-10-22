"""账户 dashboard 聚合路由测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server._native import _core
from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud
from axile.server.business_facts.assets import write_snapshot_assets
from axile.server.db.models import AccountAssetSnapshot, PortfolioAccount, TargetWeightSnapshot
from axile.server.db.models.target_weight_snapshot import target_weight_snapshot_weight
from axile.server.target_weight_snapshots import encode_snapshot_weights
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
            session.add(PortfolioAccount(account_id=1, portfolio_id=7))
            columns, rows = encode_snapshot_weights(
                1, {"normalized_weights": {"TA2701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13}}
            )
            session.add(TargetWeightSnapshot(id=1, account_id=1, portfolio_id=7, source="manual", **columns))
            await session.flush()
            await session.execute(target_weight_snapshot_weight.insert(), rows[target_weight_snapshot_weight])
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())

    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(tmp_path / "dashboard.db")))

    app = _build_app(sessions, _Scheduler(_Job(None)))
    try:
        item = TestClient(app).get("/account/dashboard").json()["data"][0]
        assert item["off_symbol_count"] == 0
        assert item["holdings_count"] == 4
        assert item["total_asset"] == equity
        assert item["last_output_status"] == "SUCCEEDED"
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.parametrize(
    "assets",
    [
        None,
        {"total_asset": 0, "positions": []},
        {
            "total_asset": 1000,
            "positions": [{"symbol": str(i), "volume": 1, "market_value": -i} for i in range(1, 15)],
        },
    ],
)
def test_dashboard_empty_zero_and_top_twelve(tmp_path, monkeypatch, assets):
    database = tmp_path / "cards.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(build_account(id=1))
            await session.flush()
            if assets is not None:
                await write_snapshot_assets(session, AccountAssetSnapshot(account_id=1, source="manual"), assets)
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(database)))
    monkeypatch.setattr(
        account_crud.live_hub,
        "progress_for",
        lambda _: {"status": "running", "execution_id": "live", "kind": "rebalance", "phase": "submitting"},
    )
    monkeypatch.setattr(account_crud, "get_running_execution_id", lambda _: "registry")
    app = _build_app(sessions, _Scheduler())
    try:
        response = TestClient(app).get("/account/dashboard")
        assert response.status_code == 200, response.text
        item = response.json()["data"][0]
        assert item["off_symbol_count"] is None
        assert item["running_execution_id"] == "live"
        assert item["running_phase"] == "submitting"
        assert item["currency"] == "CNY"
        assert item["performance"]["status"] == "empty"
        assert item["holdings_count"] == (14 if assets and assets["positions"] else 0)
        assert item["position_weights"] == (list(range(14, 2, -1)) if assets and assets["positions"] else [])
        assert (item["asset_observed_at"] is None) == (assets is None)
        assert "assets" not in item and "target_weights" not in item
    finally:
        asyncio.run(engine.dispose())
