"""成交判定独立于持仓刷新，未知证据不提升成功结论。"""

import pytest

from axile.executor.algorithms.utils.outcome import confirmed_position, summarize_outcome
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


def order(**changes):
    return UnifiedOrder(
        **{
            "order_id": "filled",
            "symbol": "A",
            "direction": OrderDirection.BUY,
            "order_type": OrderType.LIMIT,
            "volume": 100,
            "price": 1,
            "filled_volume": 100,
            "status": OrderStatus.FILLED,
            **changes,
        }
    )


def test_duplicate_cumulative_fills_and_trade_records_are_not_added_twice():
    filled = order()
    assert confirmed_position(0, [filled, filled.model_copy()]) == 100
    result = summarize_outcome(0, 0, 100, [filled, filled.model_copy()], [])
    assert result["status"] == ExecutionStatus.SUCCEEDED
    assert result["expected_volume"] == 100
    assert result["final_volume"] is None
    assert result["position_confirmed"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"order_id": ""},
        {"status": OrderStatus.UNKNOWN},
        {"status": OrderStatus.PENDING},
        {"status": OrderStatus.REJECTED},
        {"filled_volume": float("nan")},
        {"filled_volume": float("inf")},
        {"filled_volume": -1},
        {"filled_volume": 101},
        {"filled_volume": 0},
        {"volume": float("inf")},
    ],
)
def test_incomplete_order_evidence_cannot_confirm_position(changes):
    assert confirmed_position(0, [order(**changes)]) is None


def test_conflicting_duplicate_identity_and_unknown_start_cannot_confirm_position():
    assert confirmed_position(0, [order(), order(symbol="B")]) is None
    assert confirmed_position(float("nan"), [order()]) is None
    assert confirmed_position(0, []) is None


@pytest.mark.parametrize("error", ["撤单失败", "报单失败", "账户控制受限"])
def test_fills_do_not_erase_real_execution_errors(error):
    result = summarize_outcome(0, 0, 100, [order()], [], explicit_error=error)
    assert result["status"] == ExecutionStatus.PARTIAL
    assert result["error"] == error


def test_partial_canceled_order_remains_partial_with_known_expected_position():
    result = summarize_outcome(0, 0, 100, [order(status=OrderStatus.CANCELED, filled_volume=40)], [])
    assert result["status"] == ExecutionStatus.PARTIAL
    assert result["expected_volume"] == 40
    assert result["position_confirmed"] is False
