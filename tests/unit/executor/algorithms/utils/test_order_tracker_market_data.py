"""追单和市价兜底必须携带其实际消费的行情证据。"""

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.algorithms.utils import clock as clock_module
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.algorithms.utils.test_order_tracker import _build_pending_order, _ChaseExecutor, _price
from tests.unit.executor.test_base_executor_symbol_dispatch import _TestExecutor


@pytest.mark.parametrize("direction", [OrderDirection.BUY, OrderDirection.SELL])
def test_chase_reprices_with_post_cancel_quote_and_remaining_volume(monkeypatch, direction):
    clock = ClockStub()
    clock.current = 1000.0
    executor = _ChaseExecutor()
    tracker = OrderTracker(executor, clock=clock, chase_config=ChaseConfig(enabled=True, interval=0))
    executor.tracker = tracker
    order = _build_pending_order("reprice").model_copy(update={"direction": direction})
    tracker.add_order(order, direction=direction)
    bid = 11 if direction == OrderDirection.BUY else 8
    tracker.on_price_update(_price(order.symbol, bid, bid + 0.1, 1_000_000))
    fresh_bid = 8 if direction == OrderDirection.BUY else 12
    fresh = _price(order.symbol, fresh_bid, fresh_bid + 0.1, 1_011_000)

    def cancel(_order_id):
        clock.sleep(11)
        tracker.on_price_update(fresh)
        tracker.on_order_update(order.model_copy(update={"status": OrderStatus.CANCELED, "filled_volume": 40}))
        return True

    captured = []
    place = executor.place_order

    def submit(direction, order_type, volume, price=0, **kwargs):
        captured.append((price, volume, kwargs["market_data"]))
        fresh.timestamp = 1
        fresh.bids[0] = (1, fresh.bids[0][1])
        return place(direction, order_type, volume, price, **kwargs)

    monkeypatch.setattr(executor, "cancel_order", cancel)
    monkeypatch.setattr(executor, "place_order", submit)

    tracker._check_and_chase()

    expected = fresh_bid if direction == OrderDirection.BUY else fresh_bid + 0.1
    assert len(captured) == 1
    price, volume, evidence = captured[0]
    assert price == expected
    assert volume == 60
    assert evidence is not fresh
    assert evidence.timestamp == 1_011_000
    assert evidence.bids[0][0] == fresh_bid
    assert tracker._chasing_order_id is None
    chase = next(event for event in executor.emitted if event.get("reason_code") == "COMMON.ORDER_CHASE")
    assert chase["details"]["chase"]["to_price"] == expected


@pytest.mark.parametrize("missing_price", [False, True])
def test_chase_without_post_cancel_quote_clears_inflight_marker(monkeypatch, missing_price):
    clock = ClockStub()
    clock.current = 1000.0
    executor = _ChaseExecutor()
    tracker = OrderTracker(executor, clock=clock, chase_config=ChaseConfig(enabled=True, interval=0))
    executor.tracker = tracker
    order = _build_pending_order("quote-expired-during-cancel")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.on_price_update(_price(order.symbol, 11, 11.1, 1_000_000))
    cancel = executor.cancel_order

    def slow_cancel(order_id):
        clock.sleep(11)
        if missing_price:
            tracker.on_price_update(_price(order.symbol, 0, 11.1, 1_011_000))
        tracker.pending_orders[order_id].filled_volume = 40
        return cancel(order_id)

    monkeypatch.setattr(executor, "cancel_order", slow_cancel)

    tracker._check_and_chase()

    assert executor._placed == 0
    assert tracker._chasing_order_id is None
    assert tracker.get_pending_count() == 0
    assert tracker.all_done_event.is_set()
    assert tracker.explicit_error == "追单失败：撤单确认后行情不可用或追单价格缺失"
    failure = next(
        event for event in executor.emitted if event.get("reason_code") == "COMMON.ORDER_CHASE_MARKET_DATA_UNAVAILABLE"
    )
    assert failure["details"]["chase"]["remaining_volume"] == 60


@pytest.mark.parametrize("expire_after_first", [False, True])
def test_fallback_uses_one_frozen_snapshot_for_protection_and_each_child(monkeypatch, expire_after_first):
    clock = ClockStub()
    clock.current = 1000.0
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    owner = _TestExecutor()
    symbol = "rb2612.SHFE"
    session = ExecutionSession(owner, symbol)
    session.get_market_data()
    clock.sleep(11)
    tracker = OrderTracker(session, clock=clock, chase_config=ChaseConfig(enabled=True))
    order = _build_pending_order("fallback", symbol).model_copy(update={"volume": 3, "status": OrderStatus.CANCELED})
    snapshot = _price(symbol, 9.99, 10.01, 1_011_000)
    tracker.on_price_update(snapshot)
    tracker.add_order(order, direction=OrderDirection.BUY)
    info = dict(tracker._chase_info[order.order_id])
    tracker.on_order_update(order)
    evidence = []
    submitted = []
    place = session.place_order
    native_place = owner._place_order_impl

    def bounds(*_args):
        # 价格保护之后原始缓存可被更新，但不能改变本批提交的证据。
        snapshot.timestamp = 1
        snapshot.asks[0] = (100, snapshot.asks[0][1])
        return (1, 1)

    def submit(*args, **kwargs):
        evidence.append(kwargs["market_data"])
        return place(*args, **kwargs)

    def native_submit(*args, **kwargs):
        result = native_place(*args, **kwargs)
        result.order_id = f"child-{len(submitted) + 1}"
        submitted.append(result)
        if expire_after_first:
            clock.sleep(11)
            tracker.on_price_update(_price(symbol, 9.99, 10.01, int(clock.time() * 1000)))
        return result

    monkeypatch.setattr(session, "get_order_volume_bounds", bounds)
    monkeypatch.setattr(session, "place_order", submit)
    monkeypatch.setattr(owner, "_place_order_impl", native_submit)

    tracker._submit_market_fallback_order(order, info)

    assert len(submitted) == (1 if expire_after_first else 3)
    assert all(child.order_type == OrderType.MARKET and child.volume == 1 for child in submitted)
    assert all(item is evidence[0] for item in evidence)
    assert evidence[0] is not snapshot
    assert evidence[0].timestamp == 1_011_000
    assert evidence[0].asks[0][0] == 10.01
    if expire_after_first:
        assert "已过期" in tracker.explicit_error
        assert tracker.outcome_inputs()["error_reason_code"] == "COMMON.MARKET_DATA.UNAVAILABLE"
        assert tracker.pending_orders["child-1"] is submitted[0]
    else:
        assert tracker.explicit_error is None
        assert tracker.outcome_inputs()["error_reason_code"] is None


