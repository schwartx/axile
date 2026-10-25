"""三种真实算法入口的结果状态与恢复异常回归。"""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from axile.executor.algorithms.core.base import AlgorithmInput
from axile.executor.algorithms.defaults.pov import impl as pov
from axile.executor.algorithms.defaults.single_maker import impl as maker
from axile.executor.algorithms.defaults.twap import impl as twap
from axile.executor.algorithms.utils import clock as clock_module
from axile.executor.algorithms.utils import order_helper
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import ExecutionStatus, outcome_from_status
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.termination import ExecutionTerminated


class RecoveryRequired(RuntimeError):
    requires_session_recovery = True


@pytest.mark.parametrize("module", [maker, twap, pov], ids=["maker", "twap", "pov"])
@pytest.mark.parametrize(
    "case, expected",
    [
        ("reject", ExecutionStatus.FAILED),
        ("risk_block", ExecutionStatus.BLOCKED),
        # 挂单超时撤单且零成交零位移：没有执行进展，如实判 FAILED 而非 PARTIAL 告警。
        ("timeout", ExecutionStatus.FAILED),
        ("partial", ExecutionStatus.PARTIAL),
        ("unknown_cancel", ExecutionStatus.PARTIAL),
        ("filled", ExecutionStatus.SUCCEEDED),
        ("noop", ExecutionStatus.NOOP),
        ("recovery", None),
        ("final_query_failed", ExecutionStatus.PARTIAL),
        ("final_query_terminated", None),
    ],
)
def test_algorithm_outcome(monkeypatch, module, case, expected, termination_at=None, source="real"):
    order_module = maker if module is maker else order_helper
    executor = MagicMock()
    executor.symbol = "rb2610"
    assets = UnifiedAccountAssets(available_cash=10000, total_asset=10000, market_value=0, positions=[], source=source)
    executor.get_account_assets.return_value = assets
    volume = [2 if case == "noop" else 0]
    executor.get_current_volume.side_effect = lambda _: volume[0]
    executor.get_market_data.return_value = UnifiedPriceData(
        symbol="rb2610",
        last_price=100,
        bid_price=99,
        ask_price=101,
        bid_volume=1,
        ask_volume=1,
        volume=10,
        turnover=1000,
        timestamp=1,
        update_time="",
    )
    tracker = MagicMock()
    orders = []
    tracker.get_all_orders.side_effect = lambda: orders
    tracker.get_all_trades.return_value = []
    monkeypatch.setattr(module, "setup_order_tracker", lambda *args: tracker)
    monkeypatch.setattr(module, "teardown_order_tracker", lambda *args: None)
    now = [0.0]
    clock = MagicMock()
    clock.time.side_effect = lambda: now[0]
    monkeypatch.setattr(clock_module, "_default_clock", clock)

    def submit(*args, **kwargs):
        if case == "risk_block":
            from tests.unit.executor.test_account_control_diagnostics import make_guard

            guard = make_guard({"per_day": {"limit": 0, "on_trigger": "block"}})
            guard.begin_operation("place_order", symbol="rb2610")
        if case == "reject":
            raise RuntimeError("synthetic reject")
        if case == "recovery":
            raise RecoveryRequired("rebuild session")
        volume[0] = {"filled": 2, "partial": 1}.get(case, 0)
        status = {"filled": OrderStatus.FILLED, "partial": OrderStatus.CANCELED, "timeout": OrderStatus.CANCELED}.get(
            case, OrderStatus.PENDING
        )
        order = UnifiedOrder(
            order_id="1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=2,
            price=101,
            status=status,
            filled_volume=volume[0],
        )
        orders.append(order)
        if case == "final_query_failed":
            executor.get_account_assets.side_effect = RuntimeError("native query failed")
        if case == "final_query_terminated":
            executor.get_account_assets.side_effect = ExecutionTerminated(reason="stop", mode="graceful")
        return order

    def split_submit(*args, **kwargs):
        assert kwargs["deadline"] == pytest.approx(now[0] + 1)
        order = submit(*args, **kwargs)
        # 模拟拆单已经消耗一部分本片时间，外层等待不得重置额度。
        now[0] += 0.25
        return [order] if order is not None else []

    monkeypatch.setattr(order_module, "submit_and_track_split_orders", split_submit)
    if module is maker:
        entry, params = maker.single_maker_callback, maker.SingleMakerParams(max_wait_seconds=1)
    else:
        executor.sleep_or_terminate.side_effect = lambda duration: now.__setitem__(0, now[0] + duration)
        monkeypatch.setattr(
            order_helper, "cancel_pending_orders_via_query", lambda _: ["1"] if case == "unknown_cancel" else []
        )
        if module is twap:
            entry, params = twap.twap, twap.TwapParams(slices=1, total_duration=1, max_wait_seconds=1)
        else:
            entry, params = (
                pov.pov,
                pov.PovParams(max_duration=1, interval_seconds=1, complete_on_timeout=True, max_wait_seconds=1),
            )
    input_data = AlgorithmInput(symbol="rb2610", target_volume=2, trade_rule={}, params=params)
    if termination_at is not None:
        _assert_mid_execution_termination(monkeypatch, module, executor, tracker, entry, input_data, termination_at)
        return
    if case in {"recovery", "final_query_terminated"}:
        with pytest.raises(RecoveryRequired if case == "recovery" else ExecutionTerminated):
            entry(executor, input_data)
        return
    result = entry(executor, input_data)
    # 路由响应和 SQLite JSON 触发器都不接受 NaN，失败结果也必须能落库回放。
    json.dumps(result.model_dump(mode="json"), allow_nan=False)
    if case not in {"noop", "reject", "risk_block"}:
        assert tracker.wait_for_completion.call_args.kwargs["timeout"] == pytest.approx(0.75)
    # 柔和版保留 outcome 键，但它必须恒等于 f(status)，不允许独立结论。
    assert "diagnostics" not in result.model_dump()
    assert result.outcome == outcome_from_status(result.status)
    assert result.outcome_reason == result.error
    assert result.status == expected
    assert result.model_dump(mode="json")["status"] == expected.value
    _assert_result_evidence(result, expected, case, volume, orders)


