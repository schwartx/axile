"""账户通知最近结果与成功的持久化摘要。"""

from pydantic import Field

from axile.server.db.models.base import Model


class AccountNotificationState(Model):
    """每账户最多一行；保留最近结果，失败不覆盖最近成功。"""

    account_id: int
    last_success_at: str | None = Field(default=None)
    execution_id: str | None = Field(default=None)

    last_attempt_event_type: str | None = None
    last_attempt_at: str | None = None
    last_attempt_execution_id: str | None = None
    last_attempt_ok: bool | None = None
    last_attempt_error: str | None = None


class AccountNotificationStatePublic(Model):
    """通知函数最近正常返回的时间和对应执行。"""

    last_success_at: str | None = None
    execution_id: str | None = None
    last_attempt_event_type: str | None = None
    last_attempt_at: str | None = None
    last_attempt_execution_id: str | None = None
    last_attempt_ok: bool | None = None
    last_attempt_error: str | None = None
