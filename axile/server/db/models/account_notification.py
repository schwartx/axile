"""账户通知最近结果与成功的持久化摘要。"""

from dataclasses import dataclass, field


@dataclass(kw_only=True)
class AccountNotificationState:
    """每账户最多一行；保留最近结果，失败不覆盖最近成功。"""

    account_id: int
    last_success_at: str | None = field(default=None)
    execution_id: str | None = field(default=None)

    last_attempt_event_type: str | None = None
    last_attempt_at: str | None = None
    last_attempt_execution_id: str | None = None
    last_attempt_ok: bool | None = None
    last_attempt_error: str | None = None