@pytest.mark.parametrize("module", [twap, pov], ids=["twap", "pov"])
@pytest.mark.parametrize("stage", ["checkpoint", "submit", "wait", "sleep"])
def test_mid_execution_termination_preserves_payload(monkeypatch, module, stage):
    test_algorithm_outcome(monkeypatch, module, "timeout", None, termination_at=stage)


@pytest.mark.parametrize("stage", ["submit", "wait"])
def test_maker_termination_preserves_payload(monkeypatch, stage):
    test_algorithm_outcome(monkeypatch, maker, "timeout", None, termination_at=stage)


def test_maker_initial_position_failure_does_not_enter_finalization(monkeypatch):
    executor = MagicMock()
    executor.get_account_assets.return_value = UnifiedAccountAssets(
        available_cash=1, total_asset=1, market_value=0, positions=[]
    )
    executor.get_market_data.return_value = None
    error = ValueError("initial position unavailable")
    executor.get_current_volume.side_effect = error
    setup = MagicMock()
    monkeypatch.setattr(maker, "setup_order_tracker", setup)
    with pytest.raises(ValueError) as raised:
        maker.single_maker_callback(
            executor, AlgorithmInput(symbol="A", target_volume=1, trade_rule={}, params=maker.SingleMakerParams())
        )
    assert raised.value is error
    setup.assert_not_called()
    executor.get_account_assets.assert_called_once()


@pytest.mark.parametrize("module", [maker, twap, pov], ids=["maker", "twap", "pov"])
@pytest.mark.parametrize("source", ["unavailable", "assumed", "error"])
def test_algorithm_does_not_trade_when_start_snapshot_is_unknown(monkeypatch, module, source):
    executor = MagicMock()
    executor.symbol = "rb2610"
    executor.get_account_assets.return_value = UnifiedAccountAssets(
        available_cash=1, total_asset=1, market_value=0, positions=[], source=source
    )
    executor.get_market_data.return_value = None
    setup = MagicMock()
    monkeypatch.setattr(module, "setup_order_tracker", setup)
    if module is maker:
        entry, params = maker.single_maker_callback, maker.SingleMakerParams()
    elif module is twap:
        entry, params = twap.twap, twap.TwapParams(slices=1, total_duration=1, max_wait_seconds=1)
    else:
        entry, params = pov.pov, pov.PovParams(max_duration=1, interval_seconds=1, max_wait_seconds=1)
    result = entry(executor, AlgorithmInput(symbol="rb2610", target_volume=2, trade_rule={}, params=params))
    assert result.status == ExecutionStatus.FAILED
    assert result.error == "初始持仓尚未确认"
    assert result.orders == []
    setup.assert_not_called()
    executor.get_current_volume.assert_not_called()


