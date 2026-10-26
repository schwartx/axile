"""单次 execution 的运行时状态容器.

这里是 execution 级可变状态的权威来源，包含：
- 审计上下文与审计序号
- 账户控制 guard 与 terminate controller
- execution 共享查询 runtime
- 本次执行的 start_time / memory
- 本次执行的总超时（deadline）额度

Notes
-----
**执行层总超时（deadline）的语义边界**：``execution_timeout`` 是「一次执行最多跑多久」的
兜底中断，与算法级的 ``algorithm.params.max_wait_seconds``（单个 symbol 等成交的时长）
不是一个层次。它在各个 terminate 检查点被观测。

它是**硬中断，不是有序收尾**：到点即抛，**不撤挂单**（mode 报 ``graceful``）。这一点是
刻意的——超时要防的正是渠道挂死，若终止还要先等撤单往返回话，兜底就被架在了它要兜的
东西上：渠道真卡住时 deadline 也跟着卡住。残留挂单由下一次执行开工前的
``cancel_all_orders`` 清理，或按审计里的订单号人工处理。人工 terminate 不受影响，
仍按调用方指定的 mode（``cancel_pending`` 时照常撤单）。

- 保证：超时后不再开始新工作，且我们自己写的协作等待（``order_tracker`` 等待循环、
  ``sleep_or_terminate`` 片间等待、编排层的调度边界）会立即中止。
- **不保证** ``execute()`` 在 deadline 当刻返回：按品种并行调度用的
  ``ThreadPoolExecutor`` 上下文退出时会等所有兄弟线程各自跑到下一个检查点，
  deadline 是「开始解绑」的时刻而非「已经返回」的时刻。
- **不保证**能中断渠道 SDK 内部的阻塞或死循环——那里没有我们的检查点。真要覆盖这种
  情况，只能靠进程级隔离与强杀。
- **不覆盖**执行器构造与连接建立阶段：计时零点在 :meth:`reset_for_execute`，
  而它在 ``execute()`` 内、连接就绪之后才被调用。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from axile.domain.execution import (
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
    ExecutionTerminateMode,
)
from axile.executor.abstract_executor.support import _coerce_int
from axile.executor.account_control.decorators import run_controlled_call
from axile.executor.account_control.models import (
    AccountControlCounterDeltaWrite,
    AccountControlEventWrite,
)
from axile.executor.algorithms.utils import clock_now
from axile.executor.audit import ExecutionAuditSink
from axile.executor.execution_query_runtime import ExecutionQueryRuntime
from axile.executor.termination import (
    TERMINATION_TRIGGER_OPERATOR,
    TERMINATION_TRIGGER_TIMEOUT,
    ExecutionTerminated,
    ExecutionTerminationController,
    termination_ack_now,
    wait_or_terminate,
)

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor, AuditContext, OrderAuditMetadata
    from axile.executor.account_control.guard import AccountControlGuard
    from axile.executor.models.unified_order import TradeRecord, UnifiedOrder


@dataclass(slots=True)
class ExecutionRuntimeBindings:
    """创建或同步 execution runtime 时注入的外部绑定."""

    audit_context: dict[str, object] = field(default_factory=dict)
    audit_sink: ExecutionAuditSink | None = None
    account_control_guard: AccountControlGuard | None = None
    termination_controller: ExecutionTerminationController | None = None


@dataclass(frozen=True, slots=True)
class _ActiveTermination:
    """
    当前生效的终止视图.

    Attributes
    ----------
    trigger : str
        终止来源：``operator`` 或 ``timeout``。
    mode : str | None
        终止模式，决定下游是否撤挂单。
    reason : str | None
        终止原因描述。

    Notes
    -----
    这是一个**只读派生视图**，不是新的状态持有者：人工终止的真实状态在 controller 里
    （而 controller 自身也只是对服务端 ``_execution_tasks`` 的读透传），总超时则由
    ``start_time`` 与 ``execution_timeout`` 现算。把两个来源在同一处收敛成同一形状，
    是为了让各个终止入口不必各自重复一遍优先级判定。
    """

    trigger: str
    mode: str | None
    reason: str | None


class ExecutionRuntime:
    """单次 execution 的可变状态中心."""

    def __init__(self, *, owner: AbstractExecutor, bindings: ExecutionRuntimeBindings) -> None:
        self.owner = owner
        self.channel_type = owner.channel_type
        self.logger = owner.logger
        self.bindings = bindings
        self.start_time = clock_now()
        # 默认 0（不启用）：只有 execute() 里的 reset_for_execute 会装入真实额度，
        # 因此绕过 execute() 直接构造 runtime 的路径（测试、只读查询）天然不受 deadline 影响。
        self.execution_timeout = 0
        self.memory: dict[str, object] = {}
        self.audit_context: AuditContext = dict(bindings.audit_context)
        self._audit_seq = 0
        self._audit_seq_lock = threading.Lock()
        self._order_audit_metadata: dict[str, OrderAuditMetadata] = {}
        self._order_audit_metadata_lock = threading.Lock()
        self._deadline_acked_at: str | None = None
        self._deadline_ack_lock = threading.Lock()
        self._execution_query_runtime = owner._build_execution_query_runtime()

    def sync_bindings(self, bindings: ExecutionRuntimeBindings | None = None) -> None:
        """将最新 bindings 同步到当前 runtime."""
        if bindings is not None:
            self.bindings = bindings
        self.audit_context = dict(self.bindings.audit_context)

    def reset_for_execute(self, *, execution_timeout: int) -> None:
        """
        在真正执行前重置本次运行时的易失状态，但保留既有审计序列.

        Parameters
        ----------
        execution_timeout : int
            本次执行的总超时秒数；``<= 0`` 表示不启用 deadline。必填：
            该值决定本次执行的兜底额度，不给默认是为了逼调用方明确表态。

        Notes
        -----
        ``start_time`` 与 ``execution_timeout`` 必须在同一次调用里一起落地：
        二者共同构成 deadline，分开设置会留出「新执行的起点配旧执行的额度」的窗口。
        """
        self.start_time = clock_now(tz=self.start_time.tzinfo)
        self.execution_timeout = int(execution_timeout)
        self.memory = {}
        with self._order_audit_metadata_lock:
            self._order_audit_metadata = {}
        with self._deadline_ack_lock:
            self._deadline_acked_at = None
        self._execution_query_runtime = self.owner._build_execution_query_runtime()

    def elapsed_seconds(self) -> float:
        """返回当前 execution 已耗时秒数."""
        return (clock_now(tz=self.start_time.tzinfo) - self.start_time).total_seconds()

    @property
    def account_control_guard(self) -> AccountControlGuard | None:
        """当前 runtime 绑定的账户控制 guard."""
        return self.bindings.account_control_guard

    @property
    def termination_controller(self) -> ExecutionTerminationController | None:
        """当前 runtime 绑定的协作式终止控制器."""
        return self.bindings.termination_controller

    @property
    def audit_sink(self) -> ExecutionAuditSink | None:
        """当前 runtime 绑定的 execution 审计输出 sink."""
        return self.bindings.audit_sink

    def get_execution_query_runtime(self) -> ExecutionQueryRuntime:
        """返回当前 runtime 持有的 execution 共享查询运行时."""
        return self._execution_query_runtime

    def run_controlled_shared_fetch[R](
        self,
        *,
        operation: str,
        shared_query_key: tuple[str, ...],
        query_scope: str,
        fetcher: Callable[[], R],
        symbol: str | None = None,
        metadata: Mapping[str, object] | None = None,
        success_outcome: str = "fetched",
    ) -> R:
        """执行 execution 内部共享查询，并仅对真实 leader fetch 记一次账户控制事件."""
        resolved_metadata: dict[str, object] = {
            "operation": operation,
            "shared_query_key": "::".join(shared_query_key),
            "query_scope": query_scope,
        }
        execution_id = getattr(self.account_control_guard, "execution_id", None)
        if execution_id is not None:
            resolved_metadata["execution_id"] = str(execution_id)
        if metadata is not None:
            resolved_metadata.update({str(key): value for key, value in metadata.items()})

        return run_controlled_call(
            guard=self.account_control_guard,
            operation=operation,
            symbol=symbol,
            metadata=resolved_metadata,
            call=fetcher,
            success_outcome=success_outcome,
        )

    def get_pending_orders_for_execution(self, symbol: str) -> list[UnifiedOrder]:
        """execution-internal 挂单查询入口."""
        return self._execution_query_runtime.get_pending_orders_for_symbol(symbol)

    def query_trades_for_execution(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """execution-internal 成交查询入口."""
        return self._execution_query_runtime.get_trades_for_order(symbol, order_id)

    def export_account_control_records(
        self,
    ) -> tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]:
        """导出当前 runtime 累积的账户控制记录."""
        guard = self.account_control_guard
        if guard is None:
            return [], []
        return guard.flush_records()

    def deadline_remaining_seconds(self) -> float | None:
        """
        返回距离本次 execution 总超时的剩余秒数.

        Returns
        -------
        float | None
            剩余秒数（可为负，表示已超时）；未启用 deadline 时返回 ``None``。

        Notes
        -----
        返回剩余量而非布尔值，是为了让 :meth:`sleep_or_terminate` 能把片间等待钳到
        deadline 之内——只有布尔值拿不到钳制所需的时长。
        """
        if self.execution_timeout <= 0:
            return None
        return float(self.execution_timeout) - self.elapsed_seconds()

    def _current_termination(self) -> _ActiveTermination | None:
        """
        以统一优先级判定当前是否处于终止状态.

        Returns
        -------
        _ActiveTermination | None
            当前生效的终止；未终止时返回 ``None``。

        Notes
        -----
        人工 terminate 优先于总超时：前者携带调用方明确指定的 mode 与 reason，后者只是兜底。

        deadline 判定**不能**放在「无 controller 就早返回」之后：多进程 worker 路径根本不绑
        controller，那样会让总超时在该渠道上完全失效。这里没有任何按 controller 的早返回，
        两个来源各自独立判定。
        """
        controller = self.termination_controller
        if controller is not None and controller.is_requested():
            return _ActiveTermination(
                trigger=TERMINATION_TRIGGER_OPERATOR,
                mode=controller.mode(),
                reason=controller.reason(),
            )

        remaining = self.deadline_remaining_seconds()
        if remaining is None or remaining > 0.0:
            return None

        # 总超时是硬中断，不是「有序收尾」：它要防的就是渠道挂死，若还要等撤单往返回话，
        # 兜底本身就被架在了它要兜的东西上——渠道真卡住时 deadline 也跟着卡住。
        # 因此报 graceful，让 ExecutionSession 跳过撤单直接抛出；残留挂单交由下一次执行
        # 开工前的 cancel_all_orders 清理，或按审计里的订单号人工处理。
        return _ActiveTermination(
            trigger=TERMINATION_TRIGGER_TIMEOUT,
            mode=ExecutionTerminateMode.GRACEFUL.value,
            reason=f"执行总超时（{self.execution_timeout}s）",
        )

    def is_termination_requested(self) -> bool:
        """当前 execution 是否已收到 terminate 请求，或已超过总超时."""
        return self._current_termination() is not None

    def get_termination_mode(self) -> str | None:
        """读取当前 terminate 模式；总超时用 ``graceful``（不撤单）."""
        active = self._current_termination()
        return None if active is None else active.mode

    def get_termination_reason(self) -> str | None:
        """读取当前 terminate 原因."""
        active = self._current_termination()
        return None if active is None else active.reason

    def next_audit_seq(self) -> int:
        """获取本次 execution 内单调递增的审计序号."""
        with self._audit_seq_lock:
            self._audit_seq += 1
            return self._audit_seq

    def register_order_audit_metadata(self, order_id: str, metadata: OrderAuditMetadata) -> None:
        """为订单注册审计关联元数据."""
        with self._order_audit_metadata_lock:
            self._order_audit_metadata[str(order_id)] = dict(metadata)

    def get_order_audit_metadata(self, order_id: str) -> OrderAuditMetadata:
        """获取订单的审计关联元数据."""
        with self._order_audit_metadata_lock:
            return dict(self._order_audit_metadata.get(str(order_id), {}))

    def emit_audit_event(
        self,
        *,
        event_type: object,
        status: object,
        reason_family: object,
        reason_code: str,
        symbol: str | None = None,
        intent_id: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        ts_exchange: str | None = None,
        seq: int | None = None,
        details: dict[str, object] | None = None,
        audit_context: AuditContext | None = None,
    ) -> bool:
        """写入执行审计事件；未配置审计上下文时静默跳过."""
        context = audit_context or self.audit_context
        execution_id = context.get("execution_id")
        account_id = _coerce_int(context.get("account_id"))
        algorithm = context.get("algorithm")
        audit_sink = self.audit_sink
        if not execution_id or account_id is None or not algorithm or audit_sink is None:
            return False

        return audit_sink.append_event(
            execution_id=execution_id,
            account_id=int(account_id),
            channel=self.channel_type,
            algorithm=str(algorithm),
            event_type=event_type,
            status=status,
            reason_family=reason_family,
            reason_code=reason_code,
            symbol=symbol,
            intent_id=intent_id,
            order_id=order_id,
            client_order_id=client_order_id,
            ts_exchange=ts_exchange,
            seq=seq if seq is not None else self.next_audit_seq(),
            details=details or {},
        )

    def emit_audit_artifact(
        self,
        *,
        artifact_type: object,
        content: dict[str, object],
        audit_context: AuditContext | None = None,
    ) -> bool:
        """写入执行审计附件；未配置审计上下文时静默跳过."""
        context = audit_context or self.audit_context
        execution_id = context.get("execution_id")
        audit_sink = self.audit_sink
        if not execution_id or audit_sink is None:
            return False

        return audit_sink.append_artifact(
            execution_id=str(execution_id),
            artifact_type=artifact_type,
            content=content,
        )

    def handle_termination_checkpoint(self, symbol: str | None = None) -> None:
        """
        在 execution 检查点响应 terminate 请求或总超时.

        Parameters
        ----------
        symbol : str | None, optional
            当前检查点所属品种，用于审计事件标注。

        Raises
        ------
        ExecutionTerminated
            已收到人工 terminate 请求，或本次执行已超过总超时。

        Notes
        -----
        终止的判定与优先级全部收敛在 :meth:`_current_termination`；这里只负责
        「首次观测写一条 ACK 审计、然后抛出」。区分人工与超时一律读 ``trigger``。
        """
        active = self._current_termination()
        if active is None:
            return

        raise ExecutionTerminated(
            reason=active.reason,
            mode=active.mode,
            acked_at=self._acknowledge(active, symbol),
            trigger=active.trigger,
        )

    def _acknowledge(self, active: _ActiveTermination, symbol: str | None) -> str | None:
        """
        记录终止的首次 ACK，并在首次观测时写一条审计事件.

        Parameters
        ----------
        active : _ActiveTermination
            当前生效的终止。
        symbol : str | None
            当前检查点所属品种，用于审计事件标注。

        Returns
        -------
        str | None
            本次 execution 该终止来源的 ACK 时间；后续观测复用首次的取值。

        Notes
        -----
        两个来源的 ACK 闩形状相同、语义不同，故各自留一份：controller 那份在首次确认时
        还会回调服务端把状态推进到 ``TERMINATING``，总超时这份没有对应的对外副作用。

        去重是必需的：检查点在订单等待循环里每轮被调用多次，再乘以按品种并行的线程数，
        不去重会让一次超时喷出几十条同样的审计事件、且各线程各报一个时间。总超时这份
        用「``acked_at`` 是否已落地」本身充当幂等标记，而不是另立一个布尔位——后者可由
        前者推导，两份状态迟早会不同步。
        """
        if active.trigger == TERMINATION_TRIGGER_TIMEOUT:
            with self._deadline_ack_lock:
                if self._deadline_acked_at is not None:
                    return self._deadline_acked_at
                acked_at = termination_ack_now()
                self._deadline_acked_at = acked_at

            self._emit_termination_acked_event(
                active,
                symbol=symbol,
                acked_at=acked_at,
                extra_details={
                    "elapsed_seconds": round(self.elapsed_seconds(), 3),
                    "execution_timeout": self.execution_timeout,
                },
            )
            return acked_at

        controller = self.termination_controller
        if controller is None:
            return None

        if controller.acknowledge_if_requested():
            self._emit_termination_acked_event(active, symbol=symbol, acked_at=controller.acked_at)
        return controller.acked_at

    def _emit_termination_acked_event(
        self,
        active: _ActiveTermination,
        *,
        symbol: str | None,
        acked_at: str | None,
        extra_details: dict[str, object] | None = None,
    ) -> None:
        """
        写入 terminate 确认审计事件.

        Notes
        -----
        人工终止与总超时**共用** ``execution_termination_acked`` 事件类型，区分放在
        ``details.termination.trigger``：前端按事件类型做穷举渲染，新增事件类型会掉进
        兜底分支显示英文原串。
        """
        termination_details: dict[str, object] = {
            "trigger": active.trigger,
            "mode": active.mode,
            "reason": active.reason,
            "symbol": symbol,
            "acked_at": acked_at,
        }
        if extra_details is not None:
            termination_details.update(extra_details)

        self.emit_audit_event(
            event_type=ExecutionEventType.EXECUTION_TERMINATION_ACKED,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_TERMINATION_ACKED",
            symbol=symbol,
            details={"termination": termination_details},
        )

    def sleep_or_terminate(self, seconds: float, symbol: str | None = None) -> None:
        """协作式等待：睡满 ``seconds``，或在收到 terminate 请求 / 触及总超时时立即中断.

        用于算法片间等待，替代纯阻塞 ``clock.sleep``。等待期间若 ``cancel_event``
        被置位，则立即唤醒并交由 :meth:`handle_termination_checkpoint` 记录 ACK、
        执行 ``cancel_pending`` 收尾并抛出 :class:`ExecutionTerminated`，从而让终止
        在片间即时生效，而非睡满整个间隔才被观测。

        启用 deadline 时，等待时长会被钳到剩余额度以内，并在醒来后重新过一次检查点，
        避免「一个长片间隔把总超时整段睡过去」。

        Parameters
        ----------
        seconds : float
            期望等待的秒数；``<= 0`` 时不等待。
        symbol : str | None, optional
            当前检查点所属品种，用于审计事件标注。

        Raises
        ------
        ExecutionTerminated
            调用时已收到 terminate 请求或已超时，或在等待期间发生上述任一情况。
        """
        controller = self.termination_controller
        wait_or_terminate(
            seconds,
            deadline_remaining=self.deadline_remaining_seconds(),
            cancel_event=None if controller is None else controller.cancel_event,
            checkpoint=lambda: self.handle_termination_checkpoint(symbol),
        )
