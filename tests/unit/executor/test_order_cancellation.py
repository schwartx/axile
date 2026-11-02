"""公共撤单契约：提交不等于终态，限制不依赖渠道或算法。"""

from unittest.mock import Mock

import pytest

from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.order_cancellation import CancelNotAllowedError, CancelRequestStatus, cancel_orders
from axile.executor.termination import ExecutionTerminated


def order(order_id="old"):
    return UnifiedOrder(
        symbol="rb2610",
        order_id=order_id,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=10,
        price=100,
        status=OrderStatus.PENDING,
    )


@pytest.mark.parametrize("response", [True, False, CancelNotAllowedError("当前不可撤"), TimeoutError()])
@pytest.mark.parametrize("terminal", [False, True])
def test_only_actual_terminal_resolves_cancellation(response, terminal):
    original = order()
    final = original.model_copy(update={"status": OrderStatus.FILLED, "filled_volume": 10}) if terminal else None
    cancel = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    result = cancel_orders([original], cancel=cancel, reconcile=Mock(return_value=final))
    assert bool(result.unconfirmed) is (not terminal)
    assert result.terminal_orders == ([final] if terminal else [])
    assert result.attempted is (not isinstance(response, CancelNotAllowedError))
    expected = (
        CancelRequestStatus.RESTRICTED
        if isinstance(response, CancelNotAllowedError)
        else CancelRequestStatus.UNKNOWN
        if isinstance(response, Exception)
        else CancelRequestStatus.SUBMITTED
        if response
        else CancelRequestStatus.FAILED
    )
    assert result.orders[(original.symbol, original.order_id)].request_status is expected
    assert result.only_restricted is (expected is CancelRequestStatus.RESTRICTED and not terminal)
    assert result.outcomes["rb2610:old"]["terminal"] is terminal
    if isinstance(response, CancelNotAllowedError) and not terminal:
        assert result.details == {"rb2610:old": "当前不可撤"}


def test_restricted_order_does_not_stop_other_cancellations_or_lose_reason():
    first, second = order("locked"), order("cancelable")
    cancel = Mock(side_effect=[CancelNotAllowedError("委托当前禁止撤销"), True])
    final = second.model_copy(update={"status": OrderStatus.CANCELED})
    result = cancel_orders([first, second], cancel=cancel, reconcile=Mock(side_effect=[None, final]))
    assert cancel.call_count == 2
    assert result.details == {"rb2610:locked": "委托当前禁止撤销"}
    assert result.terminal_orders == [final]


@pytest.mark.parametrize("error", [MemoryError(), ExecutionTerminated(reason="stop", mode="graceful")])
@pytest.mark.parametrize("stage", ["cancel", "reconcile"])
def test_fatal_interruptions_are_not_swallowed(error, stage):
    cancel, reconcile = Mock(return_value=True), Mock(return_value=None)
    (cancel if stage == "cancel" else reconcile).side_effect = error
    with pytest.raises(type(error)):
        cancel_orders([order(), order("next")], cancel=cancel, reconcile=reconcile)
    assert cancel.call_count == 1


def test_another_order_terminal_does_not_resolve_original():
    unrelated = order("unrelated").model_copy(update={"status": OrderStatus.CANCELED})
    result = cancel_orders([order()], cancel=Mock(return_value=True), reconcile=Mock(return_value=unrelated))
    assert result.unconfirmed


def test_query_failure_does_not_erase_restriction_or_hide_uncertainty():
    result = cancel_orders(
        [order()],
        cancel=Mock(side_effect=CancelNotAllowedError("当前不可撤")),
        reconcile=Mock(side_effect=TimeoutError()),
    )
    assert not result.only_restricted
    assert result.outcomes == {
        "rb2610:old": {
            "request_status": "restricted",
            "reason": "当前不可撤",
            "terminal": False,
            "query_error": "订单终态查询失败",
        }
    }


def test_mixed_restriction_and_failed_request_is_not_normal_waiting():
    result = cancel_orders(
        [order(), order("other")],
        cancel=Mock(side_effect=[CancelNotAllowedError("当前不可撤"), False]),
        reconcile=Mock(return_value=None),
    )
    assert not result.only_restricted
    assert result.attempted
    assert len(result.unconfirmed) == 2
