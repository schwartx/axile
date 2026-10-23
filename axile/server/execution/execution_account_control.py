"""执行编排使用的账户控制辅助函数."""

import asyncio
from typing import cast

from loguru import logger

from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import (
    AccountControlContextKind,
    AccountControlCounterDeltaWrite,
    AccountControlEventWrite,
    AccountControlSource,
)
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import providers
from axile.server.account_control.store import AccountControlStore
from axile.server.db.models.account import AccountContext


async def build_account_control_guard(
    account: AccountContext,
    execution_id: str | None,
    *,
    register_monitor: bool = True,
    context_kind: AccountControlContextKind = AccountControlContextKind.EXECUTION,
    source: AccountControlSource = AccountControlSource.ALGORITHM,
) -> AccountControlGuard:
    """
    构造本次 execution 使用的账户控制防护层.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    execution_id : str | None
        当前控制上下文标识；独立账户操作沿用兼容字段。
    register_monitor : bool, optional
        是否注册为账户当前交易控制实例。
    context_kind : AccountControlContextKind, optional
        交易执行或独立账户操作。
    source : AccountControlSource, optional
        此 guard 的默认调用来源，人工操作可在局部上下文中覆盖。

    Returns
    -------
    AccountControlGuard
        已加载基线计数与最近放行快照的账户控制防护层。
    """
    policy = resolve_account_control_policy(account.account_control_preset, account.account_control_override)
    if account.id is None or execution_id is None:
        return AccountControlGuard(
            account_id=account.id,
            execution_id=execution_id,
            context_kind=context_kind,
            source=source,
            channel=account.trade_channel,
            policy=policy,
            baseline=AccountControlCounterSnapshot(),
            recent_allowed_snapshot=AccountControlRecentAllowedSnapshot(),
        )

    control_window = resolve_control_window(policy.timezone)
    store = AccountControlStore()
    active_guard = providers.active_guard(account.id)
    baseline, recent_allowed_snapshot, incomplete = await store.load_baseline(
        account.id,
        control_window.control_date,
        policy.timezone,
        active_instance_id=None if active_guard is None else active_guard.instance_id,
    )
    guard = AccountControlGuard(
        account_id=account.id,
        execution_id=execution_id,
        context_kind=context_kind,
        source=source,
        channel=account.trade_channel,
        policy=policy,
        baseline=baseline,
        recent_allowed_snapshot=recent_allowed_snapshot,
    )

    if incomplete:
        guard.incomplete_control_dates = frozenset({control_window.control_date})
        guard.monitor_incomplete = True

    try:
        await store.register_session(
            instance_id=guard.instance_id,
            account_id=account.id,
            execution_id=execution_id,
            context_kind=context_kind.value,
            source=source.value,
            timezone=policy.timezone,
            started_at_ms=clock_now_ms(),
        )
    except Exception:
        guard.monitor_incomplete = True
        guard.counter_integrity_unavailable = True
        logger.exception("流控监控会话登记失败，统计完整性未知")
    if register_monitor:
        providers.register(guard)
    return guard


async def flush_account_control_records(executor: object) -> None:
    """等待同 guard 的人工操作结束后，持久化最终计数并关闭控制会话。"""
    getter = getattr(executor, "get_account_control_guard", None)
    guard = cast(AccountControlGuard | None, getter() if callable(getter) else None)
    if guard is None:
        await _flush_account_control_records(executor)
        return
    lock = providers.operation_lock(guard)
    if not lock.acquire(blocking=False):
        await asyncio.to_thread(lock.acquire)
    try:
        await _flush_account_control_records(executor)
    finally:
        lock.release()


async def _flush_account_control_records(executor: object) -> None:
    """
    将执行器当前累积的账户控制记录刷入 SQLite.

    Parameters
    ----------
    executor : object
        当前执行器实例。

    Returns
    -------
    None
        该函数仅负责持久化账户控制记录，不返回结果。
    """
    export_records = getattr(executor, "export_account_control_records", None)
    if not callable(export_records):
        return

    counter_deltas, events = cast(
        "tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]",
        export_records(),
    )
    getter = getattr(executor, "get_account_control_guard", None)
    guard = cast(AccountControlGuard | None, getter() if callable(getter) else None)
    try:
        await AccountControlStore().flush_execution_records(
            counter_deltas=counter_deltas,
            events=events,
            close_session=None
            if guard is None
            else {"instance_id": guard.instance_id, "finished_at_ms": clock_now_ms()},
        )
    except BaseException:
        if guard is not None:
            providers.finish(guard, committed=False)
        raise
    else:
        if guard is not None:
            providers.finish(guard, committed=True)
