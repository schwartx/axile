"""固定公开字段、默认值、零和空值，不由响应模型生成预期。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import account_execution
from axile.server.target_weight_snapshots import target_snapshot_public


def test_empty_target_is_unknown_with_explicit_nulls():
    assert target_snapshot_public(None, weight_kind="raw") == {
        "weights": {},
        "target_mode": "weight",
        "raw_targets": {},
        "quantities": None,
        "strategy_weights": None,
        "account_weights": None,
        "sizing": None,
        "calculated_at": None,
        "source": None,
        "execution_id": None,
        "context_account_id": None,
    }


def test_sizing_filters_internal_fields_and_preserves_zero_and_bad_history():
    rows = account_execution._sizing_rows_from_facts(
        {
            "zero": {"reason_code": "COMMON.SIZING.EXACT", "target_quantity": 0, "internal_secret": "hidden"},
            "missing": {"target_quantity": 1},
            "bad": {"reason_code": "COMMON.SIZING.EXACT", "target_quantity": "invalid"},
            "overflow": {"reason_code": "COMMON.SIZING.EXACT", "target_quantity": 10**1000},
        },
        canonicalize=str,
        strategy_weights={"zero": 0.0},
        account_weights={"zero": 0.0},
        target_context={},
    )
    assert rows == {
        "zero": {
            "symbol": "zero",
            "sizing_mode": "weight",
            "status": "SIZED",
            "reason_code": "COMMON.SIZING.EXACT",
            "strategy_weight": 0.0,
            "account_weight": 0.0,
            "account_multiplier": None,
            "weight_precision": None,
            "equity": None,
            "reference_price": None,
            "unit_multiplier": None,
            "unit_notional": None,
            "target_notional": None,
            "raw_quantity": None,
            "target_quantity": 0.0,
            "current_quantity": None,
            "quantity_step": None,
            "min_quantity": None,
            "min_notional": None,
            "value_conversion": None,
        }
    }
    assert isinstance(rows["zero"]["target_quantity"], float)


def test_execution_status_keeps_defaults_and_filters_internal_state(monkeypatch):
    async def status(_):
        return {
            "execution_id": "exec-1",
            "account_id": 1,
            "status": "queued",
            "created_at": "2026-11-12T10:00:00+08:00",
            "is_success": 0,
            "private": "hidden",
        }

    monkeypatch.setattr(account_execution, "get_execution_status", status)
    app = FastAPI()
    app.include_router(account_execution.router)
    response = TestClient(app).get("/executions/exec-1")
    assert response.status_code == 200
    assert response.json() == {
        "execution_id": "exec-1",
        "account_id": 1,
        "execution_kind": None,
        "status": "queued",
        "created_at": "2026-11-12T10:00:00+08:00",
        "started_at": None,
        "finished_at": None,
        "error": None,
        "output_status": None,
        "outcome": None,
        "outcome_reason": None,
        "outcome_symbols": [],
        "record_id": None,
        "is_success": 0,
        "cancel_requested_at": None,
        "cancel_reason": None,
        "terminate_mode": None,
    }
