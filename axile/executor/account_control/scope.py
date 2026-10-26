"""在单个调用线程内借用 guard，不重绑正在执行的运行时。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from axile.executor.account_control.context import control_source_scope
from axile.executor.account_control.models import AccountControlSource

if TYPE_CHECKING:
    from axile.executor.account_control.guard import AccountControlGuard

_scope: ContextVar[tuple[object, AccountControlGuard] | None] = ContextVar("account_control_scope", default=None)


def scoped_guard(owner: object) -> AccountControlGuard | None:
    """只返回当前调用宿主的局部 guard。"""
    value = _scope.get()
    return value[1] if value is not None and value[0] is owner else None


@contextmanager
def account_control_scope(
    owner: object, guard: AccountControlGuard, *, source: AccountControlSource | None = None
) -> Iterator[None]:
    """临时覆盖公开操作和渠道原生操作共同读取的 guard。"""
    token = _scope.set((owner, guard))
    try:
        with control_source_scope(guard, source):
            yield
    finally:
        _scope.reset(token)
