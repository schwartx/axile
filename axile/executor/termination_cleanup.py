"""只允许当前执行、当前品种的有界终止清理，不解除新单门禁。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.termination import ExecutionTerminated


@dataclass(frozen=True)
class _CleanupScope:
    owner: object
    guard: object | None
    symbol: str
    deadline: float


@dataclass
class TerminationCleanupResult:
    """清理查询、请求失败与尚未确认的终态分别保留。"""

    query_complete: bool = False
    attempted: bool = False
    failed_order_ids: list[str] = field(default_factory=list)
    unconfirmed: bool = False


_scope: ContextVar[_CleanupScope | None] = ContextVar("termination_cleanup", default=None)


@contextmanager
def termination_cleanup_scope(
    owner: object, guard: object | None, symbol: str, *, seconds: float = 4
) -> Iterator[None]:
    """绑定线程／任务局部身份；嵌套不能延长清理期限。"""
    deadline = clock_monotonic() + seconds
    previous = _scope.get()
    if previous is not None:
        deadline = min(deadline, previous.deadline)
    token = _scope.set(_CleanupScope(owner, guard, symbol, deadline))
    try:
        yield
    finally:
        _scope.reset(token)


def cleanup_query_allowed(owner: object) -> bool:
    """原生只读查询可继续，但仍受清理超时和渠道连接校验约束。"""
    scope = _scope.get()
    if scope is None or scope.owner is not owner:
        return False
    if clock_monotonic() >= scope.deadline:
        raise TimeoutError("终止清理超过时间预算")
    return True


def cleanup_guard_allowed(guard: object, operation: str, symbol: str | None) -> bool:
    """清理只使用同一 guard 的查询／撤单，不能借作用域提交其他品种新单。"""
    scope = _scope.get()
    if scope is None or scope.guard is not guard:
        return False
    if not (operation.startswith("query_") or operation in {"cancel_order", "cancel_order_ctp"}):
        raise ExecutionTerminated(reason="终止清理期间禁止新增操作", mode="cancel_pending", cancel_unconfirmed=True)
    if symbol is not None and symbol.split(".")[-1] != scope.symbol.split(".")[-1]:
        raise ExecutionTerminated(reason="终止清理不能操作其他品种", mode="cancel_pending", cancel_unconfirmed=True)
    return cleanup_query_allowed(scope.owner)
