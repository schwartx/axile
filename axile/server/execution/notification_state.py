"""通知线程通过原生短事务保存最近完成的发送摘要。"""

from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.native_store import native_operation_sync


def record_system_notification_result(result: SystemNotificationStatePublic) -> None:
    """保存最近完成的真实告警；试跑由调用方隔离。"""
    if result.finished_at is None:
        raise ValueError("真实告警结果必须包含完成时间")
    native_operation_sync("notifications_record_system", result.model_dump(mode="json"))


def get_system_notification_result() -> SystemNotificationStatePublic:
    """读取持久化摘要；从未发送时返回空结果。"""
    return SystemNotificationStatePublic.model_validate(native_operation_sync("notifications_get_system", {}))


def record_notification_result(
    account_id: int,
    execution_id: str | None,
    finished_at: str,
    ok: bool,
    error: str | None,
    event_type: str = "execution.finished",
) -> None:
    """保存最近完成的通知结果；失败保留此前成功摘要。"""
    native_operation_sync(
        "notifications_record_account",
        {
            "account_id": account_id,
            "execution_id": execution_id,
            "finished_at": finished_at,
            "ok": ok,
            "error": error,
            "event_type": event_type,
        },
    )
