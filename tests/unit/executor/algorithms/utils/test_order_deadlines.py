"""用可控时钟验证追单和异步兜底不能延长订单提交期限。"""

from unittest.mock import MagicMock

import pytest

from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import OrderDirection
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.algorithms.utils.test_order_tracker import (
    _build_pending_order,
    _ChaseExecutor,
    _UnconfirmedCancelExecutor,
    _wide_spread_price,
)


def test_chase_cancel_wait_uses_remaining_budget(monkeypatch) -> None:
    executor = _UnconfirmedCancelExecutor()
    clock = ClockStub()
    tracker = OrderTracker(
        executor=executor,
        clock=clock,
        chase_config=ChaseConfig(enabled=True, interval=0, cancel_confirm_timeout=3),
    )
    executor.tracker = tracker
    order = _build_pending_order("deadline-cancel")
    tracker.add_order(order, direction=OrderDirection.BUY)
    price = _wide_spread_price(order.symbol)
    tracker.latest_prices[order.symbol] = price
    monkeypatch.setattr(tracker, "_usable_price", lambda _: price)

    assert tracker.wait_for_completion(timeout=1.2) is False

    assert clock.time() == pytest.approx(1.2)
    assert executor._placed == 0


@pytest.mark.parametrize("cancel_duration, expected_placements", [(0.1, 1), (0.2, 0), (0.3, 0)])
def test_cancel_confirmation_at_deadline_does_not_replace(monkeypatch, cancel_duration, expected_placements) -> None:
    executor = _ChaseExecutor()
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock, chase_config=ChaseConfig(enabled=True, interval=0))
    executor.tracker = tracker
    order = _build_pending_order("deadline-confirm")
    tracker.add_order(order, direction=OrderDirection.BUY, submission_deadline=1.2)
    price = _wide_spread_price(order.symbol)
    tracker.latest_prices[order.symbol] = price
    monkeypatch.setattr(tracker, "_usable_price", lambda _: price)

    def confirm_cancel(_order_id):
        clock.sleep(cancel_duration)
        tracker.on_order_update(order.model_copy(update={"status": OrderStatus.CANCELED}))
        return True

    monkeypatch.setattr(executor, "cancel_order", confirm_cancel)
    clock.sleep(1)

    tracker._check_and_chase()

    assert executor._placed == expected_placements
    assert tracker._chasing_order_id is None


def test_late_fallback_callback_cannot_submit_or_extend_deadline() -> None:
    executor = _UnconfirmedCancelExecutor()
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock, chase_config=ChaseConfig(enabled=True, max_count=0))
    executor.tracker = tracker
    order = _build_pending_order("deadline-fallback")
    tracker.add_order(order, direction=OrderDirection.BUY, submission_deadline=1)
    tracker._fallback_to_market_order()
    assert tracker._chase_info[order.order_id]["market_order_fallback_pending_cancel"] is True
    # 新一轮等待不能为旧单续期。
    tracker._restrict_submission_deadlines(10)
    clock.sleep(1)

    tracker.on_order_update(order.model_copy(update={"status": OrderStatus.CANCELED}))

    assert executor._placed == 0
    assert tracker.get_pending_count() == 0
    assert not tracker._fallback_submitting
    assert tracker.all_done_event.is_set()


def test_fallback_batch_checks_deadline_before_each_child(monkeypatch) -> None:
    executor = _ChaseExecutor()
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock, chase_config=ChaseConfig(enabled=True))
    executor.tracker = tracker
    order = _build_pending_order("deadline-batch").model_copy(update={"volume": 3})
    tracker.add_order(order, direction=OrderDirection.BUY, submission_deadline=1)
    info = dict(tracker._chase_info[order.order_id])
    monkeypatch.setattr(tracker, "_is_market_fallback_price_safe", lambda *_: True)
    monkeypatch.setattr(executor, "get_order_volume_bounds", lambda *_: (1, 1), raising=False)
    place = executor.place_order
    submitted_at = []

    def slow_place(*args, **kwargs):
        submitted_at.append(clock.time())
        clock.sleep(0.6)
        return place(*args, **kwargs)

    monkeypatch.setattr(executor, "place_order", slow_place)
    monkeypatch.setattr(executor, "emit_audit_event", MagicMock())

    tracker._submit_market_fallback_order(order, info)

    assert submitted_at == pytest.approx([0, 0.6])
    assert executor._placed == 2
