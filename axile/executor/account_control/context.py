"""账户控制来源在调用上下文中隔离，不修改正在共享的 guard。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from axile.executor.account_control.models import AccountControlSource

_source: ContextVar[tuple[object, AccountControlSource] | None] = ContextVar("account_control_source", default=None)


quota_wait_remaining: ContextVar[Callable[[], float] | None] = ContextVar("quota_wait_remaining", default=None)


def controlled_source(guard: object, fallback: AccountControlSource) -> AccountControlSource:
    """其他 guard 不继承本次人工操作的来源。"""
    value = _source.get()
    return value[1] if value is not None and value[0] is guard else fallback


@contextmanager
def control_source_scope(guard: object, source: AccountControlSource | None) -> Iterator[None]:
    """冻结本次受控调用的来源，退出或异常时恢复外层来源。"""
    if source is None:
        yield
        return
    token = _source.set((guard, source))
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def quota_wait_budget(deadline: float | None, now: Callable[[], float]) -> Iterator[None]:
    """让本次查询的流控排队与渠道响应共用截止时间。"""
    if deadline is None:
        yield
        return
    token = quota_wait_remaining.set(lambda: deadline - now())
    try:
        yield
    finally:
        quota_wait_remaining.reset(token)