@pytest.mark.parametrize("module", [maker, twap, pov], ids=["maker", "twap", "pov"])
def test_algorithm_keeps_tracker_cancel_error(monkeypatch, module):
    order_module = maker if module is maker else order_helper
    executor = MagicMock()
    executor.symbol = "rb2610"
    assets = UnifiedAccountAssets(available_cash=10000, total_asset=10000, market_value=0, positions=[], source="real")
    executor.get_account_assets.return_value = assets
    executor.get_current_volume.side_effect = lambda _: 0
    executor.get_market_data.return_value = UnifiedPriceData(
        symbol="rb2610",
        last_price=100,
        bid_price=99,
        ask_price=101,
        bid_volume=1,
        ask_volume=1,
        volume=10,
        turnover=1000,
        timestamp=1,
        update_time="",
    )
    tracker = MagicMock()
    orders = []
    tracker.get_all_orders.side_effect = lambda: orders
    tracker.get_all_trades.return_value = []
    tracker.explicit_error = None
    tracker.explicit_blocked_error = None

    def wait_and_fail(timeout):
        _ = timeout
        tracker.explicit_error = "撤单失败，订单终态尚未确认"
        raise RuntimeError("部分订单撤销失败: 1")

    tracker.wait_for_completion.side_effect = wait_and_fail
    monkeypatch.setattr(module, "setup_order_tracker", lambda *args: tracker)
    monkeypatch.setattr(module, "teardown_order_tracker", lambda *args: None)
    now = [0.0]
    clock = MagicMock()
    clock.time.side_effect = lambda: now[0]
    monkeypatch.setattr(clock_module, "_default_clock", clock)

    def split_submit(*args, **kwargs):
        _ = args, kwargs
        order = UnifiedOrder(
            order_id="1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=2,
            price=101,
            status=OrderStatus.PENDING,
            filled_volume=0,
        )
        orders.append(order)
        now[0] += 0.25
        return [order]

    monkeypatch.setattr(order_module, "submit_and_track_split_orders", split_submit)
    if module is not maker:
        monkeypatch.setattr(order_helper, "cancel_pending_orders_via_query", lambda _: [])
    if module is maker:
        entry, params = maker.single_maker_callback, maker.SingleMakerParams(max_wait_seconds=1)
    elif module is twap:
        executor.sleep_or_terminate.side_effect = lambda duration: now.__setitem__(0, now[0] + duration)
        entry, params = twap.twap, twap.TwapParams(slices=1, total_duration=1, max_wait_seconds=1)
    else:
        executor.sleep_or_terminate.side_effect = lambda duration: now.__setitem__(0, now[0] + duration)
        entry, params = (
            pov.pov,
            pov.PovParams(max_duration=1, interval_seconds=1, complete_on_timeout=True, max_wait_seconds=1),
        )
    result = entry(executor, AlgorithmInput(symbol="rb2610", target_volume=2, trade_rule={}, params=params))
    assert result.status == ExecutionStatus.PARTIAL
    assert result.error == "撤单失败，订单终态尚未确认"


def _assert_mid_execution_termination(monkeypatch, module, executor, tracker, entry, input_data, stage):
    order_module = maker if module is maker else order_helper
    stopped = ExecutionTerminated(reason="用户终止", mode="cancel_pending", cancel_failed_order_ids=["unconfirmed"])
    methods = {
        "checkpoint": executor.handle_termination_checkpoint,
        "wait": tracker.wait_for_completion,
        "sleep": executor.sleep_or_terminate,
    }
    if stage == "submit":
        monkeypatch.setattr(order_module, "submit_and_track_split_orders", MagicMock(side_effect=stopped))
    else:
        methods[stage].side_effect = stopped
    if stage == "sleep" and module is twap:
        input_data = replace(input_data, params=twap.TwapParams(slices=2, total_duration=2, max_wait_seconds=1))
    cleanup = MagicMock()
    monkeypatch.setattr(module, "teardown_order_tracker", cleanup)
    with pytest.raises(ExecutionTerminated) as raised:
        entry(executor, input_data)
    assert raised.value is stopped
    assert raised.value.cancel_failed_order_ids == ["unconfirmed"]
    cleanup.assert_called_once()


def _assert_result_evidence(result, expected, case, volume, orders):
    if expected in {ExecutionStatus.FAILED, ExecutionStatus.PARTIAL}:
        assert result.error
        if case != "final_query_failed":
            assert result.memory["remaining_volume"] == 2 - volume[0]
    if case == "final_query_failed":
        assert result.final_volume is None
        assert result.orders == orders
        assert result.error == "最终持仓查询失败，具体原因未确认"
    if case == "reject":
        assert result.error == "报单失败，具体原因未确认"
    if case == "risk_block":
        assert "每日下单次数" in result.error
        assert "当前已用 0 次，上限 0 次" in result.error
        assert "per_day" not in result.error
    if case == "noop":
        assert not orders


