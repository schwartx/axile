"""按调用上下文传递下单会话的审计归属，避免并发品种互相覆盖。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_order_context: ContextVar[dict[str, object] | None] = ContextVar("order_submission_audit_context", default=None)


@contextmanager
def order_submission_context(context: dict[str, object]) -> Iterator[None]:
    """在下单调用期间绑定当前会话上下文，返回或异常时恢复原绑定。"""
    token = _order_context.set(dict(context))
    try:
        yield
    finally:
        _order_context.reset(token)


def get_order_submission_context() -> dict[str, object] | None:
    """读取当前下单调用的会话审计上下文。"""
    return _order_context.get()
