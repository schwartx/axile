"""撤单提交与订单终态分开确认，逐笔保留未完成清理的原因。"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.termination import ExecutionTerminated


class CancelNotAllowedError(RuntimeError):
    """当前禁止撤单且请求未发送；原委托仍须跟踪到真实终态。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_error = message


@dataclass
class CancellationResult:
    """一次批量清理的真实终态与尚未确认的逐笔原因。"""

    unconfirmed: dict[tuple[str, str], str] = field(default_factory=dict)
    terminal_orders: list[UnifiedOrder] = field(default_factory=list)
    attempted: bool = False

    @property
    def details(self) -> dict[str, str]:
        """返回可跨 worker 传递并落入现有审计记录的原因。"""
        return {f"{symbol}:{order_id}": reason for (symbol, order_id), reason in self.unconfirmed.items()}


def _raise_if_fatal(exc: Exception) -> None:
    if isinstance(exc, (MemoryError, ExecutionTerminated)) or getattr(exc, "requires_session_recovery", False):
        raise exc


def cancel_orders(
    orders: Iterable[UnifiedOrder],
    *,
    cancel: Callable[[str, str], bool],
    reconcile: Callable[[str, str], UnifiedOrder | None],
) -> CancellationResult:
    """逐笔请求撤单并核实终态，不等待、不换单，单笔受限不阻断其他订单。"""
    result = CancellationResult()
    for order in orders:
        reason = "撤单请求已提交，订单终态尚未确认"
        try:
            submitted = cancel(order.symbol, order.order_id)
            result.attempted = True
            if not submitted:
                reason = "撤单未确认成功，订单终态尚未确认"
        except CancelNotAllowedError as exc:
            reason = exc.execution_error
        except Exception as exc:
            _raise_if_fatal(exc)
            result.attempted = True
            reason = getattr(exc, "execution_error", None) or "撤单未确认成功，订单终态尚未确认"

        # 即使撤单受限或失败，原单也可能已经成交；只有真实终态才能解除保护。
        try:
            terminal = reconcile(order.symbol, order.order_id)
        except Exception as exc:
            _raise_if_fatal(exc)
            terminal = None
        if (
            terminal is not None
            and terminal.order_id == order.order_id
            and terminal.symbol == order.symbol
            and terminal.is_completed()
        ):
            result.terminal_orders.append(terminal)
        else:
            result.unconfirmed[(order.symbol, order.order_id)] = reason
    return result
