"""穿零拆单 helper(submit_and_track_split_orders)回归测试."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from axile.common.order_param_model import OrderParamModel
from axile.executor.algorithms.utils.order_helper import (
    determine_close_intent,
    submit_and_track_split_orders,
)
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


class _FakeTracker:
    """最小订单跟踪器替身:登记订单并记录等待次数."""

    def __init__(self) -> None:
        self.orders: list[tuple[UnifiedOrder, dict[str, Any]]] = []
        self.wait_calls = 0

    def add_order(self, order: UnifiedOrder, **kwargs: Any) -> None:
        self.orders.append((order, kwargs))

    def assert_ready_for_submission(self) -> None:
        pass

    def wait_for_completion(self, timeout: float | None = None) -> bool:
        _ = timeout
        self.wait_calls += 1
        return True


def _make_executor(model: OrderParamModel) -> MagicMock:
    executor = MagicMock()
    executor.plan_close_orders = None
    executor.symbol = "rb2610"
    executor.order_param_model = model
    executor.get_min_notional.return_value = None
    executor.get_account_assets.return_value = _ASSETS
    executor.get_current_volume.return_value = 0.0
    counter = [0]

    def place_order(direction, order_type, volume, price=0, **kwargs):
        counter[0] += 1
        return UnifiedOrder(
            order_id=f"o{counter[0]}",
            symbol="rb2610",
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status="待成交",
            filled_volume=0.0,
            avg_price=0.0,
            extra=dict(kwargs),
        )

    executor.place_order.side_effect = place_order
    return executor


_ASSETS = UnifiedAccountAssets(available_cash=0.0, total_asset=0.0, market_value=0.0, positions=[])


def _submit(executor, tracker, direction, volume, target, current, price=100.0):
    return submit_and_track_split_orders(
        executor,
        tracker,
        direction,
        OrderType.LIMIT,
        volume,
        price,
        target_volume=float(target),
        current_volume=float(current),
        leg_timeout_seconds=1.0,
    )


def test_passthrough_single_order_on_position_side_model() -> None:
    """POSITION_SIDE 模型:无反向持仓时单笔开仓."""
    executor = _make_executor(OrderParamModel.POSITION_SIDE)
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.BUY, 5, target=5, current=0)

    assert len(orders) == 1
    assert executor.place_order.call_count == 1
    executor.get_positions.assert_called_once()
    assert tracker.wait_calls == 0


def test_unknown_model_fails_loud() -> None:
    """UNKNOWN 模型:歧义即失败,拒绝猜测下单语义."""
    executor = _make_executor(OrderParamModel.UNKNOWN)
    tracker = _FakeTracker()

    with pytest.raises(RuntimeError, match="拒绝猜测下单语义"):
        _submit(executor, tracker, OrderDirection.BUY, 5, target=5, current=0)

    executor.place_order.assert_not_called()


def test_offset_model_pure_open_without_position() -> None:
    """OFFSET 模型:无反向持仓时单笔开仓,不带 position_side."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = []
    executor.get_current_volume.side_effect = [0.0, -5.0]
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 5, target=-5, current=0)

    assert len(orders) == 1
    _, kwargs = executor.place_order.call_args
    assert "position_side" not in kwargs


def test_offset_model_exact_close_single_leg() -> None:
    """OFFSET 模型:下单量恰等于反向持仓,单笔平仓."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = [(2.0, PositionDirection.LONG)]
    executor.get_current_volume.side_effect = [2.0, 0.0]
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 2, target=0, current=2)

    assert len(orders) == 1
    assert executor.place_order.call_args.args[2] == 2
    assert executor.place_order.call_args.kwargs["position_side"] == "LONG"


def test_offset_model_cross_zero_splits_close_then_open() -> None:
    """OFFSET 模型:穿零拆成先平后开两腿,逐腿门控."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.side_effect = [[(2.0, PositionDirection.LONG)], []]
    executor.get_current_volume.side_effect = [2.0, 0.0, -3.0]
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 5, target=-3, current=2)

    assert [call.args[2] for call in executor.place_order.call_args_list] == [2, 3]
    assert executor.place_order.call_args_list[0].kwargs["position_side"] == "LONG"
    assert "position_side" not in executor.place_order.call_args_list[1].kwargs
    assert tracker.wait_calls == 2
    assert len(orders) == 2


