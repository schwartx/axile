"""数量目标在函数、执行输入和真实存储边界保持同一口径。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.server._native import _core
from axile.server.api.routes import account_execution
from axile.server.context import Context
from axile.server.execution import rebalance
from axile.server.integrity import plan_executable_target
from axile.server.portfolio_function import calculate_portfolio_target
from axile.server.portfolio_runner import calculate_sample_portfolio
from axile.server.target_weight_snapshots import target_snapshot_public
from tests.unit.server._execution_test_support import build_account

CODE = "def calculate_portfolio(context):\n    return {'mode': 'quantity', 'targets': {'XAUUSD': 0.015, 'XAGUSD': -0.25}}\n"


@pytest.mark.parametrize("sample", [False, True])
def test_quantity_function_preserves_signed_fractional_targets(sample):
    target = calculate_sample_portfolio(CODE) if sample else calculate_portfolio_target(CODE, Context())
    assert target.mode == "quantity"
    assert target.targets == {"XAUUSD": 0.015, "XAGUSD": -0.25}


@pytest.mark.parametrize("value", [True, "0.015", float("nan"), float("inf")])
def test_quantity_values_are_strict(value):
    with pytest.raises((TypeError, ValueError)):
        PortfolioTarget.from_result({"mode": "quantity", "targets": {"XAUUSD": value}})


@pytest.mark.parametrize(
    "result",
    [
        {"mode": "quantity"},
        {"mode": "unknown", "targets": {}},
        {"targets": {}},
        {"mode": "quantity", "targets": {}, "extra": 1},
    ],
)
def test_quantity_envelope_cannot_silently_become_empty_weights(result):
    with pytest.raises((TypeError, ValueError)):
        PortfolioTarget.from_result(result)


def test_legacy_dictionary_still_means_weight():
    assert PortfolioTarget.from_result({"XAUUSD": 1}).model_dump() == {"mode": "weight", "targets": {"XAUUSD": 1.0}}


def test_quantity_bypasses_leverage_and_survives_standard_input_roundtrip():
    account = build_account(long_leverage=5, short_leverage=3, weight_precision=0.1)
    targets = {"XAUUSD": 0.015, "XAGUSD": -0.25}
    normalized = rebalance._normalize_rebalance_target(account, targets, target_mode="quantity")
    assert normalized == targets and normalized is not targets
    inputs = rebalance._build_rebalance_standard_input(
        account=account,
        curr_target=normalized,
        last_target={},
        execution_id="quantity",
        trigger_source="manual",
        target_mode="quantity",
    )
    restored = UnifiedStandardInput.from_dict(inputs.to_dict())
    assert restored.target_mode == "quantity" and restored.curr_target == targets
    assert "target_mode" not in restored.extra


@pytest.mark.parametrize("equity", [0.0, -100.0, 1_000_000.0])
def test_quantity_alignment_needs_positions_and_not_equity(equity):
    positions = [{"symbol": "XAUUSD", "volume": 0.01, "direction": "多头"}]
    plan = plan_executable_target(positions, {"XAUUSD": 0.015}, equity, target_mode="quantity")
    assert plan.off_symbol_count == 1
    assert plan.rows[0].current_quantity == 0.01 and plan.rows[0].target_quantity == 0.015
    assert plan.rows[0].action == "increase" and plan.rows[0].target_weight is None
    assert plan_executable_target(positions, {"XAUUSD": 0.01}, equity, target_mode="quantity").off_symbol_count == 0
    assert plan_executable_target(positions, {}, equity, target_mode="quantity").rows[0].action == "close"


def test_quantity_snapshot_roundtrip_and_downgrade_guard(native_database):
    path = str(native_database)
    portfolio = _core.portfolio_operation(
        path,
        json.dumps(
            {
                "op": "create",
                "portfolio": {
                    "name": "quantity",
                    "market": "期货",
                    "custom_calc_py_code": CODE,
                    "created_at": "2026-11-06T00:00:00Z",
                    "updated_at": "2026-11-06T00:00:00Z",
                },
            }
        ),
    )
    saved = _core.target_operation(
        path,
        json.dumps(
            {
                "op": "append",
                "portfolio_id": portfolio["id"],
                "target_mode": "quantity",
                "raw_weights": {"XAUUSD": 0.015},
                "normalized_weights": None,
                "source": "manual",
                "calculated_at": "2026-11-06T00:00:00Z",
            }
        ),
    )
    restored = json.loads(
        _core.portfolio_query_json(path, json.dumps({"op": "target_snapshot", "portfolio_id": portfolio["id"]}))
    )
    assert restored["target_mode"] == saved["target_mode"] == "quantity"
    assert restored["raw_targets"] == {"XAUUSD": 0.015} and restored["weights"] == {}
    public = target_snapshot_public(saved, weight_kind="raw")
    assert public.raw_targets == {"XAUUSD": 0.015} and public.weights == {}
    assert public.quantities is None
    with pytest.raises(_core.NativeError, match="数量目标"):
        _core.migration_downgrade(path, '{"public":"0034"}')
    assert _core.migration_current(path, "{}")["public"] == "0035"


def test_retry_preserves_quantity_mode_and_frozen_targets(monkeypatch):
    account = build_account(id=1)
    monkeypatch.setattr(rebalance, "_load_rebalance_account", AsyncMock(return_value=account))
    monkeypatch.setattr(rebalance, "trade_channel_check", AsyncMock())
    monkeypatch.setattr(
        rebalance,
        "get_execution_target_snapshot",
        AsyncMock(
            return_value={
                "account_id": 1,
                "target_mode": "quantity",
                "raw_weights": {"XAUUSD": 0.015},
                "normalized_weights": {"XAUUSD": 0.015},
            }
        ),
    )
    calculate = AsyncMock(side_effect=AssertionError("重试不能重新运行组合"))
    monkeypatch.setattr(rebalance, "_resolve_rebalance_request", calculate)
    trade = AsyncMock(return_value={"record_id": 1})
    monkeypatch.setattr(rebalance, "trade", trade)
    asyncio.run(rebalance._run_account_rebalance(1, execution_id="quantity"))
    assert trade.await_args.kwargs["target_mode"] == "quantity"
    assert trade.await_args.kwargs["normalized_target"] == {"XAUUSD": 0.015}
    calculate.assert_not_awaited()


def test_history_sizing_does_not_cross_target_modes(monkeypatch):
    account = build_account()
    current = dict(
        id=2,
        portfolio_id=7,
        account_id=1,
        raw_weights={"XAUUSD": 0.015},
        normalized_weights={"XAUUSD": 0.015},
        target_mode="quantity",
        source="manual",
        execution_id=None,
        calculated_at="2026-11-06T00:00:00Z",
    )
    previous = {**current, "id": 1, "target_mode": "weight", "source": "execution", "execution_id": "old"}
    monkeypatch.setattr(account_execution, "get_previous_account_target_snapshots", AsyncMock(return_value=[previous]))
    native = AsyncMock(side_effect=AssertionError("不能复用相同数字的旧权重证据"))
    monkeypatch.setattr(account_execution, "native_operation", native)
    public = asyncio.run(account_execution._account_target_public(account, current))
    assert public.target_mode == "quantity" and public.weights == {} and public.quantities is None
    native.assert_not_awaited()


@pytest.mark.parametrize("value", [True, "0.015", float("nan"), float("inf")])
def test_standard_input_does_not_drop_invalid_quantity_values(value):
    account = build_account()
    with pytest.raises((TypeError, ValueError)):
        rebalance._build_rebalance_standard_input(
            account=account,
            curr_target={"XAUUSD": value},
            last_target={},
            execution_id="bad",
            trigger_source="manual",
            target_mode="quantity",
        )
