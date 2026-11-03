"""市价兜底批次的同步回调、数量边界与部分失败回归。"""

import pytest

from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.unified_order import OrderType, TradeRecord
from tests.fixtures.algorithm_fakes import ClockStub, build_fallback_tracker


@pytest.mark.parametrize("early_status", [None, OrderStatus.FILLED, OrderStatus.REJECTED])
def test_market_batch_handles_synchronous_cancel_and_early_callbacks(monkeypatch, early_status):
    executor, tracker, parent = build_fallback_tracker()
    parent.volume = 12
    info = tracker._chase_info[parent.order_id]
    info.update(offset_flag="3", position_side="long", trade_rule={"max_single_order_size": 10})
    bounds_calls = []

    def bounds(kind, rule):
        bounds_calls.append((kind, rule))
        return 1, 3

    monkeypatch.setattr(executor, "get_order_volume_bounds", bounds, raising=False)
    original_place = executor.place_order

    def place(*args, **kwargs):
        assert not tracker.all_done_event.is_set()
        child = original_place(*args, **kwargs)
        if early_status == OrderStatus.FILLED:
            trade = TradeRecord(
                order_id=child.order_id,
                symbol=child.symbol,
                trade_id=f"trade-{child.order_id}",
                trade_time="2026-09-09T10:00:00",
                trade_volume=child.volume,
                trade_price=100,
                trade_value=child.volume * 100,
            )
            tracker.on_trade_record(trade)
            tracker.on_trade_record(trade)
        if early_status:
            update = child.model_copy(
                update={
                    "status": early_status,
                    "filled_volume": child.volume if early_status == OrderStatus.FILLED else 0,
                }
            )
            tracker.on_order_update(update)
        return child

    monkeypatch.setattr(executor, "place_order", place)
    terminal = parent.model_copy(update={"status": OrderStatus.CANCELED, "filled_volume": 2})

    def cancel(_order_id):
        tracker.on_order_update(terminal)
        tracker.on_order_update(terminal)
        return True

    monkeypatch.setattr(executor, "cancel_order", cancel)
    tracker._fallback_to_market_order()
    tracker._fallback_to_market_order()
    assert [call["volume"] for call in executor.place_calls] == [3, 3, 3, 1]
    assert bounds_calls == [(OrderType.MARKET, info["trade_rule"])]
    assert all(
        call["kwargs"]
        == {
            "offset_flag": "3",
            "position_side": "long",
            "trade_rule": info["trade_rule"],
            "market_data": tracker.latest_prices[parent.symbol],
            "market_data_purpose": "order",
        }
        for call in executor.place_calls
    )
    if early_status == OrderStatus.FILLED:
        assert len(tracker.get_all_trades()) == 4
        assert sum(trade.trade_volume for trade in tracker.get_all_trades()) == 10
    assert not tracker._chase_info
    assert tracker.all_done_event.is_set() == bool(early_status)
    assert len(tracker.completed_orders if early_status else tracker.pending_orders) == (5 if early_status else 4)
    audits = [event["details"]["fallback"] for event in executor.audit_events]
    assert [audit["child_index"] for audit in audits] == [1, 2, 3, 4]
    assert audits[-1]["submitted_volume"] == 10
    assert audits[-1]["remaining_volume"] == 0


@pytest.mark.parametrize("recovery", [False, True])
def test_market_batch_stops_after_partial_submission(monkeypatch, recovery):
    executor, tracker, parent = build_fallback_tracker()
    parent.volume = 10
    monkeypatch.setattr(executor, "get_order_volume_bounds", lambda *_: (1, 3), raising=False)
    original_place = executor.place_order
    calls = 0

    def place(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CtpSessionRecoveryRequired("lost") if recovery else ValueError("failed")
        return original_place(*args, **kwargs)

    monkeypatch.setattr(executor, "place_order", place)
    tracker._fallback_to_market_order()
    terminal = parent.model_copy(update={"status": OrderStatus.CANCELED})
    if recovery:
        with pytest.raises(CtpSessionRecoveryRequired):
            tracker.on_order_update(terminal)
    else:
        tracker.on_order_update(terminal)
    tracker.on_order_update(terminal)
    assert calls == 2
    assert len(tracker.pending_orders) == 1
    assert not tracker.all_done_event.is_set()
    assert not tracker._fallback_submitting
    assert executor.audit_events[-1]["details"]["fallback"]["remaining_volume"] == 7


def test_market_batch_records_unrepresentable_tail(monkeypatch):
    executor, tracker, parent = build_fallback_tracker()
    parent.volume = 5
    monkeypatch.setattr(executor, "get_order_volume_bounds", lambda *_: (3, 4), raising=False)
    tracker._fallback_to_market_order()
    tracker.on_order_update(parent.model_copy(update={"status": OrderStatus.CANCELED}))
    assert [call["volume"] for call in executor.place_calls] == [4]
    assert executor.audit_events[-1]["details"]["fallback"]["remaining_volume"] == 1


def test_wait_entry_does_not_finish_during_submission(monkeypatch):
    _executor, tracker, parent = build_fallback_tracker()
    tracker.pending_orders.clear()
    tracker._fallback_submitting.add(parent.order_id)
    tracker.clock = ClockStub()

    def waiting(_event, _timeout):
        raise RuntimeError("entered wait loop")

    monkeypatch.setattr(tracker.clock, "event_wait", waiting)
    with pytest.raises(RuntimeError, match="entered wait loop"):
        tracker.wait_for_completion(1)
