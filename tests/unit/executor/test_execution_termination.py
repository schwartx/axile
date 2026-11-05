"""执行器协作式终止的单元测试。"""

from __future__ import annotations

from threading import Event

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.utils.clock import get_default_clock, set_default_clock
from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.execution_runtime import ExecutionRuntime, ExecutionRuntimeBindings
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController


class _Logger:
    def info(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def warning(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def error(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)


class _TerminationTestExecutor(AbstractExecutor):
    def __init__(self, execution_orders: list[UnifiedOrder] | None = None) -> None:
        self.logger = _Logger()
        self.cleaned_up = False
        self.execution_orders = execution_orders or []
        self.cancel_attempts: list[tuple[str, str]] = []
        super().__init__(
            TradeChannel.CTP,
            CTPAccountConfig.model_validate(
                {
                    "broker_id": "b",
                    "investor_id": "i",
                    "password": "p",
                    "td_front": "tcp://td:1",
                    "md_front": "tcp://md:2",
                    "app_id": "app",
                    "auth_code": "auth",
                }
            ),
        )

    def _initialize_connection(self, account_config: CTPAccountConfig) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        return True

    def _check_trading_time(self) -> bool:
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        return {
            symbol: UnifiedPriceData(
                symbol=symbol,
                last_price=100.0,
                asks=[(101.0, 1.0)],
                bids=[(99.0, 1.0)],
                volume=1.0,
                turnover=1.0,
                timestamp=1,
                update_time="2026-03-22T10:00:00",
            )
            for symbol in symbols
        }

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = (direction, order_type, volume, price, kwargs)
        return UnifiedOrder(
            order_id=f"order-{symbol}",
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status=OrderStatus.SUBMITTED,
        )

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        assert symbol is not None
        return [order for order in self.execution_orders if order.symbol == symbol and order.is_active()]

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        raise NotImplementedError

    def _cleanup(self) -> None:
        self.cleaned_up = True

    def _get_account_mark(self) -> str:
        return "termination-test"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {symbol: {} for symbol in symbols}

    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        _ = callback

    def register_price_callback(self, callback: PriceDataCallback) -> None:
        _ = callback

    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        _ = callback

    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        _ = callback

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        _ = symbols

    def is_monitoring(self) -> bool:
        return False

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        self.cancel_attempts.append((symbol, order_id))
        return order_id != "order-fail"


def _active_order(symbol: str, order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.SUBMITTED,
    )


def _filled_order(symbol: str, order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.FILLED,
    )


def test_execution_session_termination_cancel_pending_queries_and_cancels_current_symbol_orders() -> None:
    """session 的 cancel_pending 终止应先 query 当前 symbol，再逐笔 cancel。"""
    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "order-fail")])
    cancel_event = Event()
    cancel_event.set()
    executor.set_termination_controller(
        ExecutionTerminationController(
            cancel_event=cancel_event,
            reason_provider=lambda: "manual stop",
            mode_provider=lambda: "cancel_pending",
        )
    )
    session = ExecutionSession(owner=executor, symbol="ag2612")

    with pytest.raises(ExecutionTerminated) as exc_info:
        session.handle_termination_checkpoint()

    assert exc_info.value.reason == "manual stop"
    assert exc_info.value.mode == "cancel_pending"
    assert exc_info.value.cancel_failed_order_ids == ["order-fail"]
    assert exc_info.value.acked_at is not None
    assert executor.cancel_attempts == [("ag2612", "order-fail")]


@pytest.mark.parametrize("operation", ["query", "cancel"])
def test_execution_session_cancel_pending_propagates_session_recovery(operation: str) -> None:
    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "order-1")])
    cancel_event = Event()
    cancel_event.set()
    executor.set_termination_controller(
        ExecutionTerminationController(
            cancel_event=cancel_event,
            reason_provider=lambda: "manual stop",
            mode_provider=lambda: "cancel_pending",
        )
    )
    session = ExecutionSession(owner=executor, symbol="ag2612")
    recovery = CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2)

    if operation == "query":
        session.get_pending_orders = lambda: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]
    else:
        session.cancel_order = lambda _order_id: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        session.handle_termination_checkpoint()

    assert executor.cancel_attempts == []