def test_offset_model_partial_fill_self_heals() -> None:
    """OFFSET 模型:平仓腿部分成交后,按复核持仓继续补平再开仓."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.side_effect = [[(2.0, PositionDirection.LONG)], [(1.0, PositionDirection.LONG)], []]
    executor.get_current_volume.side_effect = [2.0, 1.0, 0.0, -3.0]
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 5, target=-3, current=2)

    volumes = [call.args[2] for call in executor.place_order.call_args_list]
    assert volumes == [2, 1, 3]
    sides = ["position_side" in call.kwargs for call in executor.place_order.call_args_list]
    assert sides == [True, True, False]
    assert len(orders) == 3


def test_offset_model_close_leg_skipped_blocks_open_leg() -> None:
    """OFFSET 模型:平仓腿不可执行时绝不补开仓腿(防锁仓门)."""
    from axile.executor.algorithms.exceptions import SubMinQuantityError

    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = [(2.0, PositionDirection.LONG)]
    executor.place_order.side_effect = SubMinQuantityError("碎量")
    executor.get_current_volume.return_value = 2.0
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 5, target=-3, current=2)

    assert orders == []
    assert executor.place_order.call_count == 1
    assert tracker.wait_calls == 0


def test_offset_model_open_leg_chunked_by_volume_bounds() -> None:
    """OFFSET 模型:开仓腿超单笔上限时按上限分段."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = []
    executor.get_current_volume.side_effect = [0.0, -5.0]
    executor.get_order_volume_bounds.return_value = (1, 2)
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, OrderDirection.SELL, 5, target=-5, current=0)

    assert [call.args[2] for call in executor.place_order.call_args_list] == [2, 2, 1]
    assert len(orders) == 3


def test_determine_close_intent_reports_opposite_volume() -> None:
    """determine_close_intent 返回可平反向持仓量供拆单使用."""
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = [(2.0, PositionDirection.LONG), (1.0, PositionDirection.SHORT)]

    intent = determine_close_intent(executor, OrderDirection.SELL, _ASSETS)

    assert intent.kwargs == {"position_side": "LONG"}
    assert intent.opposite_volume == 2.0


@pytest.mark.parametrize("model", [OrderParamModel.OFFSET, OrderParamModel.POSITION_SIDE])
@pytest.mark.parametrize("sign", [1, -1])
def test_split_respects_slice_budget_across_zero(model, sign) -> None:
    executor = _make_executor(model)
    direction = OrderDirection.BUY if sign == 1 else OrderDirection.SELL
    opposite = PositionDirection.SHORT if sign == 1 else PositionDirection.LONG
    executor.get_positions.side_effect = (
        [
            [(2.0, opposite)],
            [(2.0, opposite)],
            [],
        ]
        if model is OrderParamModel.POSITION_SIDE
        else [[(2.0, opposite)], []]
    )
    executor.get_current_volume.side_effect = [-sign * 2.0, 0.0, sign * 1.0]

    _submit(executor, _FakeTracker(), direction, 3, target=sign * 10, current=-sign * 2)

    assert [call.args[2] for call in executor.place_order.call_args_list] == [2, 1]
    assert executor.place_order.call_args_list[0].kwargs["position_side"] == ("SHORT" if sign == 1 else "LONG")
    assert "position_side" not in executor.place_order.call_args_list[1].kwargs


