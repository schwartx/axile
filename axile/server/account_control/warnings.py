"""由主服务唤醒已持久化的额度预警；通知事实由 Rust 入队。"""

from collections.abc import Iterable

from axile.server.native_store import native_operation


async def wake_pending_quota_warnings(account_ids: Iterable[int] | None = None) -> None:
    """执行进程只入队，主服务在提交成功后派发待处理账户。"""
    from axile.server.execution.notification_outbox import wake_notifications

    pending = (
        account_ids if account_ids is not None else (await native_operation("control_quota_pending", {}))["account_ids"]
    )
    for account_id in pending:
        wake_notifications(account_id)
