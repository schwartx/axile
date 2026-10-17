"""天勤常驻连接内的订单终态审计与原执行归属。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
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

    def register(self, order_id: str, runtime: ExecutionRuntime | None) -> None:
        """在调用 SDK 报单前保存原执行信息，覆盖同步早到的终态回报。"""
        if runtime is None:
            return
        context = runtime.audit_context
        execution_id = context.get("execution_id")
        account_id = _coerce_int(context.get("account_id"))
        algorithm = context.get("algorithm")
        sink = runtime.audit_sink
        if not execution_id or account_id is None or not algorithm or sink is None:
            return
        origin = _OrderOrigin(str(execution_id), account_id, str(algorithm), sink, runtime.next_audit_seq)
        with self._lock:
            self._origins[order_id] = origin

    def discard(self, order_id: str) -> None:
        """仅当确定没有报单时丢弃预注册的归属。"""
        with self._lock:
            self._origins.pop(order_id, None)

    def emit(self, order: UnifiedOrder) -> bool:
        """写入真实终态；重复或无归属的订单跳过，失败保留归属供对账重试。

        Notes
        -----
        锁覆盖写入和归属消费，保证 SDK 回报与对账并发时仅成功写入一次。
        不使用当前 execution 的上下文或 sink，避免迟到回报串到下一轮。
        成功后释放归属和 runtime 引用，不累计已完成订单的去重集合。
        """
        if not order.order_id or not order.is_completed():
            return False
        with self._lock:
            origin = self._origins.get(order.order_id)
            if origin is None:
                return False
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
            if written:
                del self._origins[order.order_id]
            else:
                self._logger.warning("天勤订单终态审计未写入，保留对账重试 order_id={}", order.order_id)
            return written