def test_offset_pure_open_stops_at_slice_target() -> None:
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = []
    executor.get_current_volume.side_effect = [0.0, 2.0]

    _submit(executor, _FakeTracker(), OrderDirection.BUY, 2, target=10, current=0)

    assert [call.args[2] for call in executor.place_order.call_args_list] == [2]


@pytest.mark.parametrize(
    "direction, position, current, side",
    [
        (OrderDirection.SELL, PositionDirection.LONG, 2, "LONG"),
        (OrderDirection.BUY, PositionDirection.SHORT, -2, "SHORT"),
    ],
)
def test_position_side_close_preserves_side(direction, position, current, side) -> None:
    executor = _make_executor(OrderParamModel.POSITION_SIDE)
    executor.get_positions.return_value = [(2.0, position)]
    executor.get_current_volume.return_value = float(current)

    _submit(executor, _FakeTracker(), direction, 2, target=0, current=current)

    assert executor.place_order.call_count == 1
    assert executor.place_order.call_args.kwargs["position_side"] == side


@pytest.mark.parametrize("model", [OrderParamModel.OFFSET, OrderParamModel.POSITION_SIDE])
def test_timeout_stops_split_and_blocks_next_slice_until_terminal(monkeypatch, model) -> None:
    executor = _make_executor(model)
    executor.get_positions.return_value = [(2.0, PositionDirection.LONG)]
    executor.get_current_volume.return_value = 2.0
    tracker = OrderTracker(executor=executor)
    # 撤单请求已受理，但订单回报尚未到达：保留真实 tracker 的 pending 状态。
    executor.get_account_assets.return_value = _ASSETS
    executor.get_pending_orders.side_effect = lambda: list(tracker.pending_orders.values())
    executor.cancel_order.return_value = True
    wait_for_completion = tracker.wait_for_completion
    monkeypatch.setattr(tracker, "wait_for_completion", lambda timeout: wait_for_completion(timeout=0))

    with pytest.raises(RuntimeError, match="订单终态尚未确认"):
        _submit(executor, tracker, OrderDirection.SELL, 5, target=-3, current=2)

    orders = list(tracker.pending_orders.values())
    assert len(orders) == 1
    assert executor.place_order.call_count == 1
    executor.cancel_order.assert_called_once_with(orders[0].order_id)
    # 下单前及 tracker 超时诊断刷新资产，拆单函数不再刷新资产后补单。
    assert executor.get_account_assets.call_count == 2
    with pytest.raises(RuntimeError, match="旧单尚未确认终态"):
        _submit(executor, tracker, OrderDirection.SELL, 5, target=-3, current=2)
    assert executor.place_order.call_count == 1

    tracker.on_order_update(orders[0].model_copy(update={"status": OrderStatus.CANCELED}))
    tracker.assert_ready_for_submission()


@pytest.mark.parametrize("elapsed, expected_volumes, expected_waits", [(0.4, [2, 3], [1, 0.6]), (1.0, [2], [1])])
def test_split_legs_share_deadline(monkeypatch, elapsed, expected_volumes, expected_waits) -> None:
    from axile.executor.algorithms.utils import order_helper

    now = [0.0]
    clock = MagicMock()
    clock.time.side_effect = lambda: now[0]
    monkeypatch.setattr(order_helper, "get_default_clock", lambda: clock)
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.side_effect = [[(2.0, PositionDirection.LONG)], []]
    executor.get_current_volume.side_effect = [2.0, 0.0, -3.0]
    waits = []

    class Tracker(_FakeTracker):
        def wait_for_completion(self, timeout=None):
            waits.append(timeout)
            now[0] += elapsed
            return True

    _submit(executor, Tracker(), OrderDirection.SELL, 5, target=-3, current=2)

    assert [call.args[2] for call in executor.place_order.call_args_list] == expected_volumes
    assert waits == pytest.approx(expected_waits)


