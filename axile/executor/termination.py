"""执行器层的协作式终止抽象."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

TERMINATION_TRIGGER_OPERATOR = "operator"
"""终止来源：人工（或调用方）发起的 terminate 请求。."""

TERMINATION_TRIGGER_TIMEOUT = "timeout"
"""终止来源：执行层总超时兜底。."""


def termination_ack_now() -> str:
    """
    返回终止 ACK 使用的时间字符串.

    Returns
    -------
    str
        本地时间的 ISO 字符串，不带微秒。

    Notes
    -----
    人工终止与总超时两条路径共用这一个取时点，两者写进 ``acked_at`` 的格式才一致；
    该字段会直接落库并在前端展示，格式漂移会让同一列出现两种精度。
    """
    from axile.executor.algorithms.utils.clock import clock_now

    return clock_now().replace(microsecond=0).isoformat()


class ExecutionTerminated(RuntimeError):
    """
    执行线程在检查点响应 terminate 请求或总超时时抛出的异常.

    Attributes
    ----------
    reason : str | None
        终止原因描述。
    mode : str | None
        终止模式；``cancel_pending`` 表示需要先撤挂单再退出。
    cancel_failed_order_ids : list[str]
        尚未确认终态的订单号列表。
    cancel_details : dict[str, str]
        以品种和订单号为键，保留不可撤单或终态未确认的原因。
    acked_at : str | None
        执行线程首次确认终止的时间。
    trigger : str
        终止来源：``operator``（人工请求）或 ``timeout``（总超时兜底）。

    Notes
    -----
    ``trigger`` 与 ``mode`` 是**正交**的两个维度：``mode`` 只回答「要不要撤挂单」，
    ``trigger`` 只回答「是谁终止的」。总超时是硬中断，其 ``mode`` 为 ``graceful``
    （不撤单，残留挂单交由下次执行开工前的 ``cancel_all_orders`` 清理）；人工终止的
    ``mode`` 由调用方指定。因此**不能靠 ``mode`` 反推 ``trigger``**，反之亦然。
    """

    def __init__(
        self,
        *,
        reason: str | None,
        mode: str | None,
        cancel_failed_order_ids: list[str] | None = None,
        acked_at: str | None = None,
        trigger: str = TERMINATION_TRIGGER_OPERATOR,
        forced: bool = False,
        cancel_attempted: bool | None = None,
        cancel_unconfirmed: bool = False,
        cancel_details: dict[str, str] | None = None,
    ) -> None:
        self.reason = reason
        self.mode = mode
        self.cancel_failed_order_ids = list(cancel_failed_order_ids or [])
        self.acked_at = acked_at
        self.trigger = trigger
        self.forced = forced
        self.cancel_attempted = cancel_attempted
        self.cancel_unconfirmed = cancel_unconfirmed
        self.cancel_details = dict(cancel_details or {})
        message_parts = ["execution terminated"]
        if trigger and trigger != TERMINATION_TRIGGER_OPERATOR:
            message_parts.append(f"trigger={trigger}")
        if mode:
            message_parts.append(f"mode={mode}")
        if reason:
            message_parts.append(f"reason={reason}")
        super().__init__(", ".join(message_parts))


@dataclass
class ExecutionTerminationController:
    """协作式终止控制器."""

    cancel_event: threading.Event
    reason_provider: Callable[[], str | None] = field(default=lambda: None)
    mode_provider: Callable[[], str | None] = field(default=lambda: None)
    acknowledge_callback: Callable[[str], None] | None = None
    _ack_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _acked_at: str | None = field(default=None, init=False, repr=False)

    def is_requested(self) -> bool:
        """是否已收到 terminate 请求."""
        return self.cancel_event.is_set()

    def reason(self) -> str | None:
        """读取 terminate 原因."""
        return self.reason_provider()

    def mode(self) -> str | None:
        """读取 terminate 模式."""
        return self.mode_provider()

    @property
    def acked_at(self) -> str | None:
        """执行线程首次确认 terminate 的时间."""
        return self._acked_at

    def acknowledge_if_requested(self) -> bool:
        """在首次观察到 terminate 请求时记录 ACK."""
        if not self.is_requested():
            return False

        with self._ack_lock:
            if self._acked_at is not None:
                return False

            acked_at = termination_ack_now()
            self._acked_at = acked_at
            if self.acknowledge_callback is not None:
                self.acknowledge_callback(acked_at)
            return True


def wait_or_terminate(
    seconds: float,
    *,
    deadline_remaining: float | None,
    cancel_event: threading.Event | None,
    checkpoint: Callable[[], None],
) -> None:
    """
    协作式等待：睡满 ``seconds``，或在收到 terminate 请求 / 触及总超时时立即中断.

    Parameters
    ----------
    seconds : float
        期望等待的秒数；``<= 0`` 时不等待。
    deadline_remaining : float | None
        距离总超时的剩余秒数；``None`` 表示未启用 deadline。
    cancel_event : threading.Event | None
        terminate 请求的唤醒源；``None`` 时退化为不可中断的睡眠（多进程 worker 路径
        不绑 controller，只能靠 deadline 兜底）。
    checkpoint : Callable[[], None]
        终止检查点。调用方决定它是 runtime 级还是 symbol 级——后者会先撤当前品种挂单。

    Raises
    ------
    ExecutionTerminated
        由 ``checkpoint`` 抛出：调用时已收到 terminate 请求或已超时，
        或在等待期间发生上述任一情况。

    Notes
    -----
    启用 deadline 时等待时长会被钳到剩余额度以内，且**醒来后必须无条件再过一次检查点**：
    钳制本身不终止执行，只钳不查等于「睡得短一点然后照常往下跑」。
    """
    # 先响应可能已就绪的请求，避免先睡满再抛。
    checkpoint()
    if seconds <= 0:
        return

    if deadline_remaining is not None:
        seconds = min(seconds, max(deadline_remaining, 0.0))

    from axile.executor.algorithms.utils.clock import get_default_clock

    clock = get_default_clock()
    if cancel_event is not None:
        clock.event_wait(cancel_event, seconds)
    elif seconds > 0:
        clock.sleep(seconds)
    checkpoint()