def test_base_executor_handle_termination_checkpoint_only_acknowledges_and_raises() -> None:
    """AbstractExecutor 检查点本身不再负责批量撤单。"""
    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "order-fail")])
    cancel_event = Event()
    cancel_event.set()
    executor.set_termination_controller(
        ExecutionTerminationController(
            cancel_event=cancel_event,
            reason_provider=lambda: "manual stop",
            mode_provider=lambda: "cancel_pending",
        )
    )

    with pytest.raises(ExecutionTerminated) as exc_info:
        executor.handle_termination_checkpoint("ag2612")

    assert exc_info.value.cancel_failed_order_ids == []
    assert executor.cancel_attempts == []


class _FakeClock:
    """记录 sleep/event_wait 的假时钟；可在 event_wait 内置位事件模拟等待中被唤醒."""

    def __init__(self, wake_event: Event | None = None) -> None:
        self.sleeps: list[float] = []
        self.event_waits: list[float] = []
        self._wake_event = wake_event

    def time(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def event_wait(self, event: Event, timeout: float) -> bool:
        self.event_waits.append(timeout)
        # 模拟「等待期间收到 terminate」：置位唤醒源后按事件状态返回。
        if self._wake_event is not None:
            self._wake_event.set()
        return event.is_set()


def _build_runtime_with_controller(
    executor: AbstractExecutor,
    controller: ExecutionTerminationController | None,
) -> ExecutionRuntime:
    bindings = ExecutionRuntimeBindings(
        audit_context={"execution_id": "exec-sleep", "account_id": 1, "algorithm": "TEST"},
        audit_sink=None,
        termination_controller=controller,
    )
    return ExecutionRuntime(owner=executor, bindings=bindings)


def _use_clock(clock: object):
    """临时替换全局默认时钟并在退出时还原."""
    previous = get_default_clock()
    set_default_clock(clock)  # type: ignore[arg-type]
    return previous


def test_runtime_sleep_or_terminate_sleeps_full_when_not_requested() -> None:
    """未收到 terminate 时应等满 seconds 后正常返回，且用可中断等待而非纯 sleep."""
    executor = _TerminationTestExecutor()
    controller = ExecutionTerminationController(cancel_event=Event())
    runtime = _build_runtime_with_controller(executor, controller)
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        runtime.sleep_or_terminate(5.0, "ag2612")
    finally:
        set_default_clock(previous)

    assert clock.event_waits == [5.0]
    assert clock.sleeps == []


def test_runtime_sleep_or_terminate_interrupts_when_event_set_mid_wait() -> None:
    """等待期间 cancel_event 被置位应立即中断并抛出 ExecutionTerminated."""
    executor = _TerminationTestExecutor()
    cancel_event = Event()
    controller = ExecutionTerminationController(
        cancel_event=cancel_event,
        reason_provider=lambda: "manual stop",
        mode_provider=lambda: "graceful",
    )
    runtime = _build_runtime_with_controller(executor, controller)
    clock = _FakeClock(wake_event=cancel_event)
    previous = _use_clock(clock)
    try:
        with pytest.raises(ExecutionTerminated) as exc_info:
            runtime.sleep_or_terminate(60.0, "ag2612")
    finally:
        set_default_clock(previous)

    assert exc_info.value.reason == "manual stop"
    assert exc_info.value.acked_at is not None
    assert clock.event_waits == [60.0]


def test_runtime_sleep_or_terminate_raises_immediately_when_already_requested() -> None:
    """调用前已收到 terminate 时应直接抛出，不进入等待."""
    executor = _TerminationTestExecutor()
    cancel_event = Event()
    cancel_event.set()
    controller = ExecutionTerminationController(cancel_event=cancel_event)
    runtime = _build_runtime_with_controller(executor, controller)
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        with pytest.raises(ExecutionTerminated):
            runtime.sleep_or_terminate(5.0, "ag2612")
    finally:
        set_default_clock(previous)

    assert clock.event_waits == []
    assert clock.sleeps == []


def test_runtime_sleep_or_terminate_falls_back_to_plain_sleep_without_controller() -> None:
    """无 controller 时退化为纯 sleep，保持既有行为不回归."""
    executor = _TerminationTestExecutor()
    runtime = _build_runtime_with_controller(executor, None)
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        runtime.sleep_or_terminate(3.0)
    finally:
        set_default_clock(previous)

    assert clock.sleeps == [3.0]
    assert clock.event_waits == []


def test_runtime_sleep_or_terminate_returns_immediately_for_non_positive_seconds() -> None:
    """seconds <= 0 时不进入任何等待."""
    executor = _TerminationTestExecutor()
    controller = ExecutionTerminationController(cancel_event=Event())
    runtime = _build_runtime_with_controller(executor, controller)
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        runtime.sleep_or_terminate(0.0)
    finally:
        set_default_clock(previous)

    assert clock.sleeps == []
    assert clock.event_waits == []


def test_session_sleep_or_terminate_interrupts_and_cancels_pending_orders() -> None:
    """session 片间等待被 terminate 唤醒时，应走 symbol 维度 cancel_pending 再抛出."""
    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "order-fail")])
    cancel_event = Event()
    controller = ExecutionTerminationController(
        cancel_event=cancel_event,
        reason_provider=lambda: "manual stop",
        mode_provider=lambda: "cancel_pending",
    )
    runtime = _build_runtime_with_controller(executor, controller)
    session = ExecutionSession(owner=executor, runtime=runtime, symbol="ag2612")
    clock = _FakeClock(wake_event=cancel_event)
    previous = _use_clock(clock)
    try:
        with pytest.raises(ExecutionTerminated) as exc_info:
            session.sleep_or_terminate(60.0)
    finally:
        set_default_clock(previous)

    assert exc_info.value.mode == "cancel_pending"
    assert exc_info.value.cancel_failed_order_ids == ["order-fail"]
    assert executor.cancel_attempts == [("ag2612", "order-fail")]


