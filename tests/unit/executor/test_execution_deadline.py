"""执行层总超时（deadline）的单元测试。

Notes
-----
本文件的时钟装配顺序**刻意**是「先装假时钟、再建 runtime」：``ExecutionRuntime.__init__``
会用当时的全局时钟取 ``start_time``，若先建 runtime 再换时钟，``elapsed_seconds()`` 会因为
两个时钟的纪元差而变成一个巨大的负数，让 deadline 用例因为错误的理由通过。
"""

from __future__ import annotations

from datetime import datetime
from threading import Event

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import get_default_clock, set_default_clock
from axile.executor.execution_runtime import ExecutionRuntime, ExecutionRuntimeBindings
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from tests.unit.executor.test_execution_termination import _active_order, _TerminationTestExecutor

_DEADLINE_START = datetime(2026, 7, 27, 10, 0, 0)


class _AdvancingClock:
    """真正会走时的假时钟：``sleep`` / ``event_wait`` 都推进当前时间戳。"""

    def __init__(self, start_time: datetime) -> None:
        self._current_ts = start_time.timestamp()
        self.sleep_calls: list[float] = []
        self.event_waits: list[float] = []

    def monotonic(self) -> float:
        """返回仿真计时值。"""
        return self.time()

    def time(self) -> float:
        return self._current_ts

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._current_ts += seconds

    def event_wait(self, event: Event, timeout: float) -> bool:
        self.event_waits.append(timeout)
        if timeout > 0:
            self._current_ts += timeout
        return event.is_set()

    def advance(self, seconds: float) -> None:
        """直接推进时间，不计入 sleep/event_wait 记录。"""
        self._current_ts += seconds


class _RecordingAuditSink:
    """记录审计事件的假 sink，用于断言 deadline 事件只写一次。"""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append_event(self, **kwargs: object) -> bool:
        self.events.append(dict(kwargs))
        return True

    def append_artifact(self, **kwargs: object) -> bool:
        _ = kwargs
        return True

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        """返回指定类型的事件；写入侧传的是枚举成员，这里按其 ``value`` 比对。"""
        return [event for event in self.events if getattr(event.get("event_type"), "value", None) == event_type]


class _DeadlineFixture:
    """一次 deadline 场景所需的执行器、runtime 与时钟。"""

    def __init__(
        self,
        *,
        executor: _TerminationTestExecutor,
        runtime: ExecutionRuntime,
        clock: _AdvancingClock,
        audit_sink: _RecordingAuditSink,
    ) -> None:
        self.executor = executor
        self.runtime = runtime
        self.clock = clock
        self.audit_sink = audit_sink

    def session(self, symbol: str = "ag2612") -> ExecutionSession:
        """基于本次 runtime 构造单品种执行会话。"""
        return ExecutionSession(owner=self.executor, runtime=self.runtime, symbol=symbol)


@pytest.fixture(autouse=True)
def restore_clock():
    """每个用例结束时还原全局默认时钟（用例内由 ``_build_deadline_fixture`` 装假时钟）。"""
    previous = get_default_clock()
    yield
    set_default_clock(previous)


def _build_deadline_fixture(
    *,
    timeout: int,
    elapsed: float,
    controller: ExecutionTerminationController | None = None,
    execution_orders: list[UnifiedOrder] | None = None,
) -> _DeadlineFixture:
    """
    装配一个「已经跑了 ``elapsed`` 秒」的 runtime.

    Notes
    -----
    顺序很重要：先 ``set_default_clock``，再构造 executor 与 runtime，最后推进时钟。
    全局时钟由 autouse 的 ``restore_clock`` fixture 在用例结束时还原。
    """
    clock = _AdvancingClock(_DEADLINE_START)
    set_default_clock(clock)  # type: ignore[arg-type]

    executor = _TerminationTestExecutor(execution_orders=execution_orders)
    audit_sink = _RecordingAuditSink()
    runtime = ExecutionRuntime(
        owner=executor,
        bindings=ExecutionRuntimeBindings(
            audit_context={"execution_id": "exec-deadline", "account_id": 1, "algorithm": "TEST"},
            audit_sink=audit_sink,  # type: ignore[arg-type]
            termination_controller=controller,
        ),
    )
    runtime.reset_for_execute(execution_timeout=timeout)
    clock.advance(elapsed)
    return _DeadlineFixture(executor=executor, runtime=runtime, clock=clock, audit_sink=audit_sink)


