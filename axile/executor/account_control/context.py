"""账户控制来源在调用上下文中隔离，不修改正在共享的 guard。"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from axile.executor.account_control.models import AccountControlContextKind, AccountControlSource

_source: ContextVar[tuple[object, AccountControlSource] | None] = ContextVar("account_control_source", default=None)
_LEGACY_OPERATION = re.compile(r"(orders|catalog)-[0-9a-f]{32}\Z")


def legacy_operation_source(context_id: str) -> AccountControlSource | None:
    """仅识别本服务曾生成的完整操作标识，普通历史 ID 保持未知。"""
    match = _LEGACY_OPERATION.fullmatch(context_id)
    if match is None:
        return None
    return AccountControlSource.CURRENT_ORDERS if match[1] == "orders" else AccountControlSource.INSTRUMENT_CATALOG


def control_context_kind(value: str, context_id: str) -> AccountControlContextKind:
    """已声明类型优先；只有未知历史类型允许按保留标识兼容识别。"""
    if value in {AccountControlContextKind.EXECUTION, AccountControlContextKind.ACCOUNT_OPERATION}:
        return AccountControlContextKind(value)
    if value != AccountControlContextKind.UNKNOWN:
        return AccountControlContextKind.UNKNOWN
    if legacy_operation_source(context_id) is not None:
        return AccountControlContextKind.ACCOUNT_OPERATION
    return AccountControlContextKind.UNKNOWN


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
