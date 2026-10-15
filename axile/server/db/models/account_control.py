"""账户控制数据库模型."""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlmodel import Field, SQLModel

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlContextKind,
    AccountControlDecision,
    AccountControlScopeType,
    AccountControlSource,
)
from axile.server.db.models.base import now_ms, now_str


class AccountControlCounterDeltaBase(SQLModel):
    """账户控制 append-only 计数增量的共用字段."""

    account_id: int = Field(
        sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
    )
    execution_id: str = Field(sa_column=Column(Text, nullable=False))
    context_kind: str = Field(
        default=AccountControlContextKind.UNKNOWN.value,
        sa_column=Column(Text, nullable=False, server_default="unknown"),
    )
    source: str = Field(
        default=AccountControlSource.UNKNOWN.value, sa_column=Column(Text, nullable=False, server_default="unknown")
    )
    control_date: str = Field(sa_column=Column(Text, nullable=False))
    bucket_type: AccountControlBucketType = Field(
        sa_column=Column(SAEnum(AccountControlBucketType, name="accountcontrolbuckettype"), nullable=False)
    )
    bucket_start: str = Field(sa_column=Column(Text, nullable=False))
    scope_type: AccountControlScopeType = Field(
        sa_column=Column(SAEnum(AccountControlScopeType, name="accountcontrolscopetype"), nullable=False)
    )
    symbol: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    operation: str = Field(sa_column=Column(Text, nullable=False))
    delta_count: int = Field(sa_column=Column(Integer, nullable=False), ge=1)
    delta_uid: str = Field(sa_column=Column(Text, nullable=False))


class AccountControlCounterDelta(AccountControlCounterDeltaBase, AsyncAttrs, table=True):
    """账户控制计数增量."""

    __tablename__: ClassVar[str] = "account_control_counter_delta"
    __table_args__ = (
        UniqueConstraint("delta_uid", name="uq_account_control_counter_delta_delta_uid"),
        Index("ix_account_control_counter_delta_account_date", "account_id", "control_date"),
        Index("ix_account_control_counter_delta_execution", "execution_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)


class AccountControlEventBase(SQLModel):
    """账户控制最小行为留痕表的共用字段."""

    account_id: int = Field(
        sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
    )
    control_date: str = Field(sa_column=Column(Text, nullable=False))
    execution_id: str = Field(sa_column=Column(Text, nullable=False))
    context_kind: str = Field(
        default=AccountControlContextKind.UNKNOWN.value,
        sa_column=Column(Text, nullable=False, server_default="unknown"),
    )
    source: str = Field(
        default=AccountControlSource.UNKNOWN.value, sa_column=Column(Text, nullable=False, server_default="unknown")
    )
    seq: int = Field(sa_column=Column(Integer, nullable=False), ge=0)
    channel: TradeChannel = Field(sa_column=Column(Text, nullable=False))
    operation: str = Field(sa_column=Column(Text, nullable=False))
    symbol: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    metadata_: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", SA_JSON, nullable=False),
    )
    decision: AccountControlDecision = Field(
        sa_column=Column(SAEnum(AccountControlDecision, name="accountcontroldecision"), nullable=False)
    )
    counted: bool = Field(sa_column=Column(Boolean, nullable=False))
    outcome: str = Field(sa_column=Column(Text, nullable=False))
    event_uid: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str = Field(
        default_factory=now_str,
        sa_column=Column(Text, nullable=False),
        description="事件创建时间，秒级可读时间戳",
    )
    occurred_at_ms: int = Field(
        default_factory=now_ms,
        sa_column=Column(Integer, nullable=False),
        ge=0,
        description="事件发生时间的毫秒时间戳，用于 min_interval_ms 判断与排障",
    )


class AccountControlEvent(AccountControlEventBase, AsyncAttrs, table=True):
    """账户控制 v1 的最小事件事实表."""

    __tablename__: ClassVar[str] = "account_control_event"
    __table_args__ = (
        UniqueConstraint("event_uid", name="uq_account_control_event_event_uid"),
        Index("ix_account_control_event_account_date_created", "account_id", "control_date", "created_at"),
        Index("ix_account_control_event_execution_seq", "execution_id", "seq"),
        Index("ix_account_control_event_account_operation_occurred", "account_id", "operation", "occurred_at_ms"),
        Index(
            "ix_account_control_event_account_date_symbol_created",
            "account_id",
            "control_date",
            "symbol",
            "created_at",
        ),
        Index(
            "ix_account_control_event_account_symbol_operation_occurred",
            "account_id",
            "symbol",
            "operation",
            "occurred_at_ms",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)


class AccountControlAuditSummaryPublic(SQLModel):
    """账户控制审计汇总公开模型."""

    operation: str
    decision: AccountControlDecision
    outcome: str
    count: int


class AccountControlAuditExecutionPublic(SQLModel):
    """控制上下文聚合，保留 execution_id 字段名以兼容已有客户端。"""

    execution_id: str
    context_kind: str = "unknown"
    sources: List[str] = Field(default_factory=list)
    event_count: int
    first_event_at: str
    last_event_at: str


class AccountControlAuditExecutionListPublic(SQLModel):
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


class AccountControlEventListPublic(SQLModel):
    """账户控制审计事件分页响应."""

    data: List[AccountControlEventPublic]
    count: int
