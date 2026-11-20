"""常驻连接内的订单终态审计与原执行归属。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import TYPE_CHECKING

from axile.common.serialization import copy_data
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.abstract_executor.support import _coerce_int

if TYPE_CHECKING:
    from loguru import Logger

    from axile.common.trade_channel import TradeChannel
    from axile.executor.audit import ExecutionAuditSink
    from axile.executor.execution_runtime import ExecutionRuntime
    from axile.executor.models.unified_order import UnifiedOrder


def order_terminal_details(order: UnifiedOrder) -> dict[str, object]:
    """将已知终态转成共享的逐单审计证据。"""
    return {
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
    }


@dataclass(frozen=True, slots=True)
class _OrderOrigin:
    """提交前冻结归属和输出 sink，不读取下一轮 execution 的绑定。"""

    execution_id: str
    account_id: int
    algorithm: str
    sink: ExecutionAuditSink
    next_seq: Callable[[], int]
    symbol: str | None


class OrderTerminalAudit:
    """回报与主动对账共享订单归属，按累计成交量去重并记录迟到的成交修正。"""

    def __init__(self, logger: Logger, channel: TradeChannel) -> None:
        self._logger = logger
        self._channel = channel
        self._lock = threading.Lock()
        self._origins: dict[str, _OrderOrigin] = {}
        self._pending: dict[str, float] = {}
        self._corrections: dict[str, tuple[_OrderOrigin, float]] = {}
        self._condition = threading.Condition(self._lock)
        self._queue: Queue[UnifiedOrder | None] = Queue()
        self._worker: threading.Thread | None = None
        self._closed = False

    def register(
        self,
        order_id: str,
        runtime: ExecutionRuntime | None,
        audit_context: dict[str, object] | None = None,
        *,
        symbol: str | None = None,
    ) -> bool:
        """将已知订单标识绑定到原执行。"""
        if runtime is None:
            return False
        context = audit_context or runtime.audit_context
        execution_id = context.get("execution_id")
        account_id = _coerce_int(context.get("account_id"))
        algorithm = context.get("algorithm")
        sink = runtime.audit_sink
        if not execution_id or account_id is None or not algorithm or sink is None:
            return False
        origin = _OrderOrigin(str(execution_id), account_id, str(algorithm), sink, runtime.next_audit_seq, symbol)
        with self._lock:
            if self._closed:
                return False
            self._corrections.pop(order_id, None)
            self._origins[order_id] = origin
            return True

    def discard(self, order_id: str) -> None:
        """仅当确定没有报单时丢弃预注册的归属。"""
        with self._lock:
            self._origins.pop(order_id, None)
            self._corrections.pop(order_id, None)

    def emit(self, order: UnifiedOrder) -> bool:
        """只排队真实终态，不在 SDK owner 线程或调用线程执行审计 IO。"""
        if not order.order_id or not order.is_completed():
            return False
        with self._lock:
            origin = self._origins.get(order.order_id)
            previous = self._corrections.get(order.order_id)
            if previous is not None:
                origin, written_fill = previous
                if order.filled_volume <= written_fill:
                    return False
            if (
                self._closed
                or origin is None
                or origin.symbol not in {None, order.symbol}
                or order.filled_volume <= self._pending.get(order.order_id, -1)
            ):
                return False
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, name=f"{str(self._channel)}-terminal-audit", daemon=True
                )
                self._worker.start()
            self._pending[order.order_id] = order.filled_volume
            self._queue.put(copy_data(order, deep=True))
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
                self._corrections.clear()

    def _run(self) -> None:
        while (order := self._queue.get()) is not None:
            with self._lock:
                origin = self._origins.get(order.order_id)
                previous = self._corrections.get(order.order_id)
                if previous is not None:
                    origin = previous[0]
            written = self._write(order, origin) if origin is not None else False
            with self._condition:
                if written and origin is not None:
                    self._origins.pop(order.order_id, None)
                    if order.filled_volume < order.volume:
                        self._corrections[order.order_id] = (origin, order.filled_volume)
                    else:
                        self._corrections.pop(order.order_id, None)
                if self._pending.get(order.order_id) == order.filled_volume:
                    del self._pending[order.order_id]
                self._condition.notify_all()
        with self._lock:
            self._origins.clear()
            self._corrections.clear()

    def _write(self, order: UnifiedOrder, origin: _OrderOrigin) -> bool:
        """独立线程写入；失败保留原归属，后续回报或快照可重新排队。"""
        try:
            written = origin.sink.append_event(
                execution_id=origin.execution_id,
                account_id=origin.account_id,
                channel=self._channel,
                algorithm=origin.algorithm,
                event_type=ExecutionEventType.ORDER_TERMINAL,
                status=ExecutionEventStatus.SUCCESS,
                reason_family=ExecutionReasonFamily.EXCHANGE,
                reason_code="COMMON.ORDER_TERMINAL",
                symbol=order.symbol,
                order_id=order.order_id,
                seq=origin.next_seq(),
                details=order_terminal_details(order),
            )
        except Exception:
            self._logger.exception("{} 订单终态审计写入失败 order_id={}", str(self._channel), order.order_id)
            return False
        if not written:
            self._logger.warning("{} 订单终态审计未写入，保留对账重试 order_id={}", str(self._channel), order.order_id)
        return written