def test_session_sleep_or_terminate_sleeps_full_when_not_requested() -> None:
    """未收到 terminate 时 session 片间等待应等满 seconds 后正常返回."""
    executor = _TerminationTestExecutor()
    controller = ExecutionTerminationController(cancel_event=Event())
    runtime = _build_runtime_with_controller(executor, controller)
    session = ExecutionSession(owner=executor, runtime=runtime, symbol="ag2612")
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        session.sleep_or_terminate(5.0)
    finally:
        set_default_clock(previous)

    assert clock.event_waits == [5.0]
    assert clock.sleeps == []


def test_session_sleep_or_terminate_without_runtime_falls_back_to_plain_sleep() -> None:
    """无活跃 runtime 时退化为纯 sleep，保持既有行为不回归."""
    executor = _TerminationTestExecutor()
    session = ExecutionSession(owner=executor, symbol="ag2612")
    clock = _FakeClock()
    previous = _use_clock(clock)
    try:
        session.sleep_or_terminate(2.0)
    finally:
        set_default_clock(previous)

    assert clock.sleeps == [2.0]
    assert clock.event_waits == []


@pytest.mark.parametrize("query_failed", [False, True])
def test_termination_restriction_is_not_cancel_failure(monkeypatch, query_failed):
    from axile.executor.order_cancellation import CancelNotAllowedError

    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "restricted")])
    session = ExecutionSession(owner=executor, symbol="ag2612")

    def restricted(_order_id):
        raise CancelNotAllowedError("当前不可撤单")

    def reconcile(*_args):
        if query_failed:
            raise TimeoutError()
        return None

    monkeypatch.setattr(session, "cancel_order", restricted)
    monkeypatch.setattr(session, "reconcile_terminal_order", reconcile)
    result = session._cancel_pending_orders_before_termination()
    assert result.failed_order_ids == []
    assert not result.attempted
    assert result.unconfirmed
    item = result.cancel_outcomes["ag2612:restricted"]
    assert item["request_status"] == "restricted"
    assert item["query_error"] == ("订单终态查询失败" if query_failed else None)


def test_cancel_failure_does_not_make_confirmed_terminal_unknown(monkeypatch):
    executor = _TerminationTestExecutor(execution_orders=[_active_order("ag2612", "order-fail")])
    event = Event()
    event.set()
    executor.set_termination_controller(
        ExecutionTerminationController(cancel_event=event, mode_provider=lambda: "cancel_pending")
    )
    session = ExecutionSession(owner=executor, symbol="ag2612")
    monkeypatch.setattr(session, "reconcile_terminal_order", lambda *_: _filled_order("ag2612", "order-fail"))

    with pytest.raises(ExecutionTerminated) as caught:
        session.handle_termination_checkpoint()

    assert caught.value.cancel_failed_order_ids == ["order-fail"]
    assert not caught.value.cancel_unconfirmed
    assert caught.value.cancel_outcomes["ag2612:order-fail"]["terminal"] is True
