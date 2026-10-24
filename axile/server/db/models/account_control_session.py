"""流控运行会话：仅用于交接及异常退出完整性提示。"""

from pydantic import Field

from axile.server.db.models.base import Model


class AccountControlSession(Model):
    """开始与收尾各写一次，不是计数检查点。"""

    id: int | None = Field(default=None)
    instance_id: str
    account_id: int
    execution_id: str
    context_kind: str = Field(default="unknown")
    effective_context_kind: str = Field(default="unknown", exclude=True)
    source: str = Field(default="unknown")
    timezone: str
    started_at_ms: int
    finished_at_ms: int | None = None
    status: str = "active"