@pytest.mark.parametrize("invalid_book", [False, True])
def test_fallback_order_validation_reaches_final_submission_boundary(monkeypatch, invalid_book):
    clock = ClockStub()
    clock.current = 1000.0
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    owner = _TestExecutor()
    session = ExecutionSession(owner, "rb2612.SHFE")
    tracker = OrderTracker(session, clock=clock, chase_config=ChaseConfig(enabled=True))
    order = _build_pending_order("fallback-boundary", session.symbol).model_copy(
        update={"volume": 3, "status": OrderStatus.CANCELED}
    )
    tracker.add_order(order, direction=OrderDirection.BUY)
    info = dict(tracker._chase_info[order.order_id])
    tracker.on_order_update(order)
    snapshot = _price(session.symbol, 9.99, 10.01, 1_000_000)
    if invalid_book:
        snapshot.bids[0] = (snapshot.bids[0][0], 0)
    tracker.on_price_update(snapshot)
    submitted = []
    monkeypatch.setattr(owner, "_place_order_impl", lambda *args, **kwargs: submitted.append(args))

    ensure_default_account_control_registry_bootstrapped()
    guard = AccountControlGuard(
        account_id=1,
        execution_id="fallback-boundary",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"min_interval_ms": {"limit": 11000}}}}}
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
        wait_poll_interval_ms=11000,
    )
    guard.begin_operation("place_order", symbol="other").record_outcome("submitted")
    monkeypatch.setattr(owner, "get_account_control_guard", lambda: guard)

    def sleep(seconds):
        clock.sleep(seconds)
        tracker.on_price_update(_price(session.symbol, 9.99, 10.01, int(clock.time() * 1000)))

    guard._sleep = sleep
    tracker._submit_market_fallback_order(order, info)

    assert submitted == []
    assert tracker.all_done_event.is_set()
    assert tracker.outcome_inputs()["error_reason_code"] == "COMMON.MARKET_DATA.UNAVAILABLE"
    assert ("缺少真实盘口" if invalid_book else "已过期") in tracker.explicit_error
    assert clock.time() == (1000 if invalid_book else 1011)


def test_fallback_final_validation_uses_order_purpose_and_frozen_quote(monkeypatch):
    clock = ClockStub()
    clock.current = 1000.0
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    owner = _TestExecutor()
    session = ExecutionSession(owner, "rb2612.SHFE")
    tracker = OrderTracker(session, clock=clock, chase_config=ChaseConfig(enabled=True))
    order = _build_pending_order("fallback-purpose", session.symbol).model_copy(update={"status": OrderStatus.CANCELED})
    tracker.add_order(order, direction=OrderDirection.BUY)
    info = dict(tracker._chase_info[order.order_id])
    tracker.on_order_update(order)
    snapshot = _price(session.symbol, 9.99, 10.01, 1_000_000)
    tracker.on_price_update(snapshot)
    checks = []
    submitted = []
    validator = owner.market_data_error

    def validate(quote, *, purpose, now=None):
        checks.append((purpose, quote.timestamp, quote.asks[0][0]))
        if purpose == "order" and clock.time() > 1000:
            return "盘口状态已变化"
        return validator(quote, purpose=purpose, now=now)

    monkeypatch.setattr(owner, "market_data_error", validate)
    monkeypatch.setattr(owner, "_place_order_impl", lambda *args, **kwargs: submitted.append(args))
    ensure_default_account_control_registry_bootstrapped()
    guard = AccountControlGuard(
        account_id=1,
        execution_id="fallback-purpose",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"min_interval_ms": {"limit": 1000}}}}}
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
        wait_poll_interval_ms=1000,
    )
    guard.begin_operation("place_order", symbol="other").record_outcome("submitted")
    monkeypatch.setattr(owner, "get_account_control_guard", lambda: guard)

    def sleep(seconds):
        clock.sleep(seconds)
        tracker.on_price_update(_price(session.symbol, 9.98, 10.02, 1_001_000))
        snapshot.asks[0] = (100, snapshot.asks[0][1])

    guard._sleep = sleep
    tracker._submit_market_fallback_order(order, info)

    assert submitted == []
    assert checks == [("order", 1_000_000, 10.01), ("order", 1_000_000, 10.01)]
    assert "盘口状态已变化" in tracker.explicit_error
    assert tracker.completed_orders[order.order_id].extra["market_order_fallback_failed"]
    assert tracker.all_done_event.is_set()
