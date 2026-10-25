"""独立 SQL 夹具行；生产流程不再依赖这些搬运模型。"""

from pydantic import Field

from axile.server.db.models.base import Model, new_execution_id


class SupplementGroup(Model):
    """一次基础排程及其有效补发步骤。"""

    id: str = Field(default_factory=new_execution_id)
    account_id: int
    fingerprint: str
    base_scheduled_at: str
    expires_at: str
    configured_count: int
    effective_count: int
    status: str = "active"
    end_reason: str | None = None
    cancel_requested_at: str | None = None


class SupplementStep(Model):
    """触发与执行的多对一关联，合并 intent 时仍保留所有行。"""

    id: str = Field(default_factory=new_execution_id)
    group_id: str
    index: int
    scheduled_at: str
    status: str = "pending"
    execution_id: str | None = Field(default=None)
