"""按调用上下文传递下单会话的审计归属，避免并发品种互相覆盖。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_order_context: ContextVar[dict[str, object] | None] = ContextVar("order_submission_audit_context", default=None)
_order_validation: ContextVar[Callable[[], None] | None] = ContextVar("order_submission_validation", default=None)


@contextmanager
def order_submission_context(
    context: dict[str, object], *, validate: Callable[[], None] | None = None
) -> Iterator[None]:
    """绑定本次委托的审计归属与行情证据，结束时恢复外层上下文。"""
    token = _order_context.set(dict(context))
    validation_token = _order_validation.set(validate)
    try:
        yield
    finally:
        _order_validation.reset(validation_token)
        _order_context.reset(token)


def get_order_submission_validator() -> Callable[[], None] | None:
    """读取本次委托的行情校验，供渠道显式携带到原生提交线程。"""
    return _order_validation.get()


def validate_order_submission() -> None:
    """在账户控制放行后重验本次委托实际消费的行情，不重新选取报价。"""
    validate = get_order_submission_validator()
    if validate is not None:
        validate()


def get_order_submission_context() -> dict[str, object] | None:
    """读取当前下单调用的会话审计上下文。"""
    return _order_context.get()
