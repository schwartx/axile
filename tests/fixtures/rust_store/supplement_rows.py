"""独立 SQL 夹具行；生产流程不再依赖这些搬运模型。"""

from dataclasses import dataclass, field

from axile.server.db.models.base import new_execution_id


@dataclass(kw_only=True)
class SupplementGroup:
    """一次基础排程及其有效补发步骤。"""

    id: str = field(default_factory=new_execution_id)
    account_id: int
    fingerprint: str
    base_scheduled_at: str
    expires_at: str
    configured_count: int
    effective_count: int
    status: str = "active"
    end_reason: str | None = None
    cancel_requested_at: str | None = None


@dataclass(kw_only=True)
class SupplementStep:
    """触发与执行的多对一关联，合并 intent 时仍保留所有行。"""

    id: str = field(default_factory=new_execution_id)
    group_id: str
    index: int
    scheduled_at: str
    status: str = "pending"
    execution_id: str | None = field(default=None)