def _requested_controller(mode: str = "graceful", reason: str = "manual stop") -> ExecutionTerminationController:
    """构造一个已置位的人工 terminate 控制器。"""
    cancel_event = Event()
    cancel_event.set()
    return ExecutionTerminationController(
        cancel_event=cancel_event,
        reason_provider=lambda: reason,
        mode_provider=lambda: mode,
    )


def test_deadline_disabled_when_timeout_non_positive() -> None:
    """``execution_timeout <= 0`` 表示关闭 deadline，跑再久也不触发。"""
    fixture = _build_deadline_fixture(timeout=0, elapsed=10_000.0)

    assert fixture.runtime.deadline_remaining_seconds() is None
    assert fixture.runtime.is_termination_requested() is False
    assert fixture.runtime.get_termination_mode() is None
    assert fixture.runtime.get_termination_reason() is None
    fixture.runtime.handle_termination_checkpoint("ag2612")


def test_deadline_triggers_without_controller() -> None:
    """未绑定 controller（多进程 worker 路径）时 deadline 仍须生效。"""
    fixture = _build_deadline_fixture(timeout=180, elapsed=181.0)

    assert fixture.runtime.termination_controller is None
    assert fixture.runtime.is_termination_requested() is True
    # graceful 而非 cancel_pending：超时是硬中断，不等撤单往返。
    assert fixture.runtime.get_termination_mode() == "graceful"
    assert fixture.runtime.get_termination_reason() == "执行总超时（180s）"


def test_deadline_not_triggered_before_timeout() -> None:
    """额度未耗尽时不应触发终止。"""
    fixture = _build_deadline_fixture(timeout=180, elapsed=179.0)

    assert fixture.runtime.deadline_remaining_seconds() == pytest.approx(1.0)
    assert fixture.runtime.is_termination_requested() is False
    fixture.runtime.handle_termination_checkpoint("ag2612")


def test_deadline_checkpoint_raises_with_timeout_trigger() -> None:
    """超时抛出的异常应标记为 timeout 触发，且模式为不撤单的 graceful。"""
    fixture = _build_deadline_fixture(timeout=180, elapsed=200.0)

    with pytest.raises(ExecutionTerminated) as exc_info:
        fixture.runtime.handle_termination_checkpoint("ag2612")

    assert exc_info.value.trigger == "timeout"
    assert exc_info.value.mode == "graceful"
    assert exc_info.value.reason == "执行总超时（180s）"
    assert exc_info.value.acked_at is not None


def test_deadline_does_not_cancel_pending_orders() -> None:
    """超时是硬中断：立即抛出，不发任何撤单请求。

    这道兜底防的就是渠道挂死；若终止还要先等撤单往返回话，渠道真卡住时 deadline
    也会跟着卡住——兜底被架在了它要兜的东西上。残留挂单交由下一次执行开工前的
    ``cancel_all_orders`` 清理。
    """
    fixture = _build_deadline_fixture(
        timeout=180,
        elapsed=200.0,
        execution_orders=[_active_order("ag2612", "order-1")],
    )
    session = fixture.session()

    with pytest.raises(ExecutionTerminated) as exc_info:
        session.handle_termination_checkpoint()

    assert exc_info.value.trigger == "timeout"
    assert fixture.executor.cancel_attempts == []


def test_deadline_audit_event_emitted_once() -> None:
    """检查点被反复调用时，deadline 的 ACK 审计事件只应写一条。"""
    fixture = _build_deadline_fixture(timeout=180, elapsed=200.0)

    for _attempt in range(5):
        with pytest.raises(ExecutionTerminated):
            fixture.runtime.handle_termination_checkpoint("ag2612")

    acked_events = fixture.audit_sink.events_of_type("execution_termination_acked")
    assert len(acked_events) == 1
    termination = acked_events[0]["details"]["termination"]  # type: ignore[index]
    assert termination["trigger"] == "timeout"
    assert termination["mode"] == "graceful"
    assert termination["execution_timeout"] == 180


def test_runtime_sleep_clamps_to_deadline_and_raises() -> None:
    """片间等待应被钳到剩余额度，并在醒来后立即被检查点截住。"""
    fixture = _build_deadline_fixture(timeout=180, elapsed=170.0)

    with pytest.raises(ExecutionTerminated) as exc_info:
        fixture.runtime.sleep_or_terminate(60.0, "ag2612")

    assert fixture.clock.sleep_calls == [10.0]
    assert exc_info.value.trigger == "timeout"


