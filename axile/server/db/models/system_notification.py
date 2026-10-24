"""系统告警的最近真实发送结果；试跑不写入此状态。"""

from axile.server.db.models.base import Model


class SystemNotificationStatePublic(Model):
    """发送结果与触发事件关联，失败只表示通知失败。"""

    finished_at: str | None = None
    mode: str | None = None
    event_type: str | None = None
    execution_id: str | None = None
    account_id: int | None = None
    ok: bool | None = None
    error: str | None = None
