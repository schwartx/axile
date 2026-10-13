"""执行编排使用的账户控制辅助函数."""

from typing import cast

from loguru import logger
from sqlalchemy import update
from sqlmodel import col

from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import AccountControlCounterDeltaWrite, AccountControlEventWrite
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import providers
from axile.server.account_control.store import AccountControlStore
from axile.server.core.db import SessionLocal
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_control_session import AccountControlSession


async def build_account_control_guard(
    account: AccountContext,
    execution_id: str | None,
) -> AccountControlGuard:
    """
    构造本次 execution 使用的账户控制防护层.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    execution_id : str | None
        当前 execution 标识。

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
            channel=account.trade_channel,
            policy=policy,
            baseline=AccountControlCounterSnapshot(),
            recent_allowed_snapshot=AccountControlRecentAllowedSnapshot(),
        )

    control_window = resolve_control_window(policy.timezone)
    async with SessionLocal() as session:
        store = AccountControlStore(session)
        baseline = await store.load_daily_counters(account.id, control_window.control_date)
        recent_allowed_snapshot = await store.load_recent_allowed_timestamps(account.id)
    guard = AccountControlGuard(
        account_id=account.id,
        execution_id=execution_id,
        channel=account.trade_channel,
        policy=policy,
        baseline=baseline,
        recent_allowed_snapshot=recent_allowed_snapshot,
    )

    try:
        async with SessionLocal() as session:
            session.add(
                AccountControlSession(
                    instance_id=guard.instance_id,
                    account_id=account.id,
                    execution_id=execution_id,
                    timezone=policy.timezone,
                    started_at_ms=clock_now_ms(),
                )
            )
            await session.commit()
    except Exception:
        guard.monitor_incomplete = True
        logger.exception("流控监控会话登记失败，统计完整性未知")
    providers.register(guard)
    return guard


async def flush_account_control_records(executor: object) -> None:
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
        async with SessionLocal() as session:
            if guard is not None:
                await session.execute(
                    update(AccountControlSession)
                    .where(col(AccountControlSession.instance_id) == guard.instance_id)
                    .values(status="closed", finished_at_ms=clock_now_ms())
                )
            await AccountControlStore(session).flush_execution_records(counter_deltas=counter_deltas, events=events)
    except BaseException:
        if guard is not None:
            providers.finish(guard, committed=False)
        raise
    else:
        if guard is not None:
            providers.finish(guard, committed=True)
