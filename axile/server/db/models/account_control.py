"""账户控制数据库模型."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlContextKind,
    AccountControlDecision,
    AccountControlScopeType,
    AccountControlSource,
)
from axile.server.db.models.base import Model, now_ms, now_str


class AccountControlCounterDeltaBase(Model):
    """账户控制 append-only 计数增量的共用字段."""

    account_id: int
    execution_id: str
    context_kind: str = Field(default=AccountControlContextKind.UNKNOWN.value)
    source: str = Field(default=AccountControlSource.UNKNOWN.value)
    control_date: str
    bucket_type: AccountControlBucketType
    bucket_start: str
    scope_type: AccountControlScopeType
    symbol: Optional[str] = Field(default=None)
    operation: str
    delta_count: int = Field(ge=1)
    delta_uid: str


class AccountControlCounterDelta(AccountControlCounterDeltaBase):
    """账户控制计数增量."""

    id: Optional[int] = Field(default=None)


class AccountControlEventBase(Model):
    """账户控制最小行为留痕表的共用字段."""

    account_id: int
    control_date: str
    execution_id: str
    context_kind: str = Field(default=AccountControlContextKind.UNKNOWN.value)
    source: str = Field(default=AccountControlSource.UNKNOWN.value)
    seq: int = Field(ge=0)
    channel: TradeChannel
    operation: str
    symbol: Optional[str] = Field(default=None)
    metadata_: Dict[str, Any] = Field(default_factory=dict)
    decision: AccountControlDecision
    counted: bool
    outcome: str
    event_uid: str
    created_at: str = Field(default_factory=now_str, description="事件创建时间，秒级可读时间戳")
    occurred_at_ms: int = Field(
        default_factory=now_ms, ge=0, description="事件发生时间的毫秒时间戳，用于 min_interval_ms 判断与排障"
    )


class AccountControlEvent(AccountControlEventBase):
    """账户控制 v1 的最小事件事实表."""

    id: Optional[int] = Field(default=None)


class AccountControlAuditSummaryPublic(Model):
    """账户控制审计汇总公开模型."""

    operation: str
    decision: AccountControlDecision
    outcome: str
    count: int


class AccountControlAuditExecutionPublic(Model):
    """控制上下文聚合，保留 execution_id 字段名以兼容已有客户端。"""

    execution_id: str
    context_kind: str = "unknown"
    sources: List[str] = Field(default_factory=list)
    event_count: int
    first_event_at: str
    last_event_at: str


class AccountControlAuditExecutionListPublic(Model):
    """账户控制 execution 分页列表响应."""

    data: List[AccountControlAuditExecutionPublic]
    count: int


class AccountControlEventPublic(BaseModel):
    """账户控制审计事件的公开表示."""

    id: Optional[int]
    account_id: int
    control_date: str
    execution_id: str
    context_kind: str = AccountControlContextKind.UNKNOWN.value
    source: str = AccountControlSource.UNKNOWN.value
    seq: int
    channel: TradeChannel
    operation: str
    symbol: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    decision: AccountControlDecision
    counted: bool
    outcome: str
    event_uid: str
    created_at: str
    occurred_at_ms: int


class AccountControlEventListPublic(Model):
    """账户控制审计事件分页响应."""

    data: List[AccountControlEventPublic]
    count: int
