"""目录查询独立的账户控制上下文，不重绑交易 execution 的 guard。"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.execution_account_control import build_account_control_guard, flush_account_control_records


@contextmanager
def catalog_control_scope(account: AccountSnapshot, deadline: float) -> Iterator[AccountControlGuard]:
    """查询任务单独计数、审计及落库，等待期限不依赖某个执行的取消。"""
    guard = asyncio.run(build_account_control_guard(account, f"catalog-{uuid4().hex}", register_monitor=False))

    def checkpoint(_symbol: str | None) -> None:
        if clock_monotonic() >= deadline:
            raise TimeoutError("目录查询账户控制等待超时")

    guard.set_termination_checkpoint(checkpoint)
    try:
        yield guard
    finally:
        adapter = SimpleNamespace(
            export_account_control_records=guard.flush_records, get_account_control_guard=lambda: guard
        )
        asyncio.run(flush_account_control_records(adapter))
