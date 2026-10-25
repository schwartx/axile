"""调仓预览保留未知持仓与明确空仓的区别。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import account_execution
from axile.server.db.models.account import AccountSnapshot
from tests.unit.server.test_account_control_routes import _build_account


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
def test_rebalance_plan_distinguishes_unknown_positions_from_flat(monkeypatch, target, assets, known):
    monkeypatch.setattr(
        account_execution,
        "_get_account_or_404",
        AsyncMock(return_value=AccountSnapshot.model_validate(_build_account().model_dump())),
    )
    monkeypatch.setattr(account_execution, "get_latest_portfolio_id_by_account_id", AsyncMock(return_value=7))
    monkeypatch.setattr(
        account_execution,
        "get_latest_account_target_snapshot",
        AsyncMock(return_value={"normalized_weights": target, "calculated_at": "2026-08-26T21:35:10"}),
    )
    monkeypatch.setattr(
        account_execution,
        "latest_asset_snapshot",
        AsyncMock(return_value={"assets": assets, "created_at": "2026-08-26T21:35:11"}),
    )
    app = FastAPI()
    app.include_router(account_execution.router, prefix="/account")
    response = TestClient(app).get("/account/1/rebalance_plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["off_symbol_count"] == (len(target) if known else None)
    if not known:
        assert plan["rows"] == []
    assert plan["observed_at"] == "2026-08-26T21:35:11"
    assert plan["target_calculated_at"] == "2026-08-26T21:35:10"
