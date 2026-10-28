"""仪表盘一次读取原生快照后组合渠道估值与实时进度。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account_crud
from tests.unit.server.test_account_control_routes import _build_account


class _Scheduler:
    def get_job(self, _id):
        return None


def _client(monkeypatch, assets, *, target=None):
    context = {
        "accounts": [_build_account().model_dump(mode="json") | {"connection": {"status": "uninitialized"}}],
        "inputs": {
            "1": {
                "observation": {"assets": assets, "created_at": "2026-08-26T21:35:11"} if assets is not None else None,
                "latest": {"display_status": "SUCCEEDED", "is_success": 1, "created_at": "2026-08-26T21:35:11"},
                "performance": {
                    "snapshot_id": None,
                    "status": "empty",
                    "computed_at": None,
                    "observed_at": None,
                    "account_equity": None,
                    "account_daily_return": None,
                    "points": [],
                },
                "target_weights": target,
                "portfolio_id": 7,
            }
        },
    }
    operation = AsyncMock(return_value=context)
    monkeypatch.setattr(account_crud, "native_operation", operation)
    app = FastAPI()
    app.include_router(account_crud.router, prefix="/account")
    app.dependency_overrides[get_scheduler] = _Scheduler
    return TestClient(app), operation


def test_dashboard_counts_off_symbols_zero_when_lots_already_match(monkeypatch):
    equity = 992_670.6124999999
    positions = [
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
    ]
    client, operation = _client(
        monkeypatch,
        {"total_asset": equity, "positions": positions},
        target={"TA2701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13},
    )
    response = client.get("/account/dashboard")
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]
    assert item["off_symbol_count"] == 0
    assert item["holdings_count"] == 4
    assert item["total_asset"] == equity
    assert item["last_output_status"] == "SUCCEEDED"
    operation.assert_awaited_once_with(
        "account_dashboard_context",
        {
            "live_sessions": account_crud.public_session_views(),
        },
    )


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
def test_dashboard_empty_zero_and_top_twelve(monkeypatch, assets):
    client, _ = _client(monkeypatch, assets)
    monkeypatch.setattr(
        account_crud.live_hub,
        "progress_for",
        lambda _: {
            "status": "running",
            "execution_id": "live",
            "kind": "rebalance",
            "phase": "submitting",
        },
    )
    monkeypatch.setattr(account_crud, "get_running_execution_id", lambda _: "registry")
    response = client.get("/account/dashboard")
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]
    assert item["off_symbol_count"] is None
    assert item["running_execution_id"] == "live" and item["running_phase"] == "submitting"
    assert item["currency"] == "CNY" and item["performance"]["status"] == "empty"
    assert item["holdings_count"] == (None if assets is None else 14 if assets["positions"] else 0)
    assert item["position_weights"] == (list(range(14, 2, -1)) if assets and assets["positions"] else [])
    assert (item["asset_observed_at"] is None) == (assets is None)
    assert "assets" not in item and "target_weights" not in item


@pytest.mark.parametrize("target", [{}, {"rb2610": 0.1}], ids=["flat-target", "open-target"])
@pytest.mark.parametrize(
    "assets, known",
    [
        ({"total_asset": 100}, False),
        ({"total_asset": 100, "positions": None}, False),
        ({"total_asset": 100, "positions": "invalid"}, False),
        ({"total_asset": 100, "positions": []}, True),
    ],
    ids=["missing", "null", "invalid", "flat"],
)
def test_dashboard_distinguishes_unknown_positions_from_flat(monkeypatch, target, assets, known):
    client, _ = _client(monkeypatch, assets, target=target)
    response = client.get("/account/dashboard")
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]
    assert item["holdings_count"] == (0 if known else None)
    assert item["off_symbol_count"] == (len(target) if known else None)
    assert item["position_weights"] == []
    assert item["asset_observed_at"] == "2026-08-26T21:35:11"