def test_session_sleep_clamps_to_deadline() -> None:
    """会话级片间等待要被钳到剩余额度，并在醒来后立即抛出（不撤单）。"""
    fixture = _build_deadline_fixture(
        timeout=180,
        elapsed=170.0,
        execution_orders=[_active_order("ag2612", "order-1")],
    )
    session = fixture.session()

    with pytest.raises(ExecutionTerminated) as exc_info:
        session.sleep_or_terminate(60.0)

    assert fixture.clock.sleep_calls == [10.0]
    assert exc_info.value.trigger == "timeout"
    assert fixture.executor.cancel_attempts == []


def test_sleep_with_controller_clamps_to_deadline() -> None:
    """绑定了 controller 时，可中断等待的时长同样受 deadline 钳制。"""
    controller = ExecutionTerminationController(cancel_event=Event())
    fixture = _build_deadline_fixture(timeout=180, elapsed=170.0, controller=controller)

    with pytest.raises(ExecutionTerminated) as exc_info:
        fixture.runtime.sleep_or_terminate(60.0, "ag2612")

    assert fixture.clock.event_waits == [10.0]
    assert exc_info.value.trigger == "timeout"


def test_sleep_without_deadline_keeps_full_wait() -> None:
    """未启用 deadline 时，片间等待行为与改造前一致：等满且不抛。"""
    controller = ExecutionTerminationController(cancel_event=Event())
    fixture = _build_deadline_fixture(timeout=0, elapsed=0.0, controller=controller)

    fixture.runtime.sleep_or_terminate(5.0, "ag2612")

    assert fixture.clock.event_waits == [5.0]


def test_operator_termination_takes_precedence_over_deadline() -> None:
    """人工 terminate 与超时同时成立时，应按人工请求上报 reason/mode/trigger。"""
    controller = _requested_controller(mode="graceful", reason="manual stop")
    fixture = _build_deadline_fixture(timeout=180, elapsed=200.0, controller=controller)

    assert fixture.runtime.get_termination_mode() == "graceful"
    assert fixture.runtime.get_termination_reason() == "manual stop"

    with pytest.raises(ExecutionTerminated) as exc_info:
        fixture.runtime.handle_termination_checkpoint("ag2612")

    assert exc_info.value.trigger == "operator"
    assert exc_info.value.mode == "graceful"
    assert exc_info.value.reason == "manual stop"

    acked_events = fixture.audit_sink.events_of_type("execution_termination_acked")
    assert len(acked_events) == 1
    assert acked_events[0]["details"]["termination"]["trigger"] == "operator"  # type: ignore[index]


def _standard_input(*, execution_timeout: int) -> UnifiedStandardInput:
    """构造一个仅用于校验 deadline 装载的最小标准输入。"""
    return UnifiedStandardInput(
        channel_type=TradeChannel("ctp"),
        account_config=CTPAccountConfig.model_validate(
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
        curr_target={},
        last_target={},
        execution_timeout=execution_timeout,
    )


def test_reset_execution_state_loads_execution_timeout_into_runtime() -> None:
    """执行前的状态重置必须把标准输入里的总超时装进 runtime。"""
    set_default_clock(_AdvancingClock(_DEADLINE_START))  # type: ignore[arg-type]
    executor = _TerminationTestExecutor()

    executor._reset_execution_state(_standard_input(execution_timeout=240))

    assert executor.require_execution_runtime().execution_timeout == 240


def test_operator_cancel_pending_still_cancels_orders() -> None:
    """人工 ``cancel_pending`` 终止不受总超时改动影响，仍要撤掉挂单。

    超时改成不撤单是针对「兜底中断」这一种场景；运维显式要求撤单收尾时，语义必须
    原样保留——否则这次改动会顺手削掉一条无关的能力。
    """
    controller = _requested_controller(mode="cancel_pending", reason="manual stop")
    fixture = _build_deadline_fixture(
        timeout=0,
        elapsed=0.0,
        controller=controller,
        execution_orders=[_active_order("ag2612", "order-1")],
    )
    session = fixture.session()

    with pytest.raises(ExecutionTerminated) as exc_info:
        session.handle_termination_checkpoint()

    assert exc_info.value.trigger == "operator"
    assert exc_info.value.mode == "cancel_pending"
    assert fixture.executor.cancel_attempts == [("ag2612", "order-1")]
