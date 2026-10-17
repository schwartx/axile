"""天勤常驻连接内的订单终态审计与原执行归属。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import TYPE_CHECKING

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.abstract_executor.support import _coerce_int

if TYPE_CHECKING:
    from loguru import Logger

    from axile.executor.audit import ExecutionAuditSink
    from axile.executor.execution_runtime import ExecutionRuntime
    from axile.executor.models.unified_order import UnifiedOrder


@dataclass(frozen=True, slots=True)
class _OrderOrigin:
    """提交前冻结归属和输出 sink，不读取下一轮 execution 的绑定。"""

    execution_id: str
    account_id: int
    algorithm: str
    sink: ExecutionAuditSink
    next_seq: Callable[[], int]


class TQOrderTerminalAudit:
    """在回报与主动对账之间共享订单归属，成功写入后原子消费一次。"""

    def __init__(self, logger: Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._origins: dict[str, _OrderOrigin] = {}
        self._pending: set[str] = set()
        self._condition = threading.Condition(self._lock)
        self._queue: Queue[UnifiedOrder | None] = Queue()
        self._worker: threading.Thread | None = None
        self._closed = False

    def register(
        self, order_id: str, runtime: ExecutionRuntime | None, audit_context: dict[str, object] | None = None
    ) -> None:
        """在调用 SDK 报单前保存原执行信息，覆盖同步早到的终态回报。"""
        if runtime is None:
            return
        context = audit_context or runtime.audit_context
        execution_id = context.get("execution_id")
        account_id = _coerce_int(context.get("account_id"))
        algorithm = context.get("algorithm")
        sink = runtime.audit_sink
        if not execution_id or account_id is None or not algorithm or sink is None:
            return
        origin = _OrderOrigin(str(execution_id), account_id, str(algorithm), sink, runtime.next_audit_seq)
        with self._lock:
            if not self._closed:
                self._origins[order_id] = origin

    def discard(self, order_id: str) -> None:
        """仅当确定没有报单时丢弃预注册的归属。"""
        with self._lock:
            self._origins.pop(order_id, None)

    def emit(self, order: UnifiedOrder) -> bool:
        """只排队真实终态，不在 SDK owner 线程或调用线程执行审计 IO。"""
        if not order.order_id or not order.is_completed():
            return False
        with self._lock:
            if self._closed or order.order_id not in self._origins or order.order_id in self._pending:
                return False
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="tq-terminal-audit", daemon=True)
                self._worker.start()
            self._pending.add(order.order_id)
            self._queue.put(order.model_copy(deep=True))
        return True

    def flush(self, timeout: float = 5) -> bool:
        """等待当前已排队写入完成；用于离线验证，不在交易回报路径调用。"""
        with self._condition:
            return self._condition.wait_for(lambda: not self._pending, timeout)

    def close(self) -> None:
        """非阻塞结束审计工作线程；已排队事件在后台完成后释放引用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._worker is not None:
                self._queue.put(None)
            else:
                self._origins.clear()

    def _run(self) -> None:
        while (order := self._queue.get()) is not None:
            with self._lock:
                origin = self._origins.get(order.order_id)
            written = self._write(order, origin) if origin is not None else False
            with self._condition:
                if written:
                    self._origins.pop(order.order_id, None)
                self._pending.discard(order.order_id)
                self._condition.notify_all()
        with self._lock:
            self._origins.clear()

    def _write(self, order: UnifiedOrder, origin: _OrderOrigin) -> bool:
        """独立线程写入；失败保留原归属，后续回报或快照可重新排队。"""
        try:
            written = origin.sink.append_event(
                execution_id=origin.execution_id,
                account_id=origin.account_id,
                channel=TradeChannel.TQ,
                algorithm=origin.algorithm,
                event_type=ExecutionEventType.ORDER_TERMINAL,
                status=ExecutionEventStatus.SUCCESS,
                reason_family=ExecutionReasonFamily.EXCHANGE,
                reason_code="COMMON.ORDER_TERMINAL",
                symbol=order.symbol,
                order_id=order.order_id,
                seq=origin.next_seq(),
                details={
                    "order": {
                        "direction": str(order.direction),
                        "order_type": str(order.order_type),
                        "status": order.status,
                        "terminal_status": order.status,
                        "volume": float(order.volume),
                        "filled_volume": float(order.filled_volume),
                        "remaining_volume": float(order.remaining_volume),
                        "avg_price": float(order.avg_price),
                        "filled_ratio": float(order.filled_ratio),
                        "offset": order.extra.get("offset"),
                    },
                    "exchange": {
                        "exchange_id": order.extra.get("exchange_id"),
                        "last_msg": order.extra.get("last_msg"),
                    },
                },
            )
        except Exception:
            self._logger.exception("天勤订单终态审计写入失败 order_id={}", order.order_id)
            return False
        if not written:
            self._logger.warning("天勤订单终态审计未写入，保留对账重试 order_id={}", order.order_id)
        return written
