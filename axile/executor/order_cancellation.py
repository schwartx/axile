"""撤单操作结果与订单终态分别记录；规则限制不属于通道故障。"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.termination import ExecutionTerminated


class CancelNotAllowedError(RuntimeError):
    """当前禁止撤单且请求未发送；原委托仍须跟踪到真实终态。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_error = message


class CancelRequestStatus(StrEnum):
    """渠道撤单操作结果，不描述订单是否终结。"""

    RESTRICTED = "restricted"
    SUBMITTED = "submitted"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class OrderCancellation:
    """一次撤单操作及其独立的终态核实结果。"""

    request_status: CancelRequestStatus
    reason: str
    terminal_order: UnifiedOrder | None = None
    query_error: str | None = None


@dataclass
class CancellationResult:
    """逐单事实为唯一来源；未确认终态不等于撤单失败。"""

    orders: dict[tuple[str, str], OrderCancellation] = field(default_factory=dict)

    @property
    def unconfirmed(self) -> dict[tuple[str, str], str]:
        """返回仍未确认终态的逐单说明。"""
        return {
            key: item.query_error or item.reason for key, item in self.orders.items() if item.terminal_order is None
        }

    @property
    def terminal_orders(self) -> list[UnifiedOrder]:
        """返回已核实的真实终态。"""
        return [item.terminal_order for item in self.orders.values() if item.terminal_order is not None]

    @property
    def attempted(self) -> bool:
        """规则禁止且未发送的请求不计为尝试。"""
        return any(item.request_status != CancelRequestStatus.RESTRICTED for item in self.orders.values())

    @property
    def only_restricted(self) -> bool:
        """是否仅因规则受限保留订单，且没有查询故障。"""
        pending = [item for item in self.orders.values() if item.terminal_order is None]
        return bool(pending) and all(
            item.request_status == CancelRequestStatus.RESTRICTED and not item.query_error for item in pending
        )

    @property
    def details(self) -> dict[str, str]:
        """返回可读的未决原因。"""
        return {f"{symbol}:{order_id}": reason for (symbol, order_id), reason in self.unconfirmed.items()}

    @property
    def outcomes(self) -> dict[str, dict[str, object]]:
        """生成跨 worker 与审计使用的结构化事实。"""
        return {
            f"{symbol}:{order_id}": {
                "request_status": item.request_status.value,
                "reason": item.reason,
                "terminal": item.terminal_order is not None,
                "query_error": item.query_error,
            }
            for (symbol, order_id), item in self.orders.items()
        }


def _raise_if_fatal(exc: Exception) -> None:
    if isinstance(exc, (MemoryError, ExecutionTerminated)) or getattr(exc, "requires_session_recovery", False):
        raise exc


def cancel_orders(
    orders: Iterable[UnifiedOrder],
    *,
    cancel: Callable[[str, str], bool],
    reconcile: Callable[[str, str], UnifiedOrder | None],
) -> CancellationResult:
    """逐笔请求并核实；异常无法证明请求是否到达上游，因而记为未知。"""
    result = CancellationResult()
    for order in orders:
        try:
            submitted = cancel(order.symbol, order.order_id)
            item = OrderCancellation(
                CancelRequestStatus.SUBMITTED if submitted else CancelRequestStatus.FAILED,
                "撤单请求已提交，订单终态尚未确认" if submitted else "撤单失败，订单终态尚未确认",
            )
        except CancelNotAllowedError as exc:
            item = OrderCancellation(CancelRequestStatus.RESTRICTED, exc.execution_error)
        except Exception as exc:
            _raise_if_fatal(exc)
            item = OrderCancellation(
                CancelRequestStatus.UNKNOWN, getattr(exc, "execution_error", None) or "撤单请求结果未知"
            )
        result.orders[(order.symbol, order.order_id)] = item
        try:
            terminal = reconcile(order.symbol, order.order_id)
        except Exception as exc:
            _raise_if_fatal(exc)
            terminal = None
            item.query_error = "订单终态查询失败"
        if (
            terminal is not None
            and terminal.order_id == order.order_id
            and terminal.symbol == order.symbol
            and terminal.is_completed()
        ):
            item.terminal_order = terminal
    return result
