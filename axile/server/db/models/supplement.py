"""内部补发组、步骤与持久化通知 outbox，不提供独立 API。"""

from typing import Any

from pydantic import Field

from axile.executor.algorithms.utils.clock import clock_now
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


class NotificationEvent(Model):
    """通知派发真源；calling 在重启后变为 unknown，避免重复副作用。"""

    sequence: int | None = Field(default=None)
    id: str
    account_id: int
    created_at: str = Field(default_factory=lambda: clock_now().isoformat())
    event_type: str
    context: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    finished_at: str | None = None
    error: str | None = None
