"""目录查询独立的账户控制上下文，不重绑交易 execution 的 guard。"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlContextKind, AccountControlSource
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.execution_account_control import build_account_control_guard, flush_account_control_records


@contextmanager
def catalog_control_scope(account: AccountSnapshot, checkpoint: Callable[[], None]) -> Iterator[AccountControlGuard]:
    """查询任务单独计数、审计及落库，流控等待检查来源会话是否有效。"""
    guard = asyncio.run(
        build_account_control_guard(
            account,
            f"catalog-{uuid4().hex}",
            register_monitor=False,
            context_kind=AccountControlContextKind.ACCOUNT_OPERATION,
            source=AccountControlSource.INSTRUMENT_CATALOG,
        )
    )

    guard.set_termination_checkpoint(lambda _symbol: checkpoint())
    try:
        yield guard
    finally:
        adapter = SimpleNamespace(
            export_account_control_records=guard.flush_records, get_account_control_guard=lambda: guard
        )
        asyncio.run(flush_account_control_records(adapter))
