"""调仓预览保留未知持仓与明确空仓的区别。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.serialization import from_payload, to_payload
from axile.server import target_sizing
from axile.server.api.routes import account_execution
from axile.server.db.models.account import AccountSnapshot
from tests.unit.server.test_account_control_routes import _build_account


def _client(monkeypatch, target, assets, *, quantities, target_mode="weight"):
    snapshot = {
        "id": 2,
        "account_id": 1,
        "portfolio_id": 7,
        "raw_weights": target,
        "normalized_weights": target,
        "target_mode": target_mode,
        "source": "execution",
        "execution_id": "frozen",
        "calculated_at": "2026-08-26T21:35:10",
    }
    monkeypatch.setattr(
        account_execution,
        "_get_account_or_404",
        AsyncMock(return_value=from_payload(AccountSnapshot, to_payload(_build_account()))),
    )
    monkeypatch.setattr(account_execution, "get_latest_portfolio_id_by_account_id", AsyncMock(return_value=7))
    latest = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(account_execution, "get_latest_account_target_snapshot", latest)
    native = AsyncMock(
        return_value={
            "1": {
                "snapshot": snapshot,
                "previous": [],
                "executions": {
                    "frozen": {
                        "artifact_versions": {"target_snapshot": 2},
                        "record_id": 1 if quantities is not None else None,
                        "input_id": "input:frozen",
                        "target_context": {},
                        "sizings": {
                            symbol: {
                                "sizing_mode": target_mode,
                                "reason_code": "COMMON.SIZING.QUANTIZED",
                                "target_quantity": quantity,
                            }
                            for symbol, quantity in (quantities or {}).items()
                        },
                    }
                },
            }
        }
    )
    monkeypatch.setattr(target_sizing, "native_operation", native)
    monkeypatch.setattr(
        account_execution,
        "latest_asset_snapshot",
        AsyncMock(return_value={"assets": assets, "created_at": "2026-08-26T21:35:11"}),
    )
    app = FastAPI()
    app.include_router(account_execution.router, prefix="/account")
    return TestClient(app), latest, native


@pytest.mark.parametrize("target_mode", ["weight", "quantity"])
@pytest.mark.parametrize("available", [False, True])
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
def test_rebalance_plan_distinguishes_unknown_positions_from_flat(
    monkeypatch, target, assets, known, available, target_mode
):
    client, latest, native = _client(
        monkeypatch,
        target,
        assets,
        quantities={symbol: 1 for symbol in target} if available else None,
        target_mode=target_mode,
    )
    response = client.get("/account/1/rebalance_plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["off_symbol_count"] == (len(target) if known and available else None)
    if not known or not available:
        assert plan["rows"] == []
    latest.assert_awaited_once_with(1, 7)
    if known:
        native.assert_awaited_once_with(
            "target_operation",
            {"op": "account_evidence", "pairs": [{"account_id": 1, "portfolio_id": 7, "snapshot_id": 2}]},
        )
    else:
        native.assert_not_awaited()
    assert plan["target_mode"] == target_mode
    assert plan["observed_at"] == "2026-08-26T21:35:11"
    assert plan["target_calculated_at"] == "2026-08-26T21:35:10"


def test_quantity_rebalance_plan_uses_frozen_rounded_quantity(monkeypatch):
    client, _, _ = _client(
        monkeypatch,
        {"XAUUSD": 0.015},
        {"positions": [{"symbol": "XAUUSD", "volume": 0.02, "direction": "多头"}]},
        quantities={"XAUUSD": 0.02},
        target_mode="quantity",
    )
    response = client.get("/account/1/rebalance_plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["off_symbol_count"] == 0
    assert plan["rows"][0]["target_quantity"] == 0.02
    assert plan["rows"][0]["action"] == "aligned"