def test_submission_gate_reconciles_missing_cancel_callback() -> None:
    executor = _make_executor(OrderParamModel.DIRECTIONAL)
    tracker = OrderTracker(executor=executor)
    order = _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)[0]
    executor.get_pending_orders.return_value = []
    executor.reconcile_terminal_order.return_value = order.model_copy(update={"status": OrderStatus.CANCELED})

    tracker.assert_ready_for_submission()

    executor.reconcile_terminal_order.assert_called_once_with("rb2610", order.order_id)
    assert tracker.get_pending_count() == 0
    _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)
    assert executor.place_order.call_count == 2


@pytest.mark.parametrize("query_fails", [False, True])
def test_submission_gate_requires_confirmed_terminal(query_fails) -> None:
    executor = _make_executor(OrderParamModel.DIRECTIONAL)
    tracker = OrderTracker(executor=executor)
    _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)
    executor.get_pending_orders.return_value = []
    executor.reconcile_terminal_order.return_value = None
    if query_fails:
        executor.get_pending_orders.side_effect = RuntimeError("query failed")

    with pytest.raises(RuntimeError, match="旧单尚未确认终态"):
        _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)

    assert executor.place_order.call_count == 1


@pytest.mark.parametrize("filled", [1.0, 2.0])
def test_reconciled_fill_reduces_next_slice_quantity(filled) -> None:
    executor = _make_executor(OrderParamModel.DIRECTIONAL)
    tracker = OrderTracker(executor=executor)
    order = _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)[0]
    executor.get_pending_orders.return_value = []
    executor.reconcile_terminal_order.return_value = order.model_copy(
        update={"status": OrderStatus.CANCELED, "filled_volume": filled}
    )
    executor.get_account_assets.return_value = _ASSETS
    executor.get_current_volume.return_value = filled

    orders = _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)

    assert sum(order.volume for order in orders) == 2 - filled
    assert executor.get_account_assets.call_count == 2


@pytest.mark.parametrize("chase_enabled", [False, True])
def test_subsecond_wait_stops_at_deadline_without_chasing(monkeypatch, chase_enabled) -> None:
    from tests.fixtures.algorithm_fakes import ClockStub

    executor = _make_executor(OrderParamModel.DIRECTIONAL)
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock, chase_config=ChaseConfig() if chase_enabled else None)
    order = _submit(executor, tracker, OrderDirection.BUY, 2, target=2, current=0)[0]
    executor.get_pending_orders.return_value = [order]
    executor.get_account_assets.return_value = _ASSETS
    executor.cancel_order.return_value = True
    chase = MagicMock()
    fallback = MagicMock()
    monkeypatch.setattr(tracker, "_check_and_chase", chase)
    monkeypatch.setattr(tracker, "_fallback_to_market_order", fallback)

    with pytest.raises(RuntimeError, match="订单终态尚未确认"):
        tracker.wait_for_completion(timeout=0.2)

    assert clock.time() == pytest.approx(0.2)
    chase.assert_not_called()
    fallback.assert_not_called()
    executor.cancel_order.assert_called_once_with(order.order_id)


@pytest.mark.parametrize("model", [OrderParamModel.OFFSET, OrderParamModel.POSITION_SIDE, OrderParamModel.DIRECTIONAL])
@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("arrival", ["callback", "query"])
def test_final_fill_before_gate_always_refreshes_slice(model, sign, arrival) -> None:
    executor = _make_executor(model)
    tracker = OrderTracker(executor=executor)
    direction = OrderDirection.BUY if sign == 1 else OrderDirection.SELL
    old_order = executor.place_order(direction, OrderType.LIMIT, 2, 100)
    tracker.add_order(old_order)
    terminal = old_order.model_copy(update={"status": OrderStatus.CANCELED, "filled_volume": 1.0})
    executor.get_pending_orders.return_value = []
    executor.reconcile_terminal_order.return_value = terminal
    if arrival == "callback":
        # 调用方先取了 current=0 的快照，旧单随后部分成交并确认撤销。
        tracker.on_order_update(terminal)
    live_volume = [float(sign)]
    executor.get_current_volume.side_effect = lambda _: live_volume[0]
    executor.get_positions.return_value = []
    place = executor.place_order.side_effect

    def fill_new_order(*args, **kwargs):
        order = place(*args, **kwargs)
        live_volume[0] += sign * order.volume
        tracker.on_order_update(order.model_copy(update={"status": OrderStatus.FILLED, "filled_volume": order.volume}))
        return order

    executor.place_order.side_effect = fill_new_order

    orders = _submit(executor, tracker, direction, 2, target=sign * 2, current=0)

    assert [order.volume for order in orders] == [1.0]
    assert live_volume[0] == sign * 2


