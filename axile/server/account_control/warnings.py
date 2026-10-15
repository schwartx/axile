"""把运行中的日额度预警接入现有通知 outbox，按控制窗口持久化去重。"""

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.executor.account_control.warnings import AccountControlQuotaWarning
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.db.models.account import Account, AccountContext
from axile.server.db.models.supplement import NotificationEvent


def build_quota_warning_context(account: Account | AccountContext, warning: AccountControlQuotaWarning) -> dict:
    """构造不含凭据的系统告警输入，保留触发瞬间及本地计数口径。"""
    return {
        "event_id": warning.event_id,
        "event_type": ACCOUNT_CONTROL_WARNING_EVENT,
        "execution_id": warning.execution_id,
        "occurred_at": datetime.fromtimestamp(warning.occurred_at_ms / 1000, ZoneInfo(warning.timezone)).isoformat(),
        "account": {"id": account.id, "name": account.name},
        "error": None,
        "quota": warning.model_dump(mode="json", exclude={"account_id", "execution_id", "occurred_at_ms"}),
        "is_test": False,
    }


async def enqueue_quota_warnings(
    session: AsyncSession,
    account: Account | AccountContext,
    warnings: Iterable[dict],
) -> None:
    """在调用方事务内去重入队，不发送通知，不更改控制计数。"""
    for raw in warnings:
        warning = AccountControlQuotaWarning.model_validate(raw)
        if warning.account_id != account.id:
            raise ValueError("额度预警与账户不匹配")
        await session.execute(
            insert(NotificationEvent)
            .values(
                id=warning.event_id,
                account_id=warning.account_id,
                event_type=ACCOUNT_CONTROL_WARNING_EVENT,
                context=build_quota_warning_context(account, warning),
                created_at=clock_now().isoformat(),
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )


async def enqueue_recorded_warnings(session: AsyncSession, events: list) -> None:
    """短执行结束前保存预警；队列写入失败不回滚已提交的控制事实。"""
    by_account: dict[int, list[dict]] = {}
    for event in events:
        warning = event.metadata.get("quota_warning")
        if isinstance(warning, dict):
            by_account.setdefault(event.account_id, []).append(warning)
    if not by_account:
        return
    try:
        for account_id, warnings in by_account.items():
            account = await session.get(Account, account_id)
            if account is not None:
                await enqueue_quota_warnings(session, account, warnings)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("额度预警入队失败，已保存的控制计数不受影响")


async def wake_pending_quota_warnings(session: AsyncSession) -> None:
    """由服务进程唤醒派发，worker 的临时事件循环只负责入队。"""
    from axile.server.execution.notification_outbox import wake_notifications

    accounts = await session.scalars(
        select(NotificationEvent.account_id)
        .where(NotificationEvent.event_type == ACCOUNT_CONTROL_WARNING_EVENT, NotificationEvent.status == "pending")
        .distinct()
    )
    for account_id in accounts:
        wake_notifications(account_id)
