"""按账户顺序派发持久化通知，不重试失败或未知结果。"""

import asyncio

import loguru
from sqlmodel import col, select

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.core.db import SessionLocal
from axile.server.db.models import Account
from axile.server.db.models.supplement import NotificationEvent
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.execution.notification_state import record_notification_result

_tasks: dict[int, asyncio.Task[None]] = {}
recovering = False
_rerun: set[int] = set()


def wake_notifications(account_id: int) -> None:
    """为账户启动一个按持久化顺序消费的通知运行器。"""
    if recovering:
        return
    task = _tasks.get(account_id)
    if task is None or task.done():
        _tasks[account_id] = asyncio.create_task(_dispatch(account_id))
    else:
        _rerun.add(account_id)


async def _dispatch(account_id: int) -> None:
    """先提交 calling，再调用用户函数；异常不影响交易状态。"""
    try:
        while True:
            async with SessionLocal() as session:
                event = await session.scalar(
                    select(NotificationEvent)
                    .where(NotificationEvent.account_id == account_id, NotificationEvent.status == "pending")
                    .order_by(col(NotificationEvent.sequence))
                )
                account = await session.get(Account, account_id)
                if event is None or account is None:
                    return
                event.status = "calling"
                session.add(event)
                await session.commit()
                code, key = account.execution_notification_code, account.feishu_key
                context = {
                    **event.context,
                    "account": event.context.get("account", {"id": account.id, "name": account.name}),
                }
            result = None
            if event.event_type == ACCOUNT_CONTROL_WARNING_EVENT:
                from axile.server.error_notifications import send_system_notification

                result = await send_system_notification(context)
            elif code:
                result = (
                    NotificationFunctionResult(False, "未配置飞书 Webhook，无法发送通知")
                    if code == DEFAULT_ACCOUNT_NOTIFICATION_CODE and not key
                    else await asyncio.to_thread(run_notification_function, code, context, feishu_key=key)
                )
            async with SessionLocal() as session:
                current = await session.get(NotificationEvent, event.sequence)
                if current is None:
                    continue
                current.status = "succeeded" if result is None or result.ok else "failed"
                current.finished_at = clock_now().isoformat()
                current.error = None if result is None else result.error
                session.add(current)
                await session.commit()
            if result is not None and event.event_type != ACCOUNT_CONTROL_WARNING_EVENT:
                execution = context.get("execution") or {}
                await asyncio.to_thread(
                    record_notification_result,
                    account_id,
                    execution.get("id"),
                    current.finished_at,
                    result.ok,
                    result.error,
                    event.event_type,
                )
    except Exception:
        loguru.logger.exception("通知 outbox 派发中断 account_id={}", account_id)
    finally:
        if _tasks.get(account_id) is asyncio.current_task():
            _tasks.pop(account_id, None)
        if account_id in _rerun:
            _rerun.discard(account_id)
            asyncio.get_running_loop().call_soon(wake_notifications, account_id)


async def recover_notifications() -> None:
    """恢复未开始事件；调用结果不明的事件永久标记 unknown。"""
    system_results = []
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(NotificationEvent).where(col(NotificationEvent.status).in_(["pending", "calling"]))
                )
            ).all()
        )
        accounts = set()
        for event in rows:
            if event.status == "calling":
                event.status = "unknown"
                event.error = "服务重启，通知调用结果未知"
                event.finished_at = clock_now().isoformat()
                session.add(event)
                if event.event_type == ACCOUNT_CONTROL_WARNING_EVENT:
                    system_results.append(
                        SystemNotificationStatePublic(
                            finished_at=event.finished_at,
                            event_type=event.event_type,
                            execution_id=event.context.get("execution_id"),
                            account_id=event.account_id,
                            ok=None,
                            error=event.error,
                        )
                    )
                    continue
                from axile.server.db.models.account_notification import AccountNotificationState

                state = await session.get(AccountNotificationState, event.account_id)
                if state is None:
                    state = AccountNotificationState(account_id=event.account_id)
                state.last_attempt_at = event.finished_at
                state.last_attempt_event_type = event.event_type
                execution = event.context.get("execution") or {}
                state.last_attempt_execution_id = execution.get("id")
                state.last_attempt_ok = False
                state.last_attempt_error = event.error
                session.add(state)
            else:
                accounts.add(event.account_id)
        await session.commit()
    if system_results:
        from axile.server.execution.notification_state import record_system_notification_result

        for result in system_results:
            try:
                await asyncio.to_thread(record_system_notification_result, result)
            except Exception:
                loguru.logger.exception("恢复系统告警摘要失败，通知事件已保留为未知")
    for account_id in accounts:
        wake_notifications(account_id)