@pytest.mark.parametrize("model", [OrderParamModel.OFFSET, OrderParamModel.POSITION_SIDE, OrderParamModel.DIRECTIONAL])
def test_slow_snapshot_cannot_submit_after_deadline(monkeypatch, model) -> None:
    from axile.executor.algorithms.utils import order_helper
    from tests.fixtures.algorithm_fakes import ClockStub

    clock = ClockStub()
    monkeypatch.setattr(order_helper, "get_default_clock", lambda: clock)
    executor = _make_executor(model)

    def slow_snapshot():
        clock.sleep(2)
        return _ASSETS

    executor.get_account_assets.side_effect = slow_snapshot

    assert _submit(executor, _FakeTracker(), OrderDirection.BUY, 2, target=2, current=0) == []
    executor.place_order.assert_not_called()


def test_chunk_submission_stops_when_budget_expires(monkeypatch) -> None:
    from axile.executor.algorithms.utils import order_helper
    from tests.fixtures.algorithm_fakes import ClockStub

    clock = ClockStub()
    monkeypatch.setattr(order_helper, "get_default_clock", lambda: clock)
    executor = _make_executor(OrderParamModel.OFFSET)
    executor.get_positions.return_value = []
    executor.get_order_volume_bounds.return_value = (1, 1)
    place = executor.place_order.side_effect

    def slow_place(*args, **kwargs):
        clock.sleep(0.6)
        return place(*args, **kwargs)

    executor.place_order.side_effect = slow_place

    orders = _submit(executor, _FakeTracker(), OrderDirection.BUY, 3, target=3, current=0)

    assert [order.volume for order in orders] == [1, 1]
    assert executor.place_order.call_count == 2


@pytest.mark.parametrize("direction,side,sign", [(OrderDirection.SELL, "LONG", 1), (OrderDirection.BUY, "SHORT", -1)])
def test_mixed_today_yesterday_close_before_crossing_zero(direction, side, sign):
    from axile.executor.futures_order_intent import plan_futures_close_orders
    from tests.unit.executor.test_futures_order_intent import _assets

    executor = _make_executor(OrderParamModel.OFFSET)
    assets = _assets("rb2610", side, 3, 2)
    executor.get_account_assets.return_value = assets
    executor.get_current_volume.side_effect = [sign * 5, 0, -sign * 2]
    pos_dir = PositionDirection.LONG if side == "LONG" else PositionDirection.SHORT
    executor.get_positions.side_effect = [[(5, pos_dir)], []]
    executor.get_order_volume_bounds.return_value = (1, 2)
    executor.plan_close_orders = lambda d, v, a: plan_futures_close_orders("rb2610", d, v, a, "SHFE")
    tracker = _FakeTracker()

    orders = _submit(executor, tracker, direction, 7, target=-sign * 2, current=sign * 5)

    calls = executor.place_order.call_args_list
    assert [c.args[2] for c in calls] == [2, 2, 1, 2]
    assert [c.kwargs.get("offset_flag") for c in calls] == ["close_yesterday", "close_today", "close_today", None]
    assert len(orders) == 4
    assert tracker.wait_calls == 2