@pytest.mark.parametrize("module", [maker, twap, pov], ids=["maker", "twap", "pov"])
@pytest.mark.parametrize("model_name", ["offset", "position_side"])
@pytest.mark.parametrize("sign", [1, -1])
def test_real_algorithm_crosses_zero_within_slice_budget(monkeypatch, module, model_name, sign):
    from axile.common.order_param_model import OrderParamModel
    from axile.executor.models.unified_account_assets import PositionDirection
    from tests.fixtures.algorithm_fakes import ClockStub
    from tests.unit.executor.algorithms.test_split_orders import _make_executor

    clock = ClockStub()
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    executor = _make_executor(OrderParamModel(model_name))
    current = [-sign * 2.0]
    executor.get_current_volume.side_effect = lambda _: current[0]
    executor.get_positions.side_effect = lambda _: (
        [(abs(current[0]), PositionDirection.LONG if current[0] > 0 else PositionDirection.SHORT)] if current[0] else []
    )
    price = UnifiedPriceData(
        symbol=executor.symbol,
        last_price=100,
        bid_price=99,
        ask_price=101,
        bid_volume=10,
        ask_volume=10,
        volume=0,
        timestamp=0,
        update_time="",
        book_valid=True,
    )
    executor.get_market_data.return_value = price
    executor.get_pending_orders.return_value = []
    callbacks = {"order": [], "trade": [], "price": []}
    for kind in callbacks:
        getattr(executor, f"register_{kind}_callback").side_effect = callbacks[kind].append
        getattr(executor, f"unregister_{kind}_callback").side_effect = callbacks[kind].remove
    seen_market_volume = [0.0]

    def emit_market_volume():
        seen_market_volume[0] += 3
        for callback in callbacks["price"]:
            callback(price.model_copy(update={"volume": seen_market_volume[0]}))

    def register_price(callback):
        callbacks["price"].append(callback)
        callback(price)
        emit_market_volume()

    executor.register_price_callback.side_effect = register_price

    def sleep(duration):
        clock.sleep(duration)
        emit_market_volume()

    executor.sleep_or_terminate.side_effect = sleep
    place = executor.place_order.side_effect
    submissions = []

    def fill(direction, order_type, volume, order_price, **kwargs):
        if current[0] * sign < 0:
            assert kwargs.get("position_side") == ("LONG" if current[0] > 0 else "SHORT")
            assert volume <= abs(current[0])
        else:
            assert "position_side" not in kwargs
        assert "deadline" not in kwargs
        assert order_price == (101 if direction == OrderDirection.BUY else 99)
        order = place(direction, order_type, volume, order_price, **kwargs)
        current[0] += sign * volume
        submissions.append((clock.time(), volume))
        for callback in callbacks["order"]:
            callback(order.model_copy(update={"status": OrderStatus.FILLED, "filled_volume": volume}))
        return order

    executor.place_order.side_effect = fill
    if module is maker:
        entry = maker.single_maker_callback
        params = maker.SingleMakerParams(max_wait_seconds=2, chase_enabled=False)
    elif module is twap:
        entry = twap.twap
        params = twap.TwapParams(slices=2, total_duration=2, max_wait_seconds=1)
    else:
        entry = pov.pov
        params = pov.PovParams(
            participation_rate=1,
            max_duration=2,
            interval_seconds=1,
            max_wait_seconds=1,
            complete_on_timeout=False,
        )

    result = entry(
        executor, AlgorithmInput(symbol=executor.symbol, target_volume=sign * 4, trade_rule={}, params=params)
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    assert current[0] == sign * 4
    assert submissions == ([(0, 2), (0, 4)] if module is maker else [(0, 2), (0, 1), (1, 3)])
    assert all(not registered for registered in callbacks.values())


@pytest.mark.parametrize("module", [maker, twap, pov], ids=["maker", "twap", "pov"])
@pytest.mark.parametrize("source", ["simulation", "custom-channel"])
@pytest.mark.parametrize("case,status", [("filled", ExecutionStatus.SUCCEEDED), ("noop", ExecutionStatus.NOOP)])
def test_custom_source_is_valid_through_real_algorithm(monkeypatch, module, source, case, status):
    test_algorithm_outcome(monkeypatch, module, case, status, source=source)
